import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.orders.models import Order, OrderStatus, PaymentMethod, PaymentStatus
from app.orders.service import (
    InsufficientStockError,
    InvalidOrderStateError,
    NotFoundError,
    annuler_commande,
    assigner_livreur,
    confirmer_livraison,
    creer_commande,
    obtenir_disponibilite,
)

# Temporary scaffolding to exercise stock/order flows by hand before the
# WhatsApp agent exists (Jalon 3). Not the final API surface; no auth.
router = APIRouter(prefix="/orders", tags=["orders"])


class OrderItemIn(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0)


class CreateOrderRequest(BaseModel):
    merchant_id: uuid.UUID
    customer_phone: str
    items: list[OrderItemIn]
    payment_method: PaymentMethod
    delivery_address: str


class AssignDelivererRequest(BaseModel):
    deliverer_id: uuid.UUID


class CancelOrderRequest(BaseModel):
    reason: str | None = None


class OrderItemOut(BaseModel):
    id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    unit_price: Decimal


class OrderOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    customer_phone: str
    status: OrderStatus
    payment_method: PaymentMethod
    payment_status: PaymentStatus
    payment_link: str | None
    delivery_address: str
    deliverer_id: uuid.UUID | None
    items: list[OrderItemOut]


class AvailabilityOut(BaseModel):
    product_id: uuid.UUID
    stock_qty: int


def _to_order_out(order: Order) -> OrderOut:
    return OrderOut(
        id=order.id,
        merchant_id=order.merchant_id,
        customer_phone=order.customer_phone,
        status=order.status,
        payment_method=order.payment_method,
        payment_status=order.payment_status,
        payment_link=order.payment_link,
        delivery_address=order.delivery_address,
        deliverer_id=order.deliverer_id,
        items=[
            OrderItemOut(
                id=item.id,
                product_id=item.product_id,
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in order.items
        ],
    )


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InsufficientStockError, InvalidOrderStateError)):
        return HTTPException(status_code=409, detail=str(exc))
    raise exc


@router.get(
    "/products/{product_id}/availability",
    response_model=AvailabilityOut,
)
async def product_availability(
    product_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> AvailabilityOut:
    try:
        stock_qty = await obtenir_disponibilite(db, product_id)
    except NotFoundError as exc:
        raise _map_error(exc) from exc
    return AvailabilityOut(product_id=product_id, stock_qty=stock_qty)


@router.post("", response_model=OrderOut)
async def create_order(
    body: CreateOrderRequest,
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    try:
        order = await creer_commande(
            db,
            merchant_id=body.merchant_id,
            customer_phone=body.customer_phone,
            items=[(item.product_id, item.quantity) for item in body.items],
            payment_method=body.payment_method,
            delivery_address=body.delivery_address,
        )
    except (NotFoundError, InsufficientStockError, ValueError) as exc:
        if isinstance(exc, ValueError) and not isinstance(
            exc, (InsufficientStockError, InvalidOrderStateError)
        ):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise _map_error(exc) from exc
    return _to_order_out(order)


@router.post("/{order_id}/assign-deliverer", response_model=OrderOut)
async def assign_deliverer(
    order_id: uuid.UUID,
    body: AssignDelivererRequest,
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    try:
        order = await assigner_livreur(db, order_id, body.deliverer_id)
    except (NotFoundError, InvalidOrderStateError) as exc:
        raise _map_error(exc) from exc
    return _to_order_out(order)


@router.post("/{order_id}/confirm-delivery", response_model=OrderOut)
async def confirm_delivery(
    order_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    try:
        order = await confirmer_livraison(db, order_id)
    except (NotFoundError, InvalidOrderStateError) as exc:
        raise _map_error(exc) from exc
    return _to_order_out(order)


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(
    order_id: uuid.UUID,
    body: CancelOrderRequest | None = None,
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    reason = body.reason if body is not None else None
    try:
        order = await annuler_commande(db, order_id, reason)
    except (NotFoundError, InvalidOrderStateError) as exc:
        raise _map_error(exc) from exc
    return _to_order_out(order)
