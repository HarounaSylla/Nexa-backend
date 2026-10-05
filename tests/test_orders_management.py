import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import delete, select

from app.agent.models import Conversation, Message
from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.main import app
from app.merchants.models import MerchantPaymentLink
from app.notifications.models import Notification
from app.orders.models import (
    Deliverer,
    DeliveryZone,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    StockMovement,
)
from app.orders.service import creer_commande, normalize_city, obtenir_disponibilite


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _auth(clerk_user_id: str):
    return patch(
        "app.auth.deps.verify_clerk_session_token",
        return_value=clerk_user_id,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid.token"}


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(Notification).where(Notification.merchant_id.in_(merchant_ids))
        )
        order_ids = list(
            (
                await db.execute(
                    select(Order.id).where(Order.merchant_id.in_(merchant_ids))
                )
            ).scalars().all()
        )
        if order_ids:
            await db.execute(
                delete(StockMovement).where(StockMovement.order_id.in_(order_ids))
            )
            await db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            await db.execute(delete(Order).where(Order.id.in_(order_ids)))
        conversation_ids = list(
            (
                await db.execute(
                    select(Conversation.id).where(
                        Conversation.merchant_id.in_(merchant_ids)
                    )
                )
            ).scalars().all()
        )
        if conversation_ids:
            await db.execute(
                delete(Message).where(Message.conversation_id.in_(conversation_ids))
            )
            await db.execute(
                delete(Conversation).where(Conversation.id.in_(conversation_ids))
            )
        await db.execute(
            delete(Deliverer).where(Deliverer.merchant_id.in_(merchant_ids))
        )
        await db.execute(
            delete(DeliveryZone).where(DeliveryZone.merchant_id.in_(merchant_ids))
        )
        await db.execute(
            delete(MerchantPaymentLink).where(
                MerchantPaymentLink.merchant_id.in_(merchant_ids)
            )
        )
        await db.execute(delete(Product).where(Product.merchant_id.in_(merchant_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_shop(
    *,
    name: str,
    clerk_user_id: str,
    stock_qty: int = 10,
    price: str = "1000.00",
    whatsapp_phone_number_id: str | None = None,
) -> tuple[Merchant, Product]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=name,
            clerk_user_id=clerk_user_id,
            whatsapp_phone_number_id=whatsapp_phone_number_id,
        )
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Article dashboard",
            description="pour tests commandes dashboard",
            category="tests",
            price=Decimal(price),
            stock_qty=stock_qty,
        )
        db.add(product)
        db.add(
            DeliveryZone(
                merchant_id=merchant.id,
                city="Dakar",
                city_normalized=normalize_city("Dakar"),
                available=True,
                min_delivery_hours=24,
                max_delivery_hours=48,
            )
        )
        await db.commit()
        await db.refresh(merchant)
        await db.refresh(product)
        return merchant, product


async def _place(
    merchant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    payment_method: PaymentMethod = PaymentMethod.cash_on_delivery,
    quantity: int = 1,
) -> Order:
    async with AsyncSessionLocal() as db:
        return await creer_commande(
            db,
            merchant_id=merchant_id,
            customer_phone="+221770000010",
            items=[(product_id, quantity)],
            payment_method=payment_method,
            delivery_address="Sacré-Cœur, Dakar",
            ville="Dakar",
        )


@pytest.mark.asyncio
async def test_order_list_and_detail_are_merchant_scoped() -> None:
    owner_clerk = f"user_ord_owner_{uuid.uuid4()}"
    other_clerk = f"user_ord_other_{uuid.uuid4()}"
    owner, owner_product = await _seed_shop(
        name=f"pytest-ord-owner-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
        price="2500.00",
    )
    other, other_product = await _seed_shop(
        name=f"pytest-ord-other-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
        price="9000.00",
    )
    try:
        mine = await _place(owner.id, owner_product.id, quantity=2)
        theirs = await _place(other.id, other_product.id, quantity=1)

        with _auth(owner_clerk):
            async with await _client() as client:
                listed = await client.get("/orders", headers=_headers())
                assert listed.status_code == 200, listed.text
                rows = listed.json()
                ids = [row["id"] for row in rows]
                assert str(mine.id) in ids
                assert str(theirs.id) not in ids
                mine_row = next(row for row in rows if row["id"] == str(mine.id))
                assert mine_row["item_count"] == 1
                assert mine_row["total"] == "5000.00"
                assert mine_row["status"] == OrderStatus.created.value
                assert mine_row["payment_method"] == PaymentMethod.cash_on_delivery.value
                assert mine_row["payment_status"] == PaymentStatus.pending.value
                assert mine_row["payment_link"] is None

                detail = await client.get(f"/orders/{mine.id}", headers=_headers())
                assert detail.status_code == 200, detail.text
                body = detail.json()
                assert body["id"] == str(mine.id)
                assert body["items"][0]["product_name"] == "Article dashboard"
                assert body["items"][0]["quantity"] == 2
                assert body["items"][0]["unit_price"] == "2500.00"
                assert body["deliverer"] is None

                hidden = await client.get(f"/orders/{theirs.id}", headers=_headers())
                assert hidden.status_code == 404
                assert hidden.json()["detail"] == "Order was not found"
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_assign_confirm_cancel_through_authenticated_routes() -> None:
    clerk_user_id = f"user_ord_actions_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-ord-actions-{uuid.uuid4()}",
        clerk_user_id=clerk_user_id,
        stock_qty=8,
    )
    try:
        to_assign = await _place(merchant.id, product.id, quantity=1)
        to_cancel = await _place(merchant.id, product.id, quantity=2)

        with _auth(clerk_user_id):
            async with await _client() as client:
                created_deliverer = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Moussa", "phone": "+221770001111"},
                )
                assert created_deliverer.status_code == 200, created_deliverer.text
                deliverer_id = created_deliverer.json()["id"]

                assigned = await client.post(
                    f"/orders/{to_assign.id}/assign-deliverer",
                    headers=_headers(),
                    json={"deliverer_id": deliverer_id},
                )
                assert assigned.status_code == 200, assigned.text
                assert assigned.json()["status"] == OrderStatus.deliverer_assigned.value
                assert assigned.json()["deliverer_id"] == deliverer_id

                detail = await client.get(
                    f"/orders/{to_assign.id}", headers=_headers()
                )
                assert detail.json()["deliverer"]["name"] == "Moussa"
                assert detail.json()["deliverer"]["phone"] == "+221770001111"

                confirmed = await client.post(
                    f"/orders/{to_assign.id}/confirm-delivery",
                    headers=_headers(),
                )
                assert confirmed.status_code == 200, confirmed.text
                assert confirmed.json()["status"] == OrderStatus.delivered.value
                assert confirmed.json()["payment_status"] == PaymentStatus.paid.value

                async with AsyncSessionLocal() as db:
                    stock_before_cancel = await obtenir_disponibilite(db, product.id)

                cancelled = await client.post(
                    f"/orders/{to_cancel.id}/cancel",
                    headers=_headers(),
                    json={"reason": "test cancel"},
                )
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["status"] == OrderStatus.cancelled.value
                assert cancelled.json()["payment_status"] == PaymentStatus.pending.value

                async with AsyncSessionLocal() as db:
                    stock_after_cancel = await obtenir_disponibilite(db, product.id)
                assert stock_after_cancel == stock_before_cancel + 2
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_assign_confirm_cancel_404_for_other_merchant_order() -> None:
    owner_clerk = f"user_ord_act_a_{uuid.uuid4()}"
    other_clerk = f"user_ord_act_b_{uuid.uuid4()}"
    owner, owner_product = await _seed_shop(
        name=f"pytest-ord-act-a-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
    )
    other, other_product = await _seed_shop(
        name=f"pytest-ord-act-b-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
    )
    try:
        theirs = await _place(other.id, other_product.id)
        with _auth(owner_clerk):
            async with await _client() as client:
                created = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Fatou", "phone": "+221770002222"},
                )
                deliverer_id = created.json()["id"]
                assigned = await client.post(
                    f"/orders/{theirs.id}/assign-deliverer",
                    headers=_headers(),
                    json={"deliverer_id": deliverer_id},
                )
                assert assigned.status_code == 404
                confirmed = await client.post(
                    f"/orders/{theirs.id}/confirm-delivery",
                    headers=_headers(),
                )
                assert confirmed.status_code == 404
                cancelled = await client.post(
                    f"/orders/{theirs.id}/cancel",
                    headers=_headers(),
                )
                assert cancelled.status_code == 404
    finally:
        await _cleanup(owner.id, other.id)


def test_build_payment_link_message_exact_text() -> None:
    from app.orders.service import build_payment_link_message

    first = build_payment_link_message(
        12, Decimal("25000.00"), "https://pay.example.com/x", False, "Wave"
    )
    assert first == (
        "Bonjour, voici le lien Wave pour régler votre commande n°12 (25 000 FCFA) :\n"
        "https://pay.example.com/x\n"
        "\n"
        "Après le paiement, envoyez-nous ici une photo ou une capture "
        "d'écran de la preuve de paiement, en indiquant le numéro de "
        "commande 12. Plusieurs clients peuvent payer avec le même lien : "
        "c'est cette preuve qui nous permet de retrouver votre paiement."
    )
    updated = build_payment_link_message(
        12, Decimal("25000.00"), "https://pay.example.com/y", True, "Orange Money"
    )
    assert updated.startswith(
        "Bonjour, voici à nouveau le lien Orange Money pour régler votre commande "
        "n°12 (25 000 FCFA) :"
    )
    assert "https://pay.example.com/y" in updated
    assert "preuve de paiement" in updated
    unnamed = build_payment_link_message(
        12, Decimal("25000.00"), "https://pay.example.com/z", False, None
    )
    assert unnamed.startswith(
        "Bonjour, voici le lien pour régler votre commande n°12 (25 000 FCFA) :"
    )
    unnamed_again = build_payment_link_message(
        12, Decimal("25000.00"), "https://pay.example.com/z", True, None
    )
    assert unnamed_again.startswith(
        "Bonjour, voici à nouveau le lien pour régler votre commande "
        "n°12 (25 000 FCFA) :"
    )


async def _add_link(
    merchant_id: uuid.UUID, label: str, url: str
) -> MerchantPaymentLink:
    async with AsyncSessionLocal() as db:
        row = MerchantPaymentLink(merchant_id=merchant_id, label=label, url=url)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _attach_conversation(
    merchant_id: uuid.UUID, order_id: uuid.UUID, phone: str
) -> Conversation:
    async with AsyncSessionLocal() as db:
        conversation = Conversation(
            merchant_id=merchant_id,
            customer_phone=phone,
            status="active",
        )
        db.add(conversation)
        await db.flush()
        order = await db.get(Order, order_id)
        assert order is not None
        order.conversation_id = conversation.id
        await db.commit()
        await db.refresh(conversation)
        return conversation


@pytest.mark.asyncio
async def test_send_payment_link_success_resend_and_no_conversation() -> None:
    clerk_user_id = f"user_ord_pay_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-ord-pay-{uuid.uuid4()}",
        clerk_user_id=clerk_user_id,
        stock_qty=8,
        price="25000.00",
        whatsapp_phone_number_id=f"pnid-pay-{uuid.uuid4()}",
    )
    try:
        online = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        conversation = await _attach_conversation(
            merchant.id, online.id, online.customer_phone
        )
        orphan = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        first = await _add_link(
            merchant.id, "Wave", "https://pay.example.com/nexa-test"
        )
        second = await _add_link(
            merchant.id, "Orange Money", "https://pay.example.com/nexa-updated"
        )

        with (
            _auth(clerk_user_id),
            patch("app.whatsapp.service.settings.whatsapp_access_token", "tok"),
            patch(
                "app.whatsapp.service.envoyer_texte_whatsapp", new_callable=AsyncMock
            ) as send,
        ):
            async with await _client() as client:
                rejected_old = await client.post(
                    f"/orders/{online.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link": first.url},
                )
                assert rejected_old.status_code == 422

                accepted = await client.post(
                    f"/orders/{online.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(first.id)},
                )
                assert accepted.status_code == 200, accepted.text
                body = accepted.json()
                assert body["payment_link"] == first.url
                assert body["payment_link_label"] == "Wave"
                assert body["payment_status"] == PaymentStatus.pending.value
                assert body["payment_link_sent_at"] is not None

                resent = await client.post(
                    f"/orders/{online.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(second.id)},
                )
                assert resent.status_code == 200, resent.text
                assert resent.json()["payment_link"] == second.url
                assert resent.json()["payment_link_label"] == "Orange Money"
                assert resent.json()["payment_link_sent_at"] is not None

                orphaned = await client.post(
                    f"/orders/{orphan.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(first.id)},
                )
                assert orphaned.status_code == 200, orphaned.text
                assert orphaned.json()["payment_link_sent_at"] is not None

        assert send.await_count == 3
        first_text = send.await_args_list[0].args[1]
        assert str(online.order_number) in first_text
        assert "25 000 FCFA" in first_text
        assert first.url in first_text
        assert "Wave" in first_text
        assert "preuve de paiement" in first_text
        assert "à nouveau" not in first_text
        second_text = send.await_args_list[1].args[1]
        assert second_text.startswith(
            f"Bonjour, voici à nouveau le lien Orange Money pour régler "
            f"votre commande n°{online.order_number}"
        )
        assert second.url in second_text

        async with AsyncSessionLocal() as db:
            stored = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.conversation_id == conversation.id
                        )
                    )
                ).scalars().all()
            )
            assert len(stored) == 2
            assert stored[0].turn_role == "merchant"
            assert stored[0].items == []
            assert str(online.order_number) in stored[0].display_text
            assert first.url in stored[0].display_text
            assert "Wave" in stored[0].display_text
            assert "preuve de paiement" in stored[0].display_text
            assert "à nouveau" in stored[1].display_text
            assert "Orange Money" in stored[1].display_text
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_send_payment_link_failures_and_rules() -> None:
    from app.whatsapp.service import WhatsAppSendError

    clerk_user_id = f"user_ord_pay_fail_{uuid.uuid4()}"
    other_clerk = f"user_ord_pay_other_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-ord-pay-fail-{uuid.uuid4()}",
        clerk_user_id=clerk_user_id,
        stock_qty=8,
        whatsapp_phone_number_id=f"pnid-fail-{uuid.uuid4()}",
    )
    bare, bare_product = await _seed_shop(
        name=f"pytest-ord-pay-bare-{uuid.uuid4()}",
        clerk_user_id=f"user_ord_pay_bare_{uuid.uuid4()}",
        stock_qty=3,
    )
    other, other_product = await _seed_shop(
        name=f"pytest-ord-pay-other-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
        stock_qty=3,
        whatsapp_phone_number_id=f"pnid-other-{uuid.uuid4()}",
    )
    try:
        online = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        window = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        conversation = await _attach_conversation(
            merchant.id, window.id, window.customer_phone
        )
        generic = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        cod = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.cash_on_delivery
        )
        cancelled = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        paid = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        bare_online = await _place(
            bare.id, bare_product.id, payment_method=PaymentMethod.online
        )
        theirs = await _place(
            other.id, other_product.id, payment_method=PaymentMethod.online
        )
        configured = await _add_link(
            merchant.id, "Wave", "https://pay.example.com/nexa-fail"
        )
        theirs_link = await _add_link(
            other.id, "Wave", "https://pay.example.com/other"
        )
        unknown_id = uuid.uuid4()

        with _auth(clerk_user_id):
            async with await _client() as client:
                cancelled_resp = await client.post(
                    f"/orders/{cancelled.id}/cancel", headers=_headers()
                )
                assert cancelled_resp.status_code == 200
                paid_resp = await client.post(
                    f"/orders/{paid.id}/mark-paid", headers=_headers()
                )
                assert paid_resp.status_code == 200

        with (
            _auth(clerk_user_id),
            patch("app.whatsapp.service.settings.whatsapp_access_token", "tok"),
            patch(
                "app.whatsapp.service.envoyer_message_commercant",
                new_callable=AsyncMock,
                side_effect=[
                    WhatsAppSendError("whatsapp_window_closed"),
                    WhatsAppSendError("whatsapp_send_failed"),
                ],
            ) as send,
        ):
            async with await _client() as client:
                missing = await client.post(
                    f"/orders/{online.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(unknown_id)},
                )
                assert missing.status_code == 404
                assert missing.json()["detail"] == "Payment link was not found"

                foreign = await client.post(
                    f"/orders/{online.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(theirs_link.id)},
                )
                assert foreign.status_code == 404
                assert foreign.json()["detail"] == "Payment link was not found"

                closed = await client.post(
                    f"/orders/{window.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(configured.id)},
                )
                assert closed.status_code == 502, closed.text
                assert closed.json()["detail"] == "whatsapp_window_closed"

                failed = await client.post(
                    f"/orders/{generic.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(configured.id)},
                )
                assert failed.status_code == 502
                assert failed.json()["detail"] == "whatsapp_send_failed"

                on_cod = await client.post(
                    f"/orders/{cod.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(configured.id)},
                )
                assert on_cod.status_code == 409
                assert "online-payment" in on_cod.json()["detail"]

                on_cancelled = await client.post(
                    f"/orders/{cancelled.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(configured.id)},
                )
                assert on_cancelled.status_code == 409
                assert "send a payment link for" in on_cancelled.json()["detail"]

                on_paid = await client.post(
                    f"/orders/{paid.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(configured.id)},
                )
                assert on_paid.status_code == 409
                assert on_paid.json()["detail"] == "This order is already paid"

                stolen = await client.post(
                    f"/orders/{theirs.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(configured.id)},
                )
                assert stolen.status_code == 404
                assert stolen.json()["detail"] == "Order was not found"

        assert send.await_count == 2

        async with AsyncSessionLocal() as db:
            window_row = await db.get(Order, window.id)
            generic_row = await db.get(Order, generic.id)
            assert window_row is not None
            assert window_row.payment_link == configured.url
            assert window_row.payment_link_label == "Wave"
            assert window_row.payment_link_sent_at is None
            assert generic_row is not None
            assert generic_row.payment_link == configured.url
            assert generic_row.payment_link_label == "Wave"
            assert generic_row.payment_link_sent_at is None
            stored = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.conversation_id == conversation.id
                        )
                    )
                ).scalars().all()
            )
            assert stored == []

        bare_link = await _add_link(
            bare.id, "Wave", "https://pay.example.com/nexa-fail"
        )
        with _auth(bare.clerk_user_id):
            async with await _client() as client:
                unconfigured = await client.post(
                    f"/orders/{bare_online.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": str(bare_link.id)},
                )
                assert unconfigured.status_code == 502
                assert unconfigured.json()["detail"] == "whatsapp_not_configured"

        async with await _client() as client:
            unauth = await client.post(
                f"/orders/{online.id}/send-payment-link",
                json={"payment_link_id": str(configured.id)},
            )
            assert unauth.status_code == 401
    finally:
        await _cleanup(merchant.id, bare.id, other.id)


@pytest.mark.asyncio
async def test_deliverers_are_merchant_scoped() -> None:
    owner_clerk = f"user_del_a_{uuid.uuid4()}"
    other_clerk = f"user_del_b_{uuid.uuid4()}"
    owner, _owner_product = await _seed_shop(
        name=f"pytest-del-a-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
    )
    other, _other_product = await _seed_shop(
        name=f"pytest-del-b-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
    )
    try:
        with _auth(owner_clerk):
            async with await _client() as client:
                created = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Livreur Awa", "phone": "+221770003333"},
                )
                assert created.status_code == 200, created.text
                owner_list = await client.get("/deliverers", headers=_headers())
                assert owner_list.status_code == 200
                names = [row["name"] for row in owner_list.json()]
                assert names == ["Livreur Awa"]

        with _auth(other_clerk):
            async with await _client() as client:
                other_list = await client.get("/deliverers", headers=_headers())
                assert other_list.status_code == 200
                assert other_list.json() == []
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_authenticated_create_order_is_scoped_to_session_merchant() -> None:
    owner_clerk = f"user_ord_create_{uuid.uuid4()}"
    other_clerk = f"user_ord_create_other_{uuid.uuid4()}"
    owner, owner_product = await _seed_shop(
        name=f"pytest-ord-create-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
        stock_qty=6,
    )
    other, other_product = await _seed_shop(
        name=f"pytest-ord-create-other-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
        stock_qty=6,
    )
    payload = {
        "customer_phone": "+221770000040",
        "items": [{"product_id": str(owner_product.id), "quantity": 1}],
        "payment_method": PaymentMethod.cash_on_delivery.value,
        "delivery_address": "Sacré-Cœur, Dakar",
        "ville": "Dakar",
        "merchant_id": str(other.id),
    }
    try:
        async with await _client() as client:
            unauth = await client.post("/orders", json=payload)
            assert unauth.status_code == 401

        with _auth(owner_clerk):
            async with await _client() as client:
                created = await client.post(
                    "/orders", headers=_headers(), json=payload
                )
                assert created.status_code == 200, created.text
                body = created.json()
                assert body["merchant_id"] == str(owner.id)
                assert body["customer_phone"] == "+221770000040"

        async with AsyncSessionLocal() as db:
            stored = (
                await db.execute(select(Order).where(Order.id == body["id"]))
            ).scalar_one()
            assert stored.merchant_id == owner.id
            assert stored.conversation_id is None
            assert stored.order_number == 1
            other_stock = await obtenir_disponibilite(db, other_product.id)
            assert other_stock == 6
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_delivery_zone_routes_are_authenticated_and_merchant_scoped() -> None:
    owner_clerk = f"user_zone_owner_{uuid.uuid4()}"
    other_clerk = f"user_zone_other_{uuid.uuid4()}"
    owner, _owner_product = await _seed_shop(
        name=f"pytest-zones-owner-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
    )
    other, _other_product = await _seed_shop(
        name=f"pytest-zones-other-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
    )
    try:
        async with await _client() as client:
            unauth = await client.get("/orders/delivery-zones")
            assert unauth.status_code == 401

        with _auth(other_clerk):
            async with await _client() as client:
                other_thies = await client.post(
                    "/orders/delivery-zones",
                    headers=_headers(),
                    json={
                        "city": "Thiès",
                        "available": True,
                        "min_delivery_hours": 48,
                        "max_delivery_hours": 72,
                    },
                )
                assert other_thies.status_code == 200, other_thies.text
                other_zone_id = other_thies.json()["id"]

        with _auth(owner_clerk):
            async with await _client() as client:
                listed = await client.get(
                    "/orders/delivery-zones", headers=_headers()
                )
                assert listed.status_code == 200
                cities = [row["city"] for row in listed.json()]
                assert cities == ["Dakar"]
                assert all(
                    row["merchant_id"] == str(owner.id) for row in listed.json()
                )

                created = await client.post(
                    "/orders/delivery-zones",
                    headers=_headers(),
                    json={
                        "city": "Thiès",
                        "available": False,
                        "min_delivery_hours": 48,
                        "max_delivery_hours": 72,
                    },
                )
                assert created.status_code == 200, created.text
                assert created.json()["merchant_id"] == str(owner.id)
                assert created.json()["city"] == "Thiès"

                listed_after = await client.get(
                    "/orders/delivery-zones", headers=_headers()
                )
                owner_cities = {row["city"] for row in listed_after.json()}
                assert owner_cities == {"Dakar", "Thiès"}

                stolen = await client.delete(
                    f"/orders/delivery-zones/{other_zone_id}",
                    headers=_headers(),
                )
                assert stolen.status_code == 404

        async with AsyncSessionLocal() as db:
            still = await db.get(DeliveryZone, uuid.UUID(other_zone_id))
            assert still is not None
            assert still.merchant_id == other.id
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_mark_paid_online_idempotent_and_allowed_after_delivery() -> None:
    clerk_user_id = f"user_ord_mark_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-ord-mark-{uuid.uuid4()}",
        clerk_user_id=clerk_user_id,
        stock_qty=6,
    )
    try:
        online = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        delivered_online = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        with _auth(clerk_user_id):
            async with await _client() as client:
                first = await client.post(
                    f"/orders/{online.id}/mark-paid", headers=_headers()
                )
                assert first.status_code == 200, first.text
                assert first.json()["payment_status"] == PaymentStatus.paid.value
                assert first.json()["payment_method"] == PaymentMethod.online.value

                second = await client.post(
                    f"/orders/{online.id}/mark-paid", headers=_headers()
                )
                assert second.status_code == 200, second.text
                assert second.json()["payment_status"] == PaymentStatus.paid.value

                confirmed = await client.post(
                    f"/orders/{delivered_online.id}/confirm-delivery",
                    headers=_headers(),
                )
                assert confirmed.status_code == 200, confirmed.text
                assert confirmed.json()["status"] == OrderStatus.delivered.value
                assert confirmed.json()["payment_status"] == PaymentStatus.pending.value

                after_delivery = await client.post(
                    f"/orders/{delivered_online.id}/mark-paid",
                    headers=_headers(),
                )
                assert after_delivery.status_code == 200, after_delivery.text
                assert after_delivery.json()["payment_status"] == PaymentStatus.paid.value
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_mark_paid_rejected_on_cod_and_cancelled_online() -> None:
    clerk_user_id = f"user_ord_mark_rej_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-ord-mark-rej-{uuid.uuid4()}",
        clerk_user_id=clerk_user_id,
        stock_qty=4,
    )
    try:
        cod = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.cash_on_delivery
        )
        online = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        with _auth(clerk_user_id):
            async with await _client() as client:
                cancelled = await client.post(
                    f"/orders/{online.id}/cancel", headers=_headers()
                )
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["payment_status"] == PaymentStatus.pending.value

                on_cod = await client.post(
                    f"/orders/{cod.id}/mark-paid", headers=_headers()
                )
                assert on_cod.status_code == 409, on_cod.text
                assert on_cod.json()["detail"] == (
                    "Only online-payment orders can be marked as paid; "
                    "cash-on-delivery orders are marked paid when delivery is confirmed"
                )

                on_cancelled = await client.post(
                    f"/orders/{online.id}/mark-paid", headers=_headers()
                )
                assert on_cancelled.status_code == 409, on_cancelled.text
                assert "mark as paid" in on_cancelled.json()["detail"]

                detail_cod = await client.get(f"/orders/{cod.id}", headers=_headers())
                assert detail_cod.json()["payment_status"] == PaymentStatus.pending.value
                detail_online = await client.get(
                    f"/orders/{online.id}", headers=_headers()
                )
                assert detail_online.json()["payment_status"] == PaymentStatus.pending.value
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_mark_paid_404_other_merchant_and_401_without_token() -> None:
    owner_clerk = f"user_ord_mark_a_{uuid.uuid4()}"
    other_clerk = f"user_ord_mark_b_{uuid.uuid4()}"
    owner, _owner_product = await _seed_shop(
        name=f"pytest-ord-mark-a-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
    )
    other, other_product = await _seed_shop(
        name=f"pytest-ord-mark-b-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
    )
    try:
        theirs = await _place(
            other.id, other_product.id, payment_method=PaymentMethod.online
        )
        async with await _client() as client:
            unauth = await client.post(f"/orders/{theirs.id}/mark-paid")
            assert unauth.status_code == 401

        with _auth(owner_clerk):
            async with await _client() as client:
                stolen = await client.post(
                    f"/orders/{theirs.id}/mark-paid", headers=_headers()
                )
                assert stolen.status_code == 404
                assert stolen.json()["detail"] == "Order was not found"
    finally:
        await _cleanup(owner.id, other.id)
