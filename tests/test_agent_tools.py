import json
import uuid
from decimal import Decimal

from unittest.mock import patch

import pytest

from app.agent.models import Conversation, Message
from app.agent.service import escalader_vers_humain
from app.agent.tools import _stock_status, execute_tool
from app.catalogue.models import Merchant, Product
from app.catalogue.service import lister_produits_populaires
from app.core.db import AsyncSessionLocal
from app.orders.models import DeliveryZone, Order, OrderItem, StockMovement
from app.orders.service import normalize_city
from sqlalchemy import delete, select


async def _cleanup_merchant(db, merchant_id: uuid.UUID) -> None:
    conversations = list(
        (
            await db.execute(
                select(Conversation.id).where(Conversation.merchant_id == merchant_id)
            )
        ).scalars().all()
    )
    if conversations:
        await db.execute(delete(Message).where(Message.conversation_id.in_(conversations)))
        await db.execute(delete(Conversation).where(Conversation.id.in_(conversations)))
    orders = list(
        (
            await db.execute(select(Order.id).where(Order.merchant_id == merchant_id))
        ).scalars().all()
    )
    if orders:
        await db.execute(delete(StockMovement).where(StockMovement.order_id.in_(orders)))
        await db.execute(delete(OrderItem).where(OrderItem.order_id.in_(orders)))
        await db.execute(delete(Order).where(Order.id.in_(orders)))
    await db.execute(delete(DeliveryZone).where(DeliveryZone.merchant_id == merchant_id))
    await db.execute(delete(Product).where(Product.merchant_id == merchant_id))
    await db.execute(delete(Merchant).where(Merchant.id == merchant_id))
    await db.commit()


@pytest.mark.asyncio
async def test_execute_tool_returns_json_error_on_insufficient_stock() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-agent-stock-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Dernier stock",
            description="test",
            category="tests",
            price=Decimal("1000.00"),
            stock_qty=0,
        )
        conversation = Conversation(
            merchant_id=merchant.id,
            customer_phone="+221770009001",
            status="active",
        )
        db.add_all(
            [
                product,
                conversation,
                DeliveryZone(
                    merchant_id=merchant.id,
                    city="Dakar",
                    city_normalized=normalize_city("Dakar"),
                    available=True,
                    min_delivery_hours=24,
                    max_delivery_hours=48,
                ),
            ]
        )
        await db.commit()
        merchant_id = merchant.id
        try:
            raw = await execute_tool(
                db,
                tool_name="creer_commande",
                tool_args={
                    "items": [{"product_id": str(product.id), "quantity": 1}],
                    "mode_paiement": "cash_on_delivery",
                    "adresse_livraison": "Dakar",
                    "ville": "Dakar",
                },
                merchant_id=merchant_id,
                conversation_id=conversation.id,
            )
            payload = json.loads(raw)
            assert "error" in payload
            assert "Insufficient stock" in payload["error"]
        finally:
            await db.rollback()
            await _cleanup_merchant(db, merchant_id)


@pytest.mark.asyncio
async def test_execute_tool_routes_obtenir_disponibilite() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-agent-avail-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Huile test",
            description="test",
            category="cosmétiques",
            price=Decimal("4000.00"),
            stock_qty=7,
        )
        conversation = Conversation(
            merchant_id=merchant.id,
            customer_phone="+221770009002",
            status="active",
        )
        db.add_all([product, conversation])
        await db.commit()
        try:
            raw = await execute_tool(
                db,
                "obtenir_disponibilite",
                {"produit_id": str(product.id)},
                merchant.id,
                conversation.id,
            )
            payload = json.loads(raw)
            assert payload["stock_status"] == "disponible"
            assert "stock_qty" not in payload
            assert payload["product_id"] == str(product.id)
            assert '"stock_qty"' not in raw
        finally:
            await _cleanup_merchant(db, merchant.id)


@pytest.mark.asyncio
async def test_escalader_vers_humain_sets_status_and_logs_message() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-agent-esc-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        conversation = Conversation(
            merchant_id=merchant.id,
            customer_phone="+221770009003",
            status="active",
        )
        db.add(conversation)
        await db.commit()
        try:
            updated = await escalader_vers_humain(
                db, conversation.id, "le client veut un humain"
            )
            assert updated.status == "escalated"
            messages = list(
                (
                    await db.execute(
                        select(Message).where(Message.conversation_id == conversation.id)
                    )
                ).scalars().all()
            )
            assert len(messages) == 1
            assert messages[0].turn_role == "agent"
            assert "le client veut un humain" in messages[0].display_text
        finally:
            await _cleanup_merchant(db, merchant.id)


@pytest.mark.asyncio
async def test_execute_tool_unknown_name_returns_error_json() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-agent-unknown-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        conversation = Conversation(
            merchant_id=merchant.id,
            customer_phone="+221770009004",
            status="active",
        )
        db.add(conversation)
        await db.commit()
        try:
            raw = await execute_tool(
                db, "not_a_tool", {}, merchant.id, conversation.id
            )
            payload = json.loads(raw)
            assert payload == {"error": "Unknown tool: not_a_tool"}
        finally:
            await _cleanup_merchant(db, merchant.id)


def _vector(index: int) -> list[float]:
    values = [0.0] * 1024
    values[index] = 1.0
    return values


def test_stock_status_maps_qty_to_qualitative_label() -> None:
    assert _stock_status(0) == "rupture"
    assert _stock_status(1) == "stock_faible"
    assert _stock_status(3) == "stock_faible"
    assert _stock_status(4) == "disponible"
    assert _stock_status(12) == "disponible"


def _assert_no_raw_stock(serialized: str, payload: dict) -> None:
    assert '"stock_qty"' not in serialized
    blob = json.dumps(payload)
    assert "stock_qty" not in blob


@pytest.mark.asyncio
async def test_execute_tool_search_and_similar_omit_stock_qty() -> None:
    query = _vector(0)
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-agent-search-stock-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        source = Product(
            merchant_id=merchant.id,
            name="Robe rouge",
            description="soirée",
            category="vêtements femme",
            price=Decimal("25000.00"),
            stock_qty=8,
            embedding=query,
        )
        near = Product(
            merchant_id=merchant.id,
            name="Robe noire",
            description="soirée",
            category="vêtements femme",
            price=Decimal("25000.00"),
            stock_qty=2,
            embedding=[0.99] + [0.0] * 1023,
        )
        conversation = Conversation(
            merchant_id=merchant.id,
            customer_phone="+221770009005",
            status="active",
        )
        db.add_all([source, near, conversation])
        await db.commit()
        try:
            with patch(
                "app.catalogue.service.embed_query",
                return_value=query,
            ):
                search_raw = await execute_tool(
                    db,
                    "rechercher_produits",
                    {"requete": "robe", "categorie": None},
                    merchant.id,
                    conversation.id,
                )
            search = json.loads(search_raw)
            _assert_no_raw_stock(search_raw, search)
            statuses = {item["name"]: item["stock_status"] for item in search["products"]}
            assert statuses["Robe rouge"] == "disponible"
            assert statuses["Robe noire"] == "stock_faible"

            similar_raw = await execute_tool(
                db,
                "trouver_produits_similaires",
                {"produit_id": str(source.id)},
                merchant.id,
                conversation.id,
            )
            similar = json.loads(similar_raw)
            _assert_no_raw_stock(similar_raw, similar)
            assert similar["products"][0]["stock_status"] == "stock_faible"
        finally:
            await _cleanup_merchant(db, merchant.id)


@pytest.mark.asyncio
async def test_execute_tool_popular_omits_stock_qty_service_keeps_it() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-agent-pop-stock-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Karité",
            description="test",
            category="cosmétiques",
            price=Decimal("4000.00"),
            stock_qty=1,
        )
        conversation = Conversation(
            merchant_id=merchant.id,
            customer_phone="+221770009006",
            status="active",
        )
        db.add_all([product, conversation])
        await db.commit()
        try:
            service_rows = await lister_produits_populaires(db, merchant.id, limit=10)
            assert service_rows[0]["stock_qty"] == 1

            raw = await execute_tool(
                db,
                "lister_produits_populaires",
                {},
                merchant.id,
                conversation.id,
            )
            payload = json.loads(raw)
            _assert_no_raw_stock(raw, payload)
            assert payload["products"][0]["stock_status"] == "stock_faible"
            assert "stock_qty" not in payload["products"][0]
        finally:
            await _cleanup_merchant(db, merchant.id)