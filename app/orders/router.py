import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_merchant
from app.catalogue.models import Merchant
from app.core.db import get_db
from app.orders.models import (
    DeliveryZone,
    Order,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.orders.service import (
    DeliveryNotAvailableError,
    InsufficientStockError,
    InvalidOrderStateError,
    NotFoundError,
    PaymentLinkNotAllowedError,
    annuler_commande_commercant,
    assigner_livreur_commercant,
    confirmer_livraison_commercant,
    creer_commande,
    creer_livreur,
    creer_ou_maj_zone_livraison,
    enregistrer_lien_paiement,
    lister_commandes_commercant,
    lister_livreurs,
    lister_zones_livraison,
    obtenir_commande_commercant,
    obtenir_disponibilite,
    order_total,
    supprimer_zone_livraison,
)

# Temporary scaffolding to exercise stock/order flows by hand before the
# WhatsApp agent exists (Jalon 3). Not the final API surface; no auth.
# Authenticated merchant list/detail/actions below are the dashboard API.
# Assign/confirm/cancel share paths with the old temp routes, so those
# three now require a merchant session (create / availability / delivery
# zones stay unauthenticated). Retiring the rest is tracked as pre-pilot
# cleanup, not this change.
router = APIRouter(prefix="/orders", tags=["orders"])
deliverers_router = APIRouter(prefix="/deliverers", tags=["deliverers"])


class OrderItemIn(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(gt=0)


class CreateOrderRequest(BaseModel):
    merchant_id: uuid.UUID
    customer_phone: str
    items: list[OrderItemIn]
    payment_method: PaymentMethod
    delivery_address: str
    ville: str


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
    city: str | None
    deliverer_id: uuid.UUID | None
    items: list[OrderItemOut]


class DeliveryZoneRequest(BaseModel):
    merchant_id: uuid.UUID
    city: str
    available: bool = True
    min_delivery_hours: int
    max_delivery_hours: int


class DeliveryZoneOut(BaseModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    city: str
    available: bool
    min_delivery_hours: int
    max_delivery_hours: int


class AvailabilityOut(BaseModel):
    product_id: uuid.UUID
    stock_qty: int


class MerchantOrderListItem(BaseModel):
    id: uuid.UUID
    customer_phone: str
    city: str | None
    status: OrderStatus
    payment_method: PaymentMethod
    payment_status: PaymentStatus
    payment_link: str | None
    item_count: int
    total: Decimal
    created_at: datetime


class MerchantOrderItemOut(BaseModel):
    product_id: uuid.UUID
    product_name: str
    quantity: int
    unit_price: Decimal


class AssignedDelivererOut(BaseModel):
    id: uuid.UUID
    name: str
    phone: str


class MerchantOrderDetail(MerchantOrderListItem):
    items: list[MerchantOrderItemOut]
    deliverer: AssignedDelivererOut | None


class PaymentLinkRequest(BaseModel):
    payment_link: HttpUrl


class DelivererCreate(BaseModel):
    name: str = Field(min_length=1)
    phone: str = Field(min_length=1)


class DelivererOut(BaseModel):
    id: uuid.UUID
    name: str
    phone: str


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
        city=order.city,
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


def _to_list_item(order: Order) -> MerchantOrderListItem:
    items = list(order.items)
    return MerchantOrderListItem(
        id=order.id,
        customer_phone=order.customer_phone,
        city=order.city,
        status=order.status,
        payment_method=order.payment_method,
        payment_status=order.payment_status,
        payment_link=order.payment_link,
        item_count=len(items),
        total=order_total(items),
        created_at=order.created_at,
    )


def _to_detail(
    order: Order, product_names: dict[uuid.UUID, str]
) -> MerchantOrderDetail:
    listed = _to_list_item(order)
    deliverer = None
    if order.deliverer is not None:
        deliverer = AssignedDelivererOut(
            id=order.deliverer.id,
            name=order.deliverer.name,
            phone=order.deliverer.phone,
        )
    return MerchantOrderDetail(
        **listed.model_dump(),
        items=[
            MerchantOrderItemOut(
                product_id=item.product_id,
                product_name=product_names.get(item.product_id, "Unknown product"),
                quantity=item.quantity,
                unit_price=item.unit_price,
            )
            for item in order.items
        ],
        deliverer=deliverer,
    )


def _not_found_order() -> HTTPException:
    return HTTPException(status_code=404, detail="Order was not found")


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (InsufficientStockError, InvalidOrderStateError, DeliveryNotAvailableError)):
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


def _to_zone_out(zone: DeliveryZone) -> DeliveryZoneOut:
    return DeliveryZoneOut(
        id=zone.id,
        merchant_id=zone.merchant_id,
        city=zone.city,
        available=zone.available,
        min_delivery_hours=zone.min_delivery_hours,
        max_delivery_hours=zone.max_delivery_hours,
    )


@router.post("/delivery-zones", response_model=DeliveryZoneOut)
async def upsert_delivery_zone(
    body: DeliveryZoneRequest,
    db: AsyncSession = Depends(get_db),
) -> DeliveryZoneOut:
    try:
        zone = await creer_ou_maj_zone_livraison(
            db,
            merchant_id=body.merchant_id,
            city=body.city,
            available=body.available,
            min_delivery_hours=body.min_delivery_hours,
            max_delivery_hours=body.max_delivery_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_zone_out(zone)


@router.get("/delivery-zones", response_model=list[DeliveryZoneOut])
async def list_delivery_zones(
    merchant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[DeliveryZoneOut]:
    zones = await lister_zones_livraison(db, merchant_id)
    return [_to_zone_out(zone) for zone in zones]


@router.delete("/delivery-zones/{zone_id}", status_code=204)
async def delete_delivery_zone(
    zone_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await supprimer_zone_livraison(db, zone_id)
    except NotFoundError as exc:
        raise _map_error(exc) from exc


@router.get("", response_model=list[MerchantOrderListItem])
async def list_merchant_orders(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> list[MerchantOrderListItem]:
    """List the calling merchant's orders (newest first, no pagination)."""
    orders = await lister_commandes_commercant(db, merchant.id)
    return [_to_list_item(order) for order in orders]


@router.get("/{order_id}", response_model=MerchantOrderDetail)
async def get_merchant_order(
    order_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> MerchantOrderDetail:
    try:
        order, names = await obtenir_commande_commercant(db, merchant.id, order_id)
    except NotFoundError as exc:
        raise _not_found_order() from exc
    return _to_detail(order, names)


@router.patch("/{order_id}/payment-link", response_model=MerchantOrderDetail)
async def set_merchant_payment_link(
    order_id: uuid.UUID,
    body: PaymentLinkRequest,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> MerchantOrderDetail:
    try:
        await enregistrer_lien_paiement(
            db, merchant.id, order_id, str(body.payment_link)
        )
        order, names = await obtenir_commande_commercant(db, merchant.id, order_id)
    except NotFoundError as exc:
        raise _not_found_order() from exc
    except PaymentLinkNotAllowedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_detail(order, names)


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
            ville=body.ville,
        )
    except (NotFoundError, InsufficientStockError, DeliveryNotAvailableError, ValueError) as exc:
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
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    try:
        order = await assigner_livreur_commercant(
            db, merchant.id, order_id, body.deliverer_id
        )
    except (NotFoundError, InvalidOrderStateError) as exc:
        if isinstance(exc, NotFoundError):
            detail = str(exc)
            if detail.startswith("Deliverer"):
                raise HTTPException(
                    status_code=404, detail="Deliverer was not found"
                ) from exc
            raise _not_found_order() from exc
        raise _map_error(exc) from exc
    return _to_order_out(order)


@router.post("/{order_id}/confirm-delivery", response_model=OrderOut)
async def confirm_delivery(
    order_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    try:
        order = await confirmer_livraison_commercant(db, merchant.id, order_id)
    except (NotFoundError, InvalidOrderStateError) as exc:
        if isinstance(exc, NotFoundError):
            raise _not_found_order() from exc
        raise _map_error(exc) from exc
    return _to_order_out(order)


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(
    order_id: uuid.UUID,
    body: CancelOrderRequest | None = None,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> OrderOut:
    reason = body.reason if body is not None else None
    try:
        order = await annuler_commande_commercant(db, merchant.id, order_id, reason)
    except (NotFoundError, InvalidOrderStateError) as exc:
        if isinstance(exc, NotFoundError):
            raise _not_found_order() from exc
        raise _map_error(exc) from exc
    return _to_order_out(order)


@deliverers_router.get("", response_model=list[DelivererOut])
async def list_merchant_deliverers(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> list[DelivererOut]:
    deliverers = await lister_livreurs(db, merchant.id)
    return [
        DelivererOut(id=item.id, name=item.name, phone=item.phone)
        for item in deliverers
    ]


@deliverers_router.post("", response_model=DelivererOut)
async def create_merchant_deliverer(
    body: DelivererCreate,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> DelivererOut:
    deliverer = await creer_livreur(db, merchant.id, body.name, body.phone)
    return DelivererOut(id=deliverer.id, name=deliverer.name, phone=deliverer.phone)
