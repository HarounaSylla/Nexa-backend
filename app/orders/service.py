import uuid
from collections import defaultdict
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.catalogue.models import Product
from app.orders.models import (
    Deliverer,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    StockMovement,
)

MOVEMENT_PROVISIONAL_DECREMENT = "order_provisional_decrement"
MOVEMENT_CANCELLED_RESTOCK = "order_cancelled_restock"
MOVEMENT_DELIVERED_FINALIZE = "order_delivered_finalize"


class NotFoundError(LookupError):
    """Raised when a product, order, or deliverer does not exist."""


class InsufficientStockError(Exception):
    def __init__(self, product_id: uuid.UUID, requested: int, available: int) -> None:
        super().__init__(
            f"Insufficient stock for product {product_id}: "
            f"requested {requested}, available {available}"
        )
        self.product_id = product_id
        self.requested = requested
        self.available = available


class InvalidOrderStateError(ValueError):
    def __init__(self, order_id: uuid.UUID, status: OrderStatus, action: str) -> None:
        super().__init__(
            f"Cannot {action} order {order_id} in status {status.value}"
        )
        self.order_id = order_id
        self.status = status
        self.action = action


def _aggregate_quantities(items: list[tuple[uuid.UUID, int]]) -> dict[uuid.UUID, int]:
    if not items:
        raise ValueError("An order must contain at least one item")
    quantities: dict[uuid.UUID, int] = defaultdict(int)
    for product_id, quantity in items:
        if quantity <= 0:
            raise ValueError(
                f"Quantity must be > 0, got {quantity} for product {product_id}"
            )
        quantities[product_id] += quantity
    return dict(quantities)


async def _lock_products(
    db: AsyncSession, product_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Product]:
    result = await db.execute(
        select(Product)
        .where(Product.id.in_(product_ids))
        .order_by(Product.id)
        .with_for_update()
    )
    locked = {product.id: product for product in result.scalars().all()}
    missing = [product_id for product_id in product_ids if product_id not in locked]
    if missing:
        raise NotFoundError(f"Product {missing[0]} was not found")
    return locked


async def _get_order_for_update(db: AsyncSession, order_id: uuid.UUID) -> Order:
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.id == order_id)
        .with_for_update()
    )
    order = result.scalar_one_or_none()
    if order is None:
        raise NotFoundError(f"Order {order_id} was not found")
    return order


async def obtenir_disponibilite(db: AsyncSession, product_id: uuid.UUID) -> int:
    """Return the current products.stock_qty for a product.

    Raise a clear NotFoundError if the product doesn't exist.
    Read-only, no lock needed.
    """
    product = await db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} was not found")
    return product.stock_qty


async def creer_commande(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    items: list[tuple[uuid.UUID, int]],
    payment_method: PaymentMethod,
    delivery_address: str,
) -> Order:
    """Create an order and provisionally decrement stock, atomically.

    Locks every involved product with SELECT ... FOR UPDATE in product_id
    order, rejects the whole order if any item is short, then decrements
    stock, writes stock_movements, and inserts the order + items in the
    same transaction.
    """
    quantities = _aggregate_quantities(items)
    product_ids = sorted(quantities)
    try:
        products = await _lock_products(db, product_ids)
        for product_id in product_ids:
            product = products[product_id]
            requested = quantities[product_id]
            if product.stock_qty < requested:
                raise InsufficientStockError(
                    product_id, requested, product.stock_qty
                )
            if product.price is None:
                raise ValueError(
                    f"Product {product_id} has no price; cannot snapshot unit_price"
                )

        order = Order(
            merchant_id=merchant_id,
            customer_phone=customer_phone,
            status=OrderStatus.created,
            payment_method=payment_method,
            payment_status=PaymentStatus.pending,
            delivery_address=delivery_address,
        )
        db.add(order)
        await db.flush()

        for product_id in product_ids:
            product = products[product_id]
            requested = quantities[product_id]
            product.stock_qty -= requested
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=product_id,
                    quantity=requested,
                    unit_price=Decimal(product.price),
                )
            )
            db.add(
                StockMovement(
                    product_id=product_id,
                    order_id=order.id,
                    movement_type=MOVEMENT_PROVISIONAL_DECREMENT,
                    quantity_delta=-requested,
                )
            )

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()


async def assigner_livreur(
    db: AsyncSession, order_id: uuid.UUID, deliverer_id: uuid.UUID
) -> Order:
    """Set deliverer_id and status=deliverer_assigned."""
    try:
        order = await _get_order_for_update(db, order_id)
        if order.status in {OrderStatus.delivered, OrderStatus.cancelled}:
            raise InvalidOrderStateError(order.id, order.status, "assign a deliverer to")
        deliverer = await db.get(Deliverer, deliverer_id)
        if deliverer is None:
            raise NotFoundError(f"Deliverer {deliverer_id} was not found")
        order.deliverer_id = deliverer_id
        order.status = OrderStatus.deliverer_assigned
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()


async def confirmer_livraison(db: AsyncSession, order_id: uuid.UUID) -> Order:
    """Set status=delivered and write quantity_delta=0 finalize markers."""
    try:
        order = await _get_order_for_update(db, order_id)
        if order.status == OrderStatus.cancelled:
            raise InvalidOrderStateError(order.id, order.status, "confirm delivery of")
        if order.status == OrderStatus.delivered:
            raise InvalidOrderStateError(order.id, order.status, "confirm delivery of")
        order.status = OrderStatus.delivered
        for item in order.items:
            db.add(
                StockMovement(
                    product_id=item.product_id,
                    order_id=order.id,
                    movement_type=MOVEMENT_DELIVERED_FINALIZE,
                    quantity_delta=0,
                )
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()


async def annuler_commande(
    db: AsyncSession, order_id: uuid.UUID, reason: str | None
) -> Order:
    """Set status=cancelled and restock every item atomically."""
    del reason  # accepted by the API; no persistable reason column yet
    try:
        order = await _get_order_for_update(db, order_id)
        if order.status == OrderStatus.delivered:
            raise InvalidOrderStateError(order.id, order.status, "cancel")
        if order.status == OrderStatus.cancelled:
            raise InvalidOrderStateError(order.id, order.status, "cancel")

        product_ids = sorted({item.product_id for item in order.items})
        products = await _lock_products(db, product_ids)
        for item in order.items:
            products[item.product_id].stock_qty += item.quantity
            db.add(
                StockMovement(
                    product_id=item.product_id,
                    order_id=order.id,
                    movement_type=MOVEMENT_CANCELLED_RESTOCK,
                    quantity_delta=item.quantity,
                )
            )
        order.status = OrderStatus.cancelled
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()
