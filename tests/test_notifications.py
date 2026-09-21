import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, select

from app.agent.models import Conversation, Message
from app.agent.service import escalader_vers_humain
from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.main import app
from app.notifications.models import Notification
from app.notifications.service import NotificationType, lister_notifications
from app.orders.models import DeliveryZone, Order, OrderItem, PaymentMethod, StockMovement
from app.orders.service import creer_commande, normalize_city


PHONE = "+221770001100"
ADDRESS = "Sacré-Cœur, Dakar"


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
            delete(DeliveryZone).where(DeliveryZone.merchant_id.in_(merchant_ids))
        )
        await db.execute(delete(Product).where(Product.merchant_id.in_(merchant_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_shop(
    *,
    name: str,
    clerk_user_id: str | None = None,
    stock_qty: int = 10,
    price: str = "1000.00",
    product_name: str = "Article notification",
) -> tuple[Merchant, Product]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name, clerk_user_id=clerk_user_id)
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name=product_name,
            description="pour tests notifications",
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
    quantity: int = 1,
    phone: str = PHONE,
) -> Order:
    async with AsyncSessionLocal() as db:
        return await creer_commande(
            db,
            merchant_id=merchant_id,
            customer_phone=phone,
            items=[(product_id, quantity)],
            payment_method=PaymentMethod.cash_on_delivery,
            delivery_address=ADDRESS,
            ville="Dakar",
        )


def _types(rows: list[Notification]) -> list[str]:
    return [row.type for row in rows]


@pytest.mark.asyncio
async def test_creer_commande_emits_one_new_order_for_that_merchant() -> None:
    owner_clerk = f"user_notif_order_{uuid.uuid4()}"
    other_clerk = f"user_notif_order_other_{uuid.uuid4()}"
    owner, product = await _seed_shop(
        name=f"pytest-notif-order-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
        stock_qty=8,
        price="25000.00",
    )
    other, _ = await _seed_shop(
        name=f"pytest-notif-order-other-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
        stock_qty=8,
    )
    try:
        order = await _place(owner.id, product.id)
        async with AsyncSessionLocal() as db:
            owner_rows = await lister_notifications(db, owner.id)
            other_rows = await lister_notifications(db, other.id)
        assert _types(owner_rows) == [NotificationType.new_order.value]
        assert len(owner_rows) == 1
        row = owner_rows[0]
        assert row.merchant_id == owner.id
        assert row.related_type == "order"
        assert row.related_id == order.id
        assert row.read_at is None
        assert row.data["customer_phone"] == PHONE
        assert Decimal(row.data["total"]) == Decimal("25000.00")
        assert other_rows == []
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_escalader_vers_humain_emits_one_conversation_escalated() -> None:
    merchant, _ = await _seed_shop(
        name=f"pytest-notif-esc-{uuid.uuid4()}",
        stock_qty=3,
    )
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001101",
                status="active",
            )
            db.add(conversation)
            await db.commit()
            conversation_id = conversation.id

        async with AsyncSessionLocal() as db:
            await escalader_vers_humain(
                db, conversation_id, "le client veut un humain"
            )
            rows = await lister_notifications(db, merchant.id)
        assert _types(rows) == [NotificationType.conversation_escalated.value]
        assert len(rows) == 1
        row = rows[0]
        assert row.related_type == "conversation"
        assert row.related_id == conversation_id
        assert row.data == {"customer_phone": "+221770001101"}
        assert row.read_at is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_order_zeroing_stock_emits_out_of_stock_above_zero_does_not() -> None:
    merchant, product = await _seed_shop(
        name=f"pytest-notif-oos-{uuid.uuid4()}",
        stock_qty=3,
        product_name="Robe test rupture",
        price="5000.00",
    )
    try:
        staying_above = await _place(merchant.id, product.id, quantity=1)
        async with AsyncSessionLocal() as db:
            after_above = await lister_notifications(db, merchant.id)
        assert _types(after_above) == [NotificationType.new_order.value]
        assert after_above[0].related_id == staying_above.id

        zeroing = await _place(merchant.id, product.id, quantity=2)
        async with AsyncSessionLocal() as db:
            rows = await lister_notifications(db, merchant.id)
        types = _types(rows)
        assert types.count(NotificationType.new_order.value) == 2
        oos = [
            row
            for row in rows
            if row.type == NotificationType.product_out_of_stock.value
        ]
        assert len(oos) == 1
        assert oos[0].related_type == "product"
        assert oos[0].related_id == product.id
        assert oos[0].data == {"product_name": "Robe test rupture"}
        assert {row.related_id for row in rows if row.type == "new_order"} == {
            staying_above.id,
            zeroing.id,
        }
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_catalogue_stock_edit_to_zero_does_not_emit_out_of_stock() -> None:
    clerk_user_id = f"user_notif_patch_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-notif-patch-{uuid.uuid4()}",
        clerk_user_id=clerk_user_id,
        stock_qty=1,
        product_name="Foulard rupture manuelle",
    )
    try:
        with _auth(clerk_user_id):
            async with await _client() as client:
                patched = await client.patch(
                    f"/catalogue/products/{product.id}",
                    headers=_headers(),
                    json={"stock_qty": 0},
                )
        assert patched.status_code == 200, patched.text
        assert patched.json()["stock_qty"] == 0
        async with AsyncSessionLocal() as db:
            rows = await lister_notifications(db, merchant.id)
        assert rows == []
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_mark_read_and_mark_all_are_scoped_per_merchant() -> None:
    owner_clerk = f"user_notif_read_{uuid.uuid4()}"
    other_clerk = f"user_notif_read_other_{uuid.uuid4()}"
    owner, owner_product = await _seed_shop(
        name=f"pytest-notif-read-{uuid.uuid4()}",
        clerk_user_id=owner_clerk,
        stock_qty=5,
        price="1000.00",
    )
    other, other_product = await _seed_shop(
        name=f"pytest-notif-read-other-{uuid.uuid4()}",
        clerk_user_id=other_clerk,
        stock_qty=5,
        price="2000.00",
    )
    try:
        first = await _place(owner.id, owner_product.id, phone="+221770001102")
        second = await _place(owner.id, owner_product.id, phone="+221770001103")
        await _place(other.id, other_product.id, phone="+221770001104")

        with _auth(owner_clerk):
            async with await _client() as client:
                listed = await client.get("/notifications", headers=_headers())
                assert listed.status_code == 200, listed.text
                body = listed.json()
                assert [row["type"] for row in body] == ["new_order", "new_order"]
                assert all(row["read_at"] is None for row in body)
                assert {row["related_id"] for row in body} == {
                    str(first.id),
                    str(second.id),
                }
                newest_id = body[0]["id"]
                older_id = body[1]["id"]

                marked = await client.post(
                    f"/notifications/{newest_id}/read",
                    headers=_headers(),
                )
                assert marked.status_code == 200, marked.text
                assert marked.json()["id"] == newest_id
                assert marked.json()["read_at"] is not None

                after_one = await client.get("/notifications", headers=_headers())
                by_id = {row["id"]: row for row in after_one.json()}
                assert by_id[newest_id]["read_at"] is not None
                assert by_id[older_id]["read_at"] is None

                all_read = await client.post(
                    "/notifications/read-all", headers=_headers()
                )
                assert all_read.status_code == 200, all_read.text
                assert all_read.json()["updated"] == 1

                after_all = await client.get("/notifications", headers=_headers())
                assert all(row["read_at"] is not None for row in after_all.json())

        with _auth(other_clerk):
            async with await _client() as client:
                foreign = await client.post(
                    f"/notifications/{newest_id}/read",
                    headers=_headers(),
                )
                assert foreign.status_code == 404
                other_list = await client.get("/notifications", headers=_headers())
                assert other_list.status_code == 200
                other_body = other_list.json()
                assert len(other_body) == 1
                assert other_body[0]["read_at"] is None
                assert other_body[0]["type"] == "new_order"
    finally:
        await _cleanup(owner.id, other.id)
