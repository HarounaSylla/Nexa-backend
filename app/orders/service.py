import unicodedata
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.catalogue.models import Merchant, Product
from app.core.formatting import format_fcfa
from app.notifications.service import (
    NotificationRelatedType,
    NotificationType,
    emit_notification,
)
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


class DeliveryNotAvailableError(Exception):
    def __init__(self, city: str) -> None:
        super().__init__(f"Delivery is not available in {city}")
        self.city = city


def normalize_city(city: str) -> str:
    """Lowercase, strip accents, collapse extra whitespace."""
    collapsed = " ".join(city.split())
    decomposed = unicodedata.normalize("NFKD", collapsed)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return without_accents.casefold()


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


async def _lock_merchant(db: AsyncSession, merchant_id: uuid.UUID) -> Merchant:
    """Lock the merchant row so order_number assignment cannot collide.

    Callers that also lock products must take this merchant lock first,
    then product rows ordered by product.id — never the reverse.
    """
    result = await db.execute(
        select(Merchant).where(Merchant.id == merchant_id).with_for_update()
    )
    merchant = result.scalar_one_or_none()
    if merchant is None:
        raise NotFoundError(f"Merchant {merchant_id} was not found")
    return merchant


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


async def obtenir_zone_livraison(
    db: AsyncSession, merchant_id: uuid.UUID, city: str
) -> DeliveryZone | None:
    """Look up by normalized city name.

    Return None if no zone is configured for this city at all — that
    means "not covered", same as an explicit available=False row, but
    the caller can tell the two apart if it wants to (no row vs.
    explicitly disabled).
    """
    normalized = normalize_city(city)
    if not normalized:
        return None
    result = await db.execute(
        select(DeliveryZone).where(
            DeliveryZone.merchant_id == merchant_id,
            DeliveryZone.city_normalized == normalized,
        )
    )
    return result.scalar_one_or_none()


async def creer_ou_maj_zone_livraison(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    city: str,
    available: bool,
    min_delivery_hours: int,
    max_delivery_hours: int,
) -> DeliveryZone:
    """Upsert on (merchant_id, city_normalized)."""
    display_city = " ".join(city.split())
    normalized = normalize_city(city)
    if not normalized:
        raise ValueError("city must not be empty")
    if min_delivery_hours < 0:
        raise ValueError("min_delivery_hours must be >= 0")
    if max_delivery_hours < min_delivery_hours:
        raise ValueError("max_delivery_hours must be >= min_delivery_hours")

    zone = await obtenir_zone_livraison(db, merchant_id, city)
    if zone is None:
        zone = DeliveryZone(
            merchant_id=merchant_id,
            city=display_city,
            city_normalized=normalized,
            available=available,
            min_delivery_hours=min_delivery_hours,
            max_delivery_hours=max_delivery_hours,
        )
        db.add(zone)
    else:
        zone.city = display_city
        zone.available = available
        zone.min_delivery_hours = min_delivery_hours
        zone.max_delivery_hours = max_delivery_hours
    await db.commit()
    await db.refresh(zone)
    return zone


async def lister_zones_livraison(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[DeliveryZone]:
    result = await db.execute(
        select(DeliveryZone)
        .where(DeliveryZone.merchant_id == merchant_id)
        .order_by(DeliveryZone.city)
    )
    return list(result.scalars().all())


async def supprimer_zone_livraison(
    db: AsyncSession,
    zone_id: uuid.UUID,
    merchant_id: uuid.UUID | None = None,
) -> None:
    query = delete(DeliveryZone).where(DeliveryZone.id == zone_id)
    if merchant_id is not None:
        query = query.where(DeliveryZone.merchant_id == merchant_id)
    result = await db.execute(query)
    if result.rowcount == 0:
        raise NotFoundError(f"Delivery zone {zone_id} was not found")
    await db.commit()


async def creer_commande(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    items: list[tuple[uuid.UUID, int]],
    payment_method: PaymentMethod,
    delivery_address: str,
    ville: str,
    conversation_id: uuid.UUID | None = None,
) -> Order:
    """Create an order and provisionally decrement stock, atomically.

    Locks the merchant row first (`SELECT ... FOR UPDATE`) to assign a
    unique per-merchant `order_number`, then locks every involved product
    with `SELECT ... FOR UPDATE` in product_id order. Rejects the whole
    order if any item is short, then decrements stock, writes
    stock_movements, and inserts the order + items in the same
    transaction. Other writers that need both locks must use this same
    order (merchant, then products) to avoid deadlocks.

    `ville` is required separately from the free-text address. Delivery
    coverage is checked before any lock; unserved cities raise
    DeliveryNotAvailableError and create nothing.

    `conversation_id` is stored when the order came from a WhatsApp
    thread; merchant-created orders leave it None.
    """
    quantities = _aggregate_quantities(items)
    product_ids = sorted(quantities)
    try:
        zone = await obtenir_zone_livraison(db, merchant_id, ville)
        if zone is None or not zone.available:
            raise DeliveryNotAvailableError(ville)
        merchant = await _lock_merchant(db, merchant_id)
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

        order_number = merchant.next_order_number
        merchant.next_order_number = order_number + 1
        order = Order(
            merchant_id=merchant_id,
            order_number=order_number,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            status=OrderStatus.created,
            payment_method=payment_method,
            payment_status=PaymentStatus.pending,
            delivery_address=delivery_address,
            city=" ".join(ville.split()),
        )
        db.add(order)
        await db.flush()

        total = Decimal("0.00")
        out_of_stock: list[Product] = []
        for product_id in product_ids:
            product = products[product_id]
            requested = quantities[product_id]
            product.stock_qty -= requested
            total += Decimal(product.price) * requested
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
            if product.stock_qty == 0:
                out_of_stock.append(product)

        await emit_notification(
            db,
            merchant_id=merchant_id,
            notification_type=NotificationType.new_order,
            related_type=NotificationRelatedType.order,
            related_id=order.id,
            data={
                "customer_phone": customer_phone,
                "total": str(total),
            },
        )
        for product in out_of_stock:
            await emit_notification(
                db,
                merchant_id=merchant_id,
                notification_type=NotificationType.product_out_of_stock,
                related_type=NotificationRelatedType.product,
                related_id=product.id,
                data={"product_name": product.name},
            )

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()


async def consulter_commande(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    order_number: int | None = None,
) -> Order | None:
    """Look up one order for this specific customer.

    Scoped by BOTH merchant_id and customer_phone — never just
    order_number — so a customer can never retrieve another customer's
    order by guessing a number. If order_number is None, return the most
    recent order for this phone at this merchant.
    """
    query = (
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.deliverer))
        .where(
            Order.merchant_id == merchant_id,
            Order.customer_phone == customer_phone,
        )
    )
    if order_number is not None:
        query = query.where(Order.order_number == order_number)
    else:
        query = query.order_by(Order.created_at.desc()).limit(1)
    result = await db.execute(query)
    return result.unique().scalars().first()


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
        if order.payment_method == PaymentMethod.cash_on_delivery:
            order.payment_status = PaymentStatus.paid
        for item in order.items:
            db.add(
                StockMovement(
                    product_id=item.product_id,
                    order_id=order.id,
                    movement_type=MOVEMENT_DELIVERED_FINALIZE,
                    quantity_delta=0,
                )
            )
        if order.conversation_id is not None:
            from app.agent.service import fermer_conversation_si_active

            await fermer_conversation_si_active(db, order.conversation_id)
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


class PaymentLinkNotAllowedError(ValueError):
    """Raised when a payment link is set on a cash-on-delivery order."""

    def __init__(self) -> None:
        super().__init__(
            "A payment link can only be set on an online-payment order"
        )


class PaymentNotMarkableError(ValueError):
    """Raised when mark-paid is used on a non-online order."""

    def __init__(self) -> None:
        super().__init__(
            "Only online-payment orders can be marked as paid; "
            "cash-on-delivery orders are marked paid when delivery is confirmed"
        )


class OrderAlreadyPaidError(ValueError):
    """Raised when a payment link is sent for an already-paid order."""

    def __init__(self) -> None:
        super().__init__("This order is already paid")


class ProofRejectionNotAllowedError(ValueError):
    """Raised when reject-proof is called on a paid order."""

    def __init__(self) -> None:
        super().__init__("This order is already paid")


def phone_lookup_variants(phone: str) -> list[str]:
    raw = (phone or "").strip()
    if not raw:
        return []
    plus = raw if raw.startswith("+") else f"+{raw}"
    bare = raw.lstrip("+")
    return list(dict.fromkeys([raw, plus, bare]))


def build_payment_link_message(
    order_number: int,
    total: Decimal,
    payment_link: str,
    updated: bool,
    label: str | None = None,
) -> str:
    amount = format_fcfa(total)
    label_word = (label or "").strip()
    named = f" {label_word}" if label_word else ""
    first = (
        f"Bonjour, voici à nouveau le lien{named} pour régler votre commande "
        f"n°{order_number} ({amount}) :"
        if updated
        else (
            f"Bonjour, voici le lien{named} pour régler votre commande "
            f"n°{order_number} ({amount}) :"
        )
    )
    return (
        f"{first}\n"
        f"{payment_link}\n"
        "\n"
        "Après le paiement, envoyez-nous ici une photo ou une capture "
        "d'écran de la preuve de paiement, en indiquant le numéro de "
        f"commande {order_number}. Plusieurs clients peuvent payer avec "
        "le même lien : c'est cette preuve qui nous permet de retrouver "
        "votre paiement."
    )


def order_total(items: list[OrderItem]) -> Decimal:
    return sum(
        (item.unit_price * item.quantity for item in items),
        Decimal("0.00"),
    )


async def _owned_order(
    db: AsyncSession, merchant_id: uuid.UUID, order_id: uuid.UUID
) -> Order:
    order = await db.get(Order, order_id)
    if order is None or order.merchant_id != merchant_id:
        raise NotFoundError(f"Order {order_id} was not found")
    return order


async def lister_commandes_commercant(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[Order]:
    """List this merchant's orders, newest first.

    No pagination — fine for a few hundred rows; add a limit/offset
    when order history grows past that.
    """
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items))
        .where(Order.merchant_id == merchant_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
    )
    return list(result.scalars().unique().all())


async def obtenir_commande_commercant(
    db: AsyncSession, merchant_id: uuid.UUID, order_id: uuid.UUID
) -> tuple[Order, dict[uuid.UUID, str]]:
    result = await db.execute(
        select(Order)
        .options(selectinload(Order.items), selectinload(Order.deliverer))
        .where(Order.id == order_id, Order.merchant_id == merchant_id)
    )
    order = result.unique().scalar_one_or_none()
    if order is None:
        raise NotFoundError(f"Order {order_id} was not found")
    product_ids = [item.product_id for item in order.items]
    names: dict[uuid.UUID, str] = {}
    if product_ids:
        products = await db.execute(
            select(Product.id, Product.name).where(Product.id.in_(product_ids))
        )
        names = {row.id: row.name for row in products.all()}
    return order, names


async def assigner_livreur_commercant(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    order_id: uuid.UUID,
    deliverer_id: uuid.UUID,
) -> Order:
    await _owned_order(db, merchant_id, order_id)
    deliverer = await db.get(Deliverer, deliverer_id)
    if deliverer is None or deliverer.merchant_id != merchant_id:
        raise NotFoundError(f"Deliverer {deliverer_id} was not found")
    return await assigner_livreur(db, order_id, deliverer_id)


async def confirmer_livraison_commercant(
    db: AsyncSession, merchant_id: uuid.UUID, order_id: uuid.UUID
) -> Order:
    await _owned_order(db, merchant_id, order_id)
    return await confirmer_livraison(db, order_id)


async def annuler_commande_commercant(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    order_id: uuid.UUID,
    reason: str | None,
) -> Order:
    await _owned_order(db, merchant_id, order_id)
    return await annuler_commande(db, order_id, reason)


async def marquer_commande_payee(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    order_id: uuid.UUID,
) -> Order:
    """Mark an online order as paid. Idempotent if already paid."""
    await _owned_order(db, merchant_id, order_id)
    try:
        order = await _get_order_for_update(db, order_id)
        if order.status == OrderStatus.cancelled:
            raise InvalidOrderStateError(order.id, order.status, "mark as paid")
        if order.payment_method != PaymentMethod.online:
            raise PaymentNotMarkableError()
        if order.payment_status != PaymentStatus.paid:
            order.payment_status = PaymentStatus.paid
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()


async def commandes_en_attente_de_preuve(
    db: AsyncSession, merchant_id: uuid.UUID, customer_phone: str
) -> list[Order]:
    variants = phone_lookup_variants(customer_phone)
    if not variants:
        return []
    result = await db.execute(
        select(Order)
        .where(
            Order.merchant_id == merchant_id,
            Order.customer_phone.in_(variants),
            Order.payment_method == PaymentMethod.online,
            Order.status != OrderStatus.cancelled,
            Order.payment_status.in_(
                (PaymentStatus.pending, PaymentStatus.proof_received)
            ),
            Order.payment_link_sent_at.is_not(None),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
    )
    return list(result.scalars().all())


async def appliquer_preuve_recue(db: AsyncSession, order_id: uuid.UUID) -> Order:
    """Set pending → proof_received under the usual row lock. Does not commit."""
    order = await _get_order_for_update(db, order_id)
    if order.payment_status == PaymentStatus.pending:
        order.payment_status = PaymentStatus.proof_received
    return order


async def lister_commandes_par_conversations(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    conversation_ids: list[uuid.UUID],
    *,
    limit_per_conversation: int = 10,
) -> dict[uuid.UUID, list[Order]]:
    if not conversation_ids:
        return {}
    result = await db.execute(
        select(Order)
        .where(
            Order.merchant_id == merchant_id,
            Order.conversation_id.in_(conversation_ids),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
    )
    grouped: dict[uuid.UUID, list[Order]] = {cid: [] for cid in conversation_ids}
    for order in result.scalars().all():
        if order.conversation_id is None:
            continue
        bucket = grouped.setdefault(order.conversation_id, [])
        if len(bucket) < limit_per_conversation:
            bucket.append(order)
    return grouped


async def rejeter_preuve(
    db: AsyncSession, merchant_id: uuid.UUID, order_id: uuid.UUID
) -> Order:
    await _owned_order(db, merchant_id, order_id)
    try:
        order = await _get_order_for_update(db, order_id)
        if order.status == OrderStatus.cancelled:
            raise InvalidOrderStateError(order.id, order.status, "reject proof for")
        if order.payment_status == PaymentStatus.paid:
            raise ProofRejectionNotAllowedError()
        if order.payment_status == PaymentStatus.proof_received:
            order.payment_status = PaymentStatus.pending
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()


async def envoyer_lien_paiement(
    db: AsyncSession,
    merchant: Merchant,
    order_id: uuid.UUID,
    payment_link_id: uuid.UUID,
) -> Order:
    from app.agent.service import enregistrer_message_commercant
    from app.merchants.service import obtenir_lien_paiement
    from app.whatsapp.service import envoyer_message_commercant

    merchant_id = merchant.id
    sender = Merchant(
        whatsapp_phone_number_id=merchant.whatsapp_phone_number_id
    )
    order = await _owned_order(db, merchant_id, order_id)
    if order.payment_method != PaymentMethod.online:
        raise PaymentLinkNotAllowedError()
    if order.status == OrderStatus.cancelled:
        raise InvalidOrderStateError(order.id, order.status, "send a payment link for")
    if order.payment_status == PaymentStatus.paid:
        raise OrderAlreadyPaidError()

    link = await obtenir_lien_paiement(db, merchant_id, payment_link_id)
    snapshot_url = link.url
    snapshot_label = link.label
    updated = order.payment_link_sent_at is not None
    order.payment_link = snapshot_url
    order.payment_link_label = snapshot_label
    await db.commit()

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    order = result.scalar_one()
    customer_phone = order.customer_phone
    conversation_id = order.conversation_id
    text = build_payment_link_message(
        order.order_number,
        order_total(list(order.items)),
        snapshot_url,
        updated,
        snapshot_label,
    )
    await db.rollback()

    await envoyer_message_commercant(sender, customer_phone, text)

    order = await _owned_order(db, merchant_id, order_id)
    order.payment_link_sent_at = datetime.now(timezone.utc)
    if conversation_id is not None:
        await enregistrer_message_commercant(db, conversation_id, text)
    await db.commit()

    result = await db.execute(
        select(Order).options(selectinload(Order.items)).where(Order.id == order.id)
    )
    return result.scalar_one()


async def lister_livreurs(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[Deliverer]:
    result = await db.execute(
        select(Deliverer)
        .where(Deliverer.merchant_id == merchant_id)
        .order_by(Deliverer.name, Deliverer.created_at)
    )
    return list(result.scalars().all())


async def creer_livreur(
    db: AsyncSession, merchant_id: uuid.UUID, name: str, phone: str
) -> Deliverer:
    deliverer = Deliverer(merchant_id=merchant_id, name=name, phone=phone)
    db.add(deliverer)
    await db.commit()
    await db.refresh(deliverer)
    return deliverer

