import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.notifications.models import Notification
from app.agent.models import Conversation, Message
from app.agent.service import STATUS_ACTIVE, STATUS_CLOSED, STATUS_ESCALATED
from app.orders.models import (
    DeliveryZone,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    StockMovement,
)
from app.orders.service import (
    DeliveryNotAvailableError,
    InsufficientStockError,
    InvalidOrderStateError,
    annuler_commande,
    confirmer_livraison,
    consulter_commande,
    creer_commande,
    marquer_commande_payee,
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
        conversation_ids = list(
            (
                await db.execute(
                    select(Conversation.id).where(
                        Conversation.merchant_id == merchant_id
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
            delete(DeliveryZone).where(DeliveryZone.merchant_id == merchant_id)
        )
        await db.execute(delete(Product).where(Product.merchant_id == merchant_id))
        await db.execute(delete(Merchant).where(Merchant.id == merchant_id))
        await db.commit()


async def _place_order(
    merchant_id: uuid.UUID,
    product_id: uuid.UUID,
    quantity: int = 1,
    payment_method: PaymentMethod = PaymentMethod.cash_on_delivery,
) -> Order:
    async with AsyncSessionLocal() as db:
        return await creer_commande(
            db,
            merchant_id=merchant_id,
            customer_phone=PHONE,
            items=[(product_id, quantity)],
            payment_method=payment_method,
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
            assert cancelled.payment_status == PaymentStatus.pending
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
async def test_confirmer_livraison_marks_cod_paid() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=2,
        name=f"pytest-cod-paid-{uuid.uuid4()}",
    )
    try:
        order = await _place_order(merchant_id, product_id)
        assert order.payment_method == PaymentMethod.cash_on_delivery
        assert order.payment_status == PaymentStatus.pending
        async with AsyncSessionLocal() as db:
            delivered = await confirmer_livraison(db, order.id)
            assert delivered.status == OrderStatus.delivered
            assert delivered.payment_status == PaymentStatus.paid
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_confirmer_livraison_leaves_online_pending() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=2,
        name=f"pytest-online-pending-{uuid.uuid4()}",
    )
    try:
        order = await _place_order(
            merchant_id, product_id, payment_method=PaymentMethod.online
        )
        assert order.payment_status == PaymentStatus.pending
        async with AsyncSessionLocal() as db:
            delivered = await confirmer_livraison(db, order.id)
            assert delivered.status == OrderStatus.delivered
            assert delivered.payment_status == PaymentStatus.pending
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_annuler_commande_does_not_change_payment_status() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=4,
        name=f"pytest-cancel-pay-{uuid.uuid4()}",
    )
    try:
        pending = await _place_order(
            merchant_id, product_id, payment_method=PaymentMethod.online
        )
        already_paid = await _place_order(
            merchant_id, product_id, payment_method=PaymentMethod.online
        )
        async with AsyncSessionLocal() as db:
            marked = await marquer_commande_payee(db, merchant_id, already_paid.id)
            assert marked.payment_status == PaymentStatus.paid
            cancelled_pending = await annuler_commande(db, pending.id, reason=None)
            cancelled_paid = await annuler_commande(db, already_paid.id, reason=None)
            assert cancelled_pending.status == OrderStatus.cancelled
            assert cancelled_pending.payment_status == PaymentStatus.pending
            assert cancelled_paid.status == OrderStatus.cancelled
            assert cancelled_paid.payment_status == PaymentStatus.paid
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


@pytest.mark.asyncio
async def test_creer_commande_assigns_sequential_order_numbers() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=5,
        name=f"pytest-order-numbers-{uuid.uuid4()}",
    )
    try:
        first = await _place_order(merchant_id, product_id)
        second = await _place_order(merchant_id, product_id)
        assert first.order_number == 1
        assert second.order_number == 2
        async with AsyncSessionLocal() as db:
            merchant = await db.get(Merchant, merchant_id)
            assert merchant is not None
            assert merchant.next_order_number == 3
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_concurrent_creer_commande_gets_distinct_order_numbers() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=5,
        name=f"pytest-order-num-concurrent-{uuid.uuid4()}",
    )
    try:
        results = await asyncio.gather(
            _place_order(merchant_id, product_id),
            _place_order(merchant_id, product_id),
            return_exceptions=True,
        )
        others = [item for item in results if not isinstance(item, Order)]
        assert others == [], f"unexpected concurrent results: {others!r}"
        numbers = sorted(order.order_number for order in results)
        assert numbers == [1, 2]
        assert results[0].id != results[1].id
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_consulter_commande_is_scoped_to_customer_phone() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=5,
        name=f"pytest-consulter-{uuid.uuid4()}",
    )
    other_phone = "+221770000099"
    try:
        owner_order = await _place_order(merchant_id, product_id)
        async with AsyncSessionLocal() as db:
            other_order = await creer_commande(
                db,
                merchant_id=merchant_id,
                customer_phone=other_phone,
                items=[(product_id, 1)],
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address=ADDRESS,
                ville="Dakar",
            )
        async with AsyncSessionLocal() as db:
            stolen = await consulter_commande(
                db, merchant_id, other_phone, owner_order.order_number
            )
            assert stolen is None
            own = await consulter_commande(
                db, merchant_id, PHONE, owner_order.order_number
            )
            assert own is not None
            assert own.id == owner_order.id
            missing = await consulter_commande(db, merchant_id, PHONE, 999999)
            assert missing is None
            latest_other = await consulter_commande(
                db, merchant_id, other_phone, None
            )
            assert latest_other is not None
            assert latest_other.id == other_order.id
    finally:
        await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_confirmer_livraison_closes_active_conversation_only() -> None:
    merchant_id, product_id = await _make_merchant_product(
        stock_qty=5,
        name=f"pytest-close-on-delivery-{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            linked = Conversation(
                merchant_id=merchant_id,
                customer_phone=PHONE,
                status=STATUS_ACTIVE,
            )
            escalated = Conversation(
                merchant_id=merchant_id,
                customer_phone="+221770000001",
                status=STATUS_ESCALATED,
            )
            already_closed = Conversation(
                merchant_id=merchant_id,
                customer_phone="+221770000002",
                status=STATUS_CLOSED,
            )
            db.add_all([linked, escalated, already_closed])
            await db.commit()
            await db.refresh(linked)
            await db.refresh(escalated)
            await db.refresh(already_closed)
            linked_id = linked.id
            escalated_id = escalated.id
            closed_id = already_closed.id

        async with AsyncSessionLocal() as db:
            from_confirm = await creer_commande(
                db,
                merchant_id=merchant_id,
                customer_phone=PHONE,
                items=[(product_id, 1)],
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address=ADDRESS,
                ville="Dakar",
                conversation_id=linked_id,
            )
            still_active = await db.get(Conversation, linked_id)
            assert still_active is not None
            assert still_active.status == STATUS_ACTIVE

        async with AsyncSessionLocal() as db:
            delivered = await confirmer_livraison(db, from_confirm.id)
            assert delivered.status == OrderStatus.delivered
            closed = await db.get(Conversation, linked_id)
            assert closed is not None
            assert closed.status == STATUS_CLOSED

        async with AsyncSessionLocal() as db:
            other_escalated = await creer_commande(
                db,
                merchant_id=merchant_id,
                customer_phone="+221770000001",
                items=[(product_id, 1)],
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address=ADDRESS,
                ville="Dakar",
                conversation_id=escalated_id,
            )
            other_closed = await creer_commande(
                db,
                merchant_id=merchant_id,
                customer_phone="+221770000002",
                items=[(product_id, 1)],
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address=ADDRESS,
                ville="Dakar",
                conversation_id=closed_id,
            )
            no_thread = await creer_commande(
                db,
                merchant_id=merchant_id,
                customer_phone="+221770000003",
                items=[(product_id, 1)],
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address=ADDRESS,
                ville="Dakar",
            )

        async with AsyncSessionLocal() as db:
            await confirmer_livraison(db, other_escalated.id)
            await confirmer_livraison(db, other_closed.id)
            await confirmer_livraison(db, no_thread.id)
            assert (await db.get(Conversation, escalated_id)).status == STATUS_ESCALATED
            assert (await db.get(Conversation, closed_id)).status == STATUS_CLOSED
    finally:
        await _cleanup(merchant_id)

