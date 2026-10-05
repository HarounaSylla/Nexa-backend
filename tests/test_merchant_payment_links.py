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
    DeliveryZone,
    Order,
    OrderItem,
    PaymentMethod,
    StockMovement,
)
from app.orders.service import creer_commande, normalize_city


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


async def _seed_shop(clerk_user_id: str) -> tuple[Merchant, Product]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=f"pytest-paylink-{uuid.uuid4()}",
            clerk_user_id=clerk_user_id,
            whatsapp_phone_number_id=f"pnid-paylink-{uuid.uuid4()}",
        )
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Article lien",
            category="tests",
            price=Decimal("4000.00"),
            stock_qty=10,
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


@pytest.mark.asyncio
async def test_payment_link_crud_and_validation() -> None:
    clerk = f"user_paylink_{uuid.uuid4()}"
    other_clerk = f"user_paylink_other_{uuid.uuid4()}"
    merchant, _product = await _seed_shop(clerk)
    other, _ = await _seed_shop(other_clerk)
    try:
        async with await _client() as client:
            unauth = await client.get("/merchants/me/payment-links")
            assert unauth.status_code == 401

        with _auth(clerk):
            async with await _client() as client:
                empty = await client.get(
                    "/merchants/me/payment-links", headers=_headers()
                )
                assert empty.status_code == 200
                assert empty.json() == []

                created = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "  Wave  ", "url": "https://pay.example.com/wave"},
                )
                assert created.status_code == 201, created.text
                wave = created.json()
                assert wave["label"] == "Wave"
                assert wave["url"] == "https://pay.example.com/wave"
                wave_id = wave["id"]

                http = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "Bad", "url": "http://pay.example.com/x"},
                )
                assert http.status_code == 422

                js = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "Bad", "url": "javascript:alert(1)"},
                )
                assert js.status_code == 422

                blank = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "   ", "url": "https://pay.example.com/x"},
                )
                assert blank.status_code == 422

                long_label = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "x" * 41, "url": "https://pay.example.com/x"},
                )
                assert long_label.status_code == 422

                duplicate = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "wave", "url": "https://pay.example.com/other"},
                )
                assert duplicate.status_code == 409
                assert (
                    duplicate.json()["detail"]
                    == "A payment link with this label already exists"
                )

                orange = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={
                        "label": "Orange Money",
                        "url": "https://pay.example.com/orange",
                    },
                )
                assert orange.status_code == 201
                orange_id = orange.json()["id"]

                keep = await client.put(
                    f"/merchants/me/payment-links/{wave_id}",
                    headers=_headers(),
                    json={"label": "Wave", "url": "https://pay.example.com/wave2"},
                )
                assert keep.status_code == 200
                assert keep.json()["label"] == "Wave"
                assert keep.json()["url"] == "https://pay.example.com/wave2"

                clash = await client.put(
                    f"/merchants/me/payment-links/{wave_id}",
                    headers=_headers(),
                    json={
                        "label": "orange money",
                        "url": "https://pay.example.com/wave2",
                    },
                )
                assert clash.status_code == 409

                listed = await client.get(
                    "/merchants/me/payment-links", headers=_headers()
                )
                assert [row["label"] for row in listed.json()] == [
                    "Wave",
                    "Orange Money",
                ]

                for index in range(8):
                    extra = await client.post(
                        "/merchants/me/payment-links",
                        headers=_headers(),
                        json={
                            "label": f"App {index}",
                            "url": f"https://pay.example.com/app-{index}",
                        },
                    )
                    assert extra.status_code == 201, extra.text

                eleventh = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "Too many", "url": "https://pay.example.com/11"},
                )
                assert eleventh.status_code == 409
                assert eleventh.json()["detail"] == "At most 10 payment links are allowed"

                deleted = await client.delete(
                    f"/merchants/me/payment-links/{orange_id}",
                    headers=_headers(),
                )
                assert deleted.status_code == 204
                again = await client.delete(
                    f"/merchants/me/payment-links/{orange_id}",
                    headers=_headers(),
                )
                assert again.status_code == 404

        with _auth(other_clerk):
            async with await _client() as client:
                theirs = await client.get(
                    "/merchants/me/payment-links", headers=_headers()
                )
                assert theirs.status_code == 200
                assert theirs.json() == []
                stolen_put = await client.put(
                    f"/merchants/me/payment-links/{wave_id}",
                    headers=_headers(),
                    json={"label": "Stolen", "url": "https://pay.example.com/x"},
                )
                assert stolen_put.status_code == 404
                stolen_del = await client.delete(
                    f"/merchants/me/payment-links/{wave_id}",
                    headers=_headers(),
                )
                assert stolen_del.status_code == 404
    finally:
        await _cleanup(merchant.id, other.id)


@pytest.mark.asyncio
async def test_order_keeps_snapshot_after_link_edit_or_delete() -> None:
    clerk = f"user_paylink_snap_{uuid.uuid4()}"
    merchant, product = await _seed_shop(clerk)
    try:
        async with AsyncSessionLocal() as db:
            order = await creer_commande(
                db,
                merchant_id=merchant.id,
                customer_phone="+221770050001",
                items=[(product.id, 1)],
                payment_method=PaymentMethod.online,
                delivery_address="Sacré-Cœur, Dakar",
                ville="Dakar",
            )
        with _auth(clerk):
            async with await _client() as client:
                created = await client.post(
                    "/merchants/me/payment-links",
                    headers=_headers(),
                    json={"label": "Wave", "url": "https://pay.example.com/wave"},
                )
                link_id = created.json()["id"]
        with (
            _auth(clerk),
            patch("app.whatsapp.service.settings.whatsapp_access_token", "tok"),
            patch(
                "app.whatsapp.service.envoyer_texte_whatsapp", new_callable=AsyncMock
            ),
        ):
            async with await _client() as client:
                sent = await client.post(
                    f"/orders/{order.id}/send-payment-link",
                    headers=_headers(),
                    json={"payment_link_id": link_id},
                )
                assert sent.status_code == 200, sent.text
                assert sent.json()["payment_link"] == "https://pay.example.com/wave"
                assert sent.json()["payment_link_label"] == "Wave"
                sent_at = sent.json()["payment_link_sent_at"]

                edited = await client.put(
                    f"/merchants/me/payment-links/{link_id}",
                    headers=_headers(),
                    json={
                        "label": "Wave Pro",
                        "url": "https://pay.example.com/wave-new",
                    },
                )
                assert edited.status_code == 200
                after_edit = await client.get(
                    f"/orders/{order.id}", headers=_headers()
                )
                assert after_edit.json()["payment_link"] == "https://pay.example.com/wave"
                assert after_edit.json()["payment_link_label"] == "Wave"
                assert after_edit.json()["payment_link_sent_at"] == sent_at

                deleted = await client.delete(
                    f"/merchants/me/payment-links/{link_id}",
                    headers=_headers(),
                )
                assert deleted.status_code == 204
                after_delete = await client.get(
                    f"/orders/{order.id}", headers=_headers()
                )
                assert after_delete.json()["payment_link"] == "https://pay.example.com/wave"
                assert after_delete.json()["payment_link_label"] == "Wave"
    finally:
        await _cleanup(merchant.id)
