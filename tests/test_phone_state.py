"""Canonical phones, escalated inbound, and related dashboard behaviour."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from app.agent.models import Conversation, Message
from app.agent.orchestrator import _get_or_create_conversation, traiter_message_entrant
from app.agent.service import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_ESCALATED,
    reprendre_par_agent,
)
from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.core.phone import (
    InvalidPhoneNumberError,
    normalize_phone,
    normalize_phone_webhook,
)
from app.main import app
from app.notifications.models import Notification
from app.notifications.service import marquer_comme_lue
from app.orders.models import (
    DeliveryZone,
    Order,
    OrderItem,
    PaymentMethod,
    StockMovement,
)
from app.orders.service import consulter_commande, creer_commande, normalize_city
from app.whatsapp.service import extract_image_messages, extract_text_messages


ADDRESS = "Sacré-Cœur, Dakar"


class _FakeGraph:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, initial, config=None):
        self.calls += 1
        return {
            **initial,
            "new_items": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Réponse test",
                }
            ],
            "output_text": "Réponse test",
        }


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
        await db.execute(delete(Product).where(Product.merchant_id.in_(merchant_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_shop(phone_suffix: str = "") -> tuple[Merchant, Product]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=f"pytest-phone-{uuid.uuid4()}",
            clerk_user_id=f"user_phone_{uuid.uuid4()}",
            whatsapp_phone_number_id=f"pnid-phone-{uuid.uuid4()}{phone_suffix}",
        )
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Article phone",
            description="tests phone",
            category="tests",
            price=Decimal("4000.00"),
            stock_qty=20,
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


def _load_0017():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0017_normalize_phones.py"
    )
    spec = importlib.util.spec_from_file_location("rev_0017_normalize_phones", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+221771234567", "+221771234567"),
        ("221771234567", "+221771234567"),
        ("00 221 77 123 45 67", "+221771234567"),
        ("+221 77-123.45 67", "+221771234567"),
        ("(221) 771234567", "+221771234567"),
        ("771234567", "+221771234567"),
    ],
)
def test_normalize_phone_accepted_forms(raw: str, expected: str) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "1234567", "1" * 16])
def test_normalize_phone_rejects_invalid(raw: str) -> None:
    with pytest.raises(InvalidPhoneNumberError, match="Invalid phone number"):
        normalize_phone(raw)


def test_normalize_phone_webhook_falls_back() -> None:
    assert normalize_phone_webhook("abc") == "+"
    assert normalize_phone_webhook("12") == "+12"
    assert normalize_phone_webhook("221771234567") == "+221771234567"


def test_webhook_extract_normalises_and_falls_back() -> None:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "pn"},
                            "messages": [
                                {
                                    "from": "221770001400",
                                    "id": "wamid.ok",
                                    "type": "text",
                                    "text": {"body": "salut"},
                                },
                                {
                                    "from": "abc",
                                    "id": "wamid.fallback",
                                    "type": "text",
                                    "text": {"body": "salut"},
                                },
                                {
                                    "from": "221770001400",
                                    "id": "wamid.img",
                                    "type": "image",
                                    "image": {"id": "media-1", "mime_type": "image/jpeg"},
                                },
                            ],
                        }
                    }
                ]
            }
        ],
    }
    texts = extract_text_messages(payload)
    by_id = {row["message_id"]: row["customer_phone"] for row in texts}
    assert by_id["wamid.ok"] == "+221770001400"
    assert by_id["wamid.fallback"] == "+"
    images = extract_image_messages(payload)
    assert images[0]["customer_phone"] == "+221770001400"


@pytest.mark.asyncio
async def test_escalated_inbound_stores_silences_and_notifies_once() -> None:
    merchant, _product = await _seed_shop()
    other, _other_product = await _seed_shop("-other")
    phone = "+221770060001"
    other_phone = "+221770060002"
    graph = _FakeGraph()
    try:
        async with AsyncSessionLocal() as db:
            escalated = Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status=STATUS_ESCALATED,
            )
            other_same_phone = Conversation(
                merchant_id=other.id,
                customer_phone=phone,
                status=STATUS_ESCALATED,
            )
            other_phone_row = Conversation(
                merchant_id=merchant.id,
                customer_phone=other_phone,
                status=STATUS_ESCALATED,
            )
            db.add_all([escalated, other_same_phone, other_phone_row])
            await db.commit()
            await db.refresh(escalated)
            await db.refresh(other_same_phone)
            await db.refresh(other_phone_row)
            escalated_id = escalated.id
            other_merchant_id = other_same_phone.id
            other_phone_id = other_phone_row.id
            other_updated = other_same_phone.updated_at
            other_phone_updated = other_phone_row.updated_at

        with patch("app.agent.orchestrator._build_graph", return_value=graph):
            async with AsyncSessionLocal() as db:
                first = await traiter_message_entrant(
                    db, merchant.id, "221770060001", "premier message"
                )
                second = await traiter_message_entrant(
                    db, merchant.id, phone, "deuxième message"
                )
        assert first is None
        assert second is None
        assert graph.calls == 0

        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(Conversation).where(
                            Conversation.merchant_id == merchant.id,
                            Conversation.customer_phone == phone,
                        )
                    )
                ).scalars().all()
            )
            assert len(rows) == 1
            assert rows[0].id == escalated_id
            assert rows[0].status == STATUS_ESCALATED
            messages = list(
                (
                    await db.execute(
                        select(Message)
                        .where(Message.conversation_id == escalated_id)
                        .order_by(Message.created_at)
                    )
                ).scalars().all()
            )
            assert [row.display_text for row in messages] == [
                "premier message",
                "deuxième message",
            ]
            assert all(row.turn_role == "customer" for row in messages)
            assert all(row.items == [] for row in messages)
            notes = list(
                (
                    await db.execute(
                        select(Notification).where(
                            Notification.merchant_id == merchant.id,
                            Notification.type == "escalated_customer_message",
                        )
                    )
                ).scalars().all()
            )
            assert len(notes) == 1
            assert notes[0].related_id == escalated_id
            assert notes[0].read_at is None
            assert notes[0].data == {"customer_phone": phone}
            untouched_merchant = await db.get(Conversation, other_merchant_id)
            untouched_phone = await db.get(Conversation, other_phone_id)
            assert untouched_merchant is not None
            assert untouched_phone is not None
            assert untouched_merchant.updated_at == other_updated
            assert untouched_phone.updated_at == other_phone_updated
            note_id = notes[0].id

        async with AsyncSessionLocal() as db:
            await marquer_comme_lue(db, merchant.id, note_id)

        with patch("app.agent.orchestrator._build_graph", return_value=graph):
            async with AsyncSessionLocal() as db:
                third = await traiter_message_entrant(
                    db, merchant.id, phone, "après lecture"
                )
        assert third is None
        assert graph.calls == 0

        async with AsyncSessionLocal() as db:
            notes = list(
                (
                    await db.execute(
                        select(Notification).where(
                            Notification.merchant_id == merchant.id,
                            Notification.type == "escalated_customer_message",
                        )
                    )
                ).scalars().all()
            )
            assert len(notes) == 2
            unread = [row for row in notes if row.read_at is None]
            assert len(unread) == 1

        async with AsyncSessionLocal() as db:
            returned = await reprendre_par_agent(db, merchant.id, escalated_id)
            assert returned.status == STATUS_ACTIVE

        with patch("app.agent.orchestrator._build_graph", return_value=graph):
            async with AsyncSessionLocal() as db:
                reply = await traiter_message_entrant(
                    db, merchant.id, phone, "retour agent"
                )
        assert reply == "Réponse test"
        assert graph.calls == 1
    finally:
        await _cleanup(merchant.id, other.id)


@pytest.mark.asyncio
async def test_bare_and_plus_phone_share_one_conversation() -> None:
    merchant, _product = await _seed_shop()
    try:
        async with AsyncSessionLocal() as db:
            first = await _get_or_create_conversation(
                db, merchant.id, "221770060010"
            )
            second = await _get_or_create_conversation(
                db, merchant.id, "+221770060010"
            )
            await db.commit()
            assert first.id == second.id
            assert first.customer_phone == "+221770060010"
            assert second.customer_phone == "+221770060010"
            rows = list(
                (
                    await db.execute(
                        select(Conversation).where(
                            Conversation.merchant_id == merchant.id
                        )
                    )
                ).scalars().all()
            )
            assert len(rows) == 1
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_dashboard_order_normalises_phone_and_rejects_invalid() -> None:
    merchant, product = await _seed_shop()
    clerk = merchant.clerk_user_id or ""
    payload = {
        "customer_phone": "221770060020",
        "items": [{"product_id": str(product.id), "quantity": 1}],
        "payment_method": PaymentMethod.cash_on_delivery.value,
        "delivery_address": ADDRESS,
        "ville": "Dakar",
    }
    try:
        with _auth(clerk):
            async with await _client() as client:
                created = await client.post(
                    "/orders", headers=_headers(), json=payload
                )
                assert created.status_code == 200, created.text
                assert created.json()["customer_phone"] == "+221770060020"
                invalid = await client.post(
                    "/orders",
                    headers=_headers(),
                    json={**payload, "customer_phone": "abc"},
                )
                assert invalid.status_code == 422
                assert "Invalid phone number" in invalid.text
        async with AsyncSessionLocal() as db:
            stored = (
                await db.execute(
                    select(Order).where(Order.merchant_id == merchant.id)
                )
            ).scalar_one()
            assert stored.customer_phone == "+221770060020"
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_simulate_empty_reply_and_invalid_phone() -> None:
    merchant, _product = await _seed_shop()
    phone = "+221770060030"
    graph = _FakeGraph()
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Conversation(
                    merchant_id=merchant.id,
                    customer_phone=phone,
                    status=STATUS_ESCALATED,
                )
            )
            await db.commit()
        with patch("app.agent.orchestrator._build_graph", return_value=graph):
            async with await _client() as client:
                response = await client.post(
                    "/agent/simulate",
                    json={
                        "merchant_id": str(merchant.id),
                        "customer_phone": "221770060030",
                        "message": "toujours là",
                    },
                )
                bad = await client.post(
                    "/agent/simulate",
                    json={
                        "merchant_id": str(merchant.id),
                        "customer_phone": "abc",
                        "message": "x",
                    },
                )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["reply"] == ""
        assert graph.calls == 0
        assert bad.status_code == 422
        assert "Invalid phone number" in bad.text
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_consulter_commande_finds_legacy_unnormalised_row() -> None:
    merchant, product = await _seed_shop()
    try:
        async with AsyncSessionLocal() as db:
            order = await creer_commande(
                db,
                merchant_id=merchant.id,
                customer_phone="+221770060040",
                items=[(product.id, 1)],
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address=ADDRESS,
                ville="Dakar",
            )
        async with AsyncSessionLocal() as db:
            row = await db.get(Order, order.id)
            assert row is not None
            row.customer_phone = "221770060040"
            await db.commit()
            number = row.order_number
        async with AsyncSessionLocal() as db:
            found_plus = await consulter_commande(
                db, merchant.id, "+221770060040", number
            )
            found_bare = await consulter_commande(
                db, merchant.id, "221770060040", number
            )
            assert found_plus is not None and found_plus.id == order.id
            assert found_bare is not None and found_bare.id == order.id
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_migration_0017_normalises_sample_legacy_rows() -> None:
    merchant, _product = await _seed_shop()
    module = _load_0017()
    try:
        async with AsyncSessionLocal() as db:
            rows = [
                Conversation(
                    merchant_id=merchant.id,
                    customer_phone="221770060050",
                    status=STATUS_ACTIVE,
                ),
                Conversation(
                    merchant_id=merchant.id,
                    customer_phone="00 221 770060051",
                    status=STATUS_ACTIVE,
                ),
                Conversation(
                    merchant_id=merchant.id,
                    customer_phone="770060052",
                    status=STATUS_ACTIVE,
                ),
                Conversation(
                    merchant_id=merchant.id,
                    customer_phone="not-a-phone",
                    status=STATUS_ACTIVE,
                ),
                Conversation(
                    merchant_id=merchant.id,
                    customer_phone="+221770060053",
                    status=STATUS_ACTIVE,
                ),
            ]
            db.add_all(rows)
            db.add(
                Order(
                    merchant_id=merchant.id,
                    order_number=1,
                    customer_phone="221770060054",
                    payment_method=PaymentMethod.cash_on_delivery,
                    delivery_address=ADDRESS,
                    city="Dakar",
                )
            )
            db.add(
                Order(
                    merchant_id=merchant.id,
                    order_number=2,
                    customer_phone="xx",
                    payment_method=PaymentMethod.cash_on_delivery,
                    delivery_address=ADDRESS,
                    city="Dakar",
                )
            )
            await db.commit()
            ids = {row.customer_phone: row.id for row in rows}
            await db.execute(text(module.normalize_phones_sql("conversations")))
            await db.execute(text(module.normalize_phones_sql("orders")))
            await db.commit()
            db.expire_all()
            assert (
                await db.get(Conversation, ids["221770060050"])
            ).customer_phone == "+221770060050"
            assert (
                await db.get(Conversation, ids["00 221 770060051"])
            ).customer_phone == "+221770060051"
            assert (
                await db.get(Conversation, ids["770060052"])
            ).customer_phone == "+221770060052"
            assert (
                await db.get(Conversation, ids["not-a-phone"])
            ).customer_phone == "not-a-phone"
            assert (
                await db.get(Conversation, ids["+221770060053"])
            ).customer_phone == "+221770060053"
            orders = list(
                (
                    await db.execute(
                        select(Order).where(Order.merchant_id == merchant.id)
                    )
                ).scalars().all()
            )
            by_number = {row.order_number: row.customer_phone for row in orders}
            assert by_number[1] == "+221770060054"
            assert by_number[2] == "xx"
    finally:
        await _cleanup(merchant.id)


def _load_0018():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0018_unique_open_conversation.py"
    )
    spec = importlib.util.spec_from_file_location("rev_0018_unique_open", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_concurrent_get_or_create_yields_one_open_conversation() -> None:
    merchant, _product = await _seed_shop()
    phone = "+221770060070"

    async def _create() -> uuid.UUID:
        async with AsyncSessionLocal() as db:
            conversation = await _get_or_create_conversation(
                db, merchant.id, phone
            )
            await db.commit()
            return conversation.id

    try:
        first, second = await asyncio.gather(_create(), _create())
        async with AsyncSessionLocal() as db:
            open_rows = list(
                (
                    await db.execute(
                        select(Conversation).where(
                            Conversation.merchant_id == merchant.id,
                            Conversation.customer_phone == phone,
                            Conversation.status.in_([STATUS_ACTIVE, STATUS_ESCALATED]),
                        )
                    )
                ).scalars().all()
            )
        assert len(open_rows) == 1
        assert first == second == open_rows[0].id
        assert open_rows[0].status == STATUS_ACTIVE
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_closed_and_new_active_are_both_kept() -> None:
    merchant, _product = await _seed_shop()
    phone = "+221770060071"
    try:
        async with AsyncSessionLocal() as db:
            closed = Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status=STATUS_CLOSED,
            )
            db.add(closed)
            await db.flush()
            closed.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
            await db.commit()
            closed_id = closed.id
        async with AsyncSessionLocal() as db:
            active = await _get_or_create_conversation(db, merchant.id, phone)
            await db.commit()
            leftover = await db.get(Conversation, closed_id)
            assert leftover is not None
            assert leftover.status == STATUS_CLOSED
            assert active.id != closed_id
            assert active.status == STATUS_ACTIVE
        async with AsyncSessionLocal() as db:
            with pytest.raises(IntegrityError):
                db.add(
                    Conversation(
                        merchant_id=merchant.id,
                        customer_phone=phone,
                        status=STATUS_ACTIVE,
                    )
                )
                await db.commit()
            await db.rollback()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_migration_0018_closes_duplicate_opens_keeps_escalated() -> None:
    merchant, _product = await _seed_shop()
    module = _load_0018()
    phone = "+221770060072"
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text(f"DROP INDEX IF EXISTS {module.INDEX_NAME}"))
            await db.commit()
            now = datetime.now(timezone.utc)
            escalated = Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status=STATUS_ESCALATED,
            )
            older_active = Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status=STATUS_ACTIVE,
            )
            newer_active = Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status=STATUS_ACTIVE,
            )
            db.add_all([escalated, older_active, newer_active])
            await db.flush()
            older_active.updated_at = now - timedelta(hours=2)
            newer_active.updated_at = now - timedelta(hours=1)
            await db.commit()
            escalated_id = escalated.id
            older_id = older_active.id
            newer_id = newer_active.id
            await db.execute(text(module.CLOSE_DUPLICATE_OPENS_SQL))
            await db.commit()
            db.expire_all()
            assert (await db.get(Conversation, escalated_id)).status == STATUS_ESCALATED
            assert (await db.get(Conversation, older_id)).status == STATUS_CLOSED
            assert (await db.get(Conversation, newer_id)).status == STATUS_CLOSED
            await db.execute(
                text(
                    f"CREATE UNIQUE INDEX {module.INDEX_NAME} "
                    "ON conversations (merchant_id, customer_phone) "
                    "WHERE status IN ('active', 'escalated')"
                )
            )
            await db.commit()
    finally:
        await _cleanup(merchant.id)
