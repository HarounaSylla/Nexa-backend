import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, select

from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.main import app
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
        await db.execute(
            delete(Deliverer).where(Deliverer.merchant_id.in_(merchant_ids))
        )
        await db.execute(
            delete(DeliveryZone).where(DeliveryZone.merchant_id.in_(merchant_ids))
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
) -> tuple[Merchant, Product]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name, clerk_user_id=clerk_user_id)
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

                async with AsyncSessionLocal() as db:
                    stock_before_cancel = await obtenir_disponibilite(db, product.id)

                cancelled = await client.post(
                    f"/orders/{to_cancel.id}/cancel",
                    headers=_headers(),
                    json={"reason": "test cancel"},
                )
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["status"] == OrderStatus.cancelled.value

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


@pytest.mark.asyncio
async def test_payment_link_rejected_on_cod_accepted_on_online() -> None:
    clerk_user_id = f"user_ord_pay_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-ord-pay-{uuid.uuid4()}",
        clerk_user_id=clerk_user_id,
        stock_qty=5,
    )
    try:
        cod = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.cash_on_delivery
        )
        online = await _place(
            merchant.id, product.id, payment_method=PaymentMethod.online
        )
        link = "https://pay.example.com/nexa-test"

        with _auth(clerk_user_id):
            async with await _client() as client:
                rejected = await client.patch(
                    f"/orders/{cod.id}/payment-link",
                    headers=_headers(),
                    json={"payment_link": link},
                )
                assert rejected.status_code == 409, rejected.text
                assert "online-payment" in rejected.json()["detail"]

                accepted = await client.patch(
                    f"/orders/{online.id}/payment-link",
                    headers=_headers(),
                    json={"payment_link": link},
                )
                assert accepted.status_code == 200, accepted.text
                assert accepted.json()["payment_link"] == link
                assert accepted.json()["payment_status"] == PaymentStatus.pending.value
                assert accepted.json()["payment_method"] == PaymentMethod.online.value
    finally:
        await _cleanup(merchant.id)


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
