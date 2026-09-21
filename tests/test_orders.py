import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.notifications.models import Notification
from app.orders.models import (
    DeliveryZone,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    StockMovement,
)
from app.orders.service import (
    DeliveryNotAvailableError,
    InsufficientStockError,
    InvalidOrderStateError,
    annuler_commande,
    confirmer_livraison,
    creer_commande,
    obtenir_disponibilite,
    obtenir_zone_livraison,
    normalize_city,
)

PHONE = "+221770000000"
ADDRESS = "Sacré-Cœur, Dakar"


async def _make_merchant_product(*, stock_qty: int, name: str) -> tuple[uuid.UUID, uuid.UUID]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name)
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Article test stock",
            description="pour tests commandes",
            category="tests",
            price=Decimal("1000.00"),
            stock_qty=stock_qty,
        )
        db.add(product)
        await db.flush()
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
        return merchant.id, product.id


async def _cleanup(merchant_id: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(Notification).where(Notification.merchant_id == merchant_id)
        )
        order_ids = list(
            (
                await db.execute(select(Order.id).where(Order.merchant_id == merchant_id))
            ).scalars().all()
        )
        if order_ids:
            await db.execute(
                delete(StockMovement).where(StockMovement.order_id.in_(order_ids))
            )
            await db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            await db.execute(delete(Order).where(Order.id.in_(order_ids)))
        await db.execute(
            delete(DeliveryZone).where(DeliveryZone.merchant_id == merchant_id)
        )
        await db.execute(delete(Product).where(Product.merchant_id == merchant_id))
        await db.execute(delete(Merchant).where(Merchant.id == merchant_id))
        await db.commit()


async def _place_order(merchant_id: uuid.UUID, product_id: uuid.UUID, quantity: int = 1) -> Order:
    async with AsyncSessionLocal() as db:
        return await creer_commande(
            db,
            merchant_id=merchant_id,
            customer_phone=PHONE,
            items=[(product_id, quantity)],
            payment_method=PaymentMethod.cash_on_delivery,
            delivery_address=ADDRESS,
            ville="Dakar",
        )


@pytest.mark.asyncio
async def test_concurrent_creer_commande_on_last_unit() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=1,
        name=f"pytest-concurrent-{uuid.uuid4()}",
    )
    try:
        results = await asyncio.gather(
            _place_order(merchant_id, product_id),
            _place_order(merchant_id, product_id),
            return_exceptions=True,
        )
        successes = [item for item in results if isinstance(item, Order)]
        stock_errors = [
            item for item in results if isinstance(item, InsufficientStockError)
        ]
        others = [
            item
            for item in results
            if not isinstance(item, (Order, InsufficientStockError))
        ]
        assert others == [], f"unexpected concurrent results: {others!r}"
        assert len(successes) == 1
        assert len(stock_errors) == 1

        async with AsyncSessionLocal() as db:
            stock = await obtenir_disponibilite(db, product_id)
            order_count = (
                await db.execute(
                    select(func.count()).select_from(Order).where(
                        Order.merchant_id == merchant_id
                    )
                )
            ).scalar_one()
        assert stock == 0
        assert order_count == 1
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_creer_commande_insufficient_stock_rolls_back() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=1,
        name=f"pytest-insufficient-{uuid.uuid4()}",
    )
    try:
        with pytest.raises(InsufficientStockError):
            await _place_order(merchant_id, product_id, quantity=2)

        async with AsyncSessionLocal() as db:
            stock = await obtenir_disponibilite(db, product_id)
            order_count = (
                await db.execute(
                    select(func.count()).select_from(Order).where(
                        Order.merchant_id == merchant_id
                    )
                )
            ).scalar_one()
            movements_for_product = (
                await db.execute(
                    select(func.count()).select_from(StockMovement).where(
                        StockMovement.product_id == product_id
                    )
                )
            ).scalar_one()
        assert stock == 1
        assert order_count == 0
        assert movements_for_product == 0
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_annuler_commande_restores_stock() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=5,
        name=f"pytest-cancel-{uuid.uuid4()}",
    )
    try:
        order = await _place_order(merchant_id, product_id, quantity=2)
        async with AsyncSessionLocal() as db:
            assert await obtenir_disponibilite(db, product_id) == 3
            cancelled = await annuler_commande(db, order.id, reason="client changed mind")
            assert cancelled.status == OrderStatus.cancelled
            assert await obtenir_disponibilite(db, product_id) == 5
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_confirmer_livraison_on_cancelled_order_raises() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=2,
        name=f"pytest-confirm-cancelled-{uuid.uuid4()}",
    )
    try:
        order = await _place_order(merchant_id, product_id, quantity=1)
        async with AsyncSessionLocal() as db:
            await annuler_commande(db, order.id, reason=None)
            with pytest.raises(InvalidOrderStateError):
                await confirmer_livraison(db, order.id)
            assert await obtenir_disponibilite(db, product_id) == 2
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_creer_commande_unserved_city_creates_nothing() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=3,
        name=f"pytest-unserved-city-{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            with pytest.raises(DeliveryNotAvailableError) as raised:
                await creer_commande(
                    db,
                    merchant_id=merchant_id,
                    customer_phone=PHONE,
                    items=[(product_id, 1)],
                    payment_method=PaymentMethod.cash_on_delivery,
                    delivery_address="Marché Ocass, Touba",
                    ville="Touba",
                )
            assert raised.value.city == "Touba"
            stock = await obtenir_disponibilite(db, product_id)
            order_count = (
                await db.execute(
                    select(func.count()).select_from(Order).where(
                        Order.merchant_id == merchant_id
                    )
                )
            ).scalar_one()
            movements = (
                await db.execute(
                    select(func.count()).select_from(StockMovement).where(
                        StockMovement.product_id == product_id
                    )
                )
            ).scalar_one()
        assert stock == 3
        assert order_count == 0
        assert movements == 0
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_obtenir_zone_livraison_normalizes_city_name() -> None:
    merchant_id, _product_id = await _make_merchant_product(
        stock_qty=1,
        name=f"pytest-zone-norm-{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                DeliveryZone(
                    merchant_id=merchant_id,
                    city="Thiès",
                    city_normalized=normalize_city("Thiès"),
                    available=True,
                    min_delivery_hours=48,
                    max_delivery_hours=72,
                )
            )
            await db.commit()
            matches = []
            for query in ("Thiès", "thies", "THIÈS", "  Thiès  ", "Thies"):
                zone = await obtenir_zone_livraison(db, merchant_id, query)
                assert zone is not None, f"no match for {query!r}"
                matches.append(zone.id)
            assert len(set(matches)) == 1
            missing = await obtenir_zone_livraison(db, merchant_id, "Kaolack")
            assert missing is None
    finally:
        await _cleanup(merchant_id)
