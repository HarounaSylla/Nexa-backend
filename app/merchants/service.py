"""Merchant onboarding, demo-account linking, and shop preferences."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.models import Merchant
from app.merchants.models import (
    DEFAULT_SHOP_TIMEZONE,
    MerchantPaymentLink,
    MerchantPreferences,
)
from app.orders.models import PaymentMethod

MAX_PAYMENT_LINKS_PER_MERCHANT = 10

DEMO_MERCHANT_NAME = "Boutique Awa"


class DemoMerchantAlreadyLinkedError(Exception):
    def __init__(self, merchant_id: object) -> None:
        super().__init__("demo_merchant_already_linked")
        self.merchant_id = merchant_id


class DemoMerchantNotFoundError(LookupError):
    def __init__(self) -> None:
        super().__init__(f"Demo merchant {DEMO_MERCHANT_NAME!r} was not found")


async def get_merchant_by_whatsapp_phone_number_id(
    db: AsyncSession, phone_number_id: str
) -> Merchant | None:
    if not phone_number_id:
        return None
    result = await db.execute(
        select(Merchant).where(
            Merchant.whatsapp_phone_number_id == phone_number_id
        )
    )
    return result.scalar_one_or_none()


async def link_demo_whatsapp_phone_number(
    db: AsyncSession, phone_number_id: str
) -> Merchant:
    """Attach the Cloud API phone number id to the seeded Boutique Awa shop."""
    number = phone_number_id.strip()
    if not number:
        raise ValueError("whatsapp_phone_number_id must not be empty")
    result = await db.execute(
        select(Merchant)
        .where(Merchant.name == DEMO_MERCHANT_NAME)
        .order_by(Merchant.created_at)
        .limit(1)
    )
    demo = result.scalar_one_or_none()
    if demo is None:
        raise DemoMerchantNotFoundError()
    demo.whatsapp_phone_number_id = number
    await db.commit()
    await db.refresh(demo)
    return demo


async def get_merchant_by_clerk_user_id(
    db: AsyncSession, clerk_user_id: str
) -> Merchant | None:
    result = await db.execute(
        select(Merchant).where(Merchant.clerk_user_id == clerk_user_id)
    )
    return result.scalar_one_or_none()


async def onboard_merchant(
    db: AsyncSession, clerk_user_id: str, name: str
) -> Merchant:
    """Create a merchant for this Clerk user, or return the existing one."""
    existing = await get_merchant_by_clerk_user_id(db, clerk_user_id)
    if existing is not None:
        return existing
    merchant = Merchant(name=name.strip(), clerk_user_id=clerk_user_id)
    db.add(merchant)
    await db.commit()
    await db.refresh(merchant)
    return merchant


async def link_demo_merchant(db: AsyncSession, clerk_user_id: str) -> Merchant:
    """Attach this Clerk user to the seeded Boutique Awa shop if unclaimed."""
    already = await get_merchant_by_clerk_user_id(db, clerk_user_id)
    if already is not None:
        return already

    result = await db.execute(
        select(Merchant)
        .where(Merchant.name == DEMO_MERCHANT_NAME)
        .order_by(Merchant.created_at)
        .limit(1)
    )
    demo = result.scalar_one_or_none()
    if demo is None:
        raise DemoMerchantNotFoundError()
    if demo.clerk_user_id is not None:
        raise DemoMerchantAlreadyLinkedError(demo.id)
    demo.clerk_user_id = clerk_user_id
    await db.commit()
    await db.refresh(demo)
    return demo


@dataclass(frozen=True)
class MerchantPreferencesData:
    accepts_cash_on_delivery: bool = True
    accepts_online_payment: bool = True
    timezone: str = DEFAULT_SHOP_TIMEZONE
    shop_address: str | None = None
    opening_hours: str | None = None
    return_policy: str | None = None
    delivery_fee_note: str | None = None
    extra_info: str | None = None


class PaymentMethodNotAcceptedError(ValueError):
    """Raised when the agent tries a payment method this shop does not take."""

    def __init__(self, accepted: list[str]) -> None:
        methods = ", ".join(accepted)
        super().__init__(
            f"This shop does not accept that payment method. Accepted: {methods}."
        )
        self.accepted = accepted


def _from_row(row: MerchantPreferences) -> MerchantPreferencesData:
    return MerchantPreferencesData(
        accepts_cash_on_delivery=row.accepts_cash_on_delivery,
        accepts_online_payment=row.accepts_online_payment,
        timezone=row.timezone,
        shop_address=row.shop_address,
        opening_hours=row.opening_hours,
        return_policy=row.return_policy,
        delivery_fee_note=row.delivery_fee_note,
        extra_info=row.extra_info,
    )


async def get_preferences(
    db: AsyncSession, merchant_id: uuid.UUID
) -> MerchantPreferencesData:
    """Return stored preferences, or the no-row defaults."""
    row = await db.get(MerchantPreferences, merchant_id)
    if row is None:
        return MerchantPreferencesData()
    return _from_row(row)


async def update_preferences(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    data: MerchantPreferencesData,
) -> MerchantPreferencesData:
    """Upsert the seven editable preference fields."""
    row = await db.get(MerchantPreferences, merchant_id)
    if row is None:
        row = MerchantPreferences(merchant_id=merchant_id)
        db.add(row)
    row.accepts_cash_on_delivery = data.accepts_cash_on_delivery
    row.accepts_online_payment = data.accepts_online_payment
    row.timezone = data.timezone
    row.shop_address = data.shop_address
    row.opening_hours = data.opening_hours
    row.return_policy = data.return_policy
    row.delivery_fee_note = data.delivery_fee_note
    row.extra_info = data.extra_info
    await db.commit()
    await db.refresh(row)
    return _from_row(row)


def accepted_payment_method_values(prefs: MerchantPreferencesData) -> list[str]:
    accepted: list[str] = []
    if prefs.accepts_cash_on_delivery:
        accepted.append(PaymentMethod.cash_on_delivery.value)
    if prefs.accepts_online_payment:
        accepted.append(PaymentMethod.online.value)
    return accepted


class DuplicatePaymentLinkLabelError(ValueError):
    def __init__(self) -> None:
        super().__init__("A payment link with this label already exists")


class PaymentLinkLimitExceededError(ValueError):
    def __init__(self) -> None:
        super().__init__("At most 10 payment links are allowed")


class ConfiguredPaymentLinkNotFoundError(LookupError):
    def __init__(self) -> None:
        super().__init__("Payment link was not found")


async def lister_liens_paiement(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[MerchantPaymentLink]:
    result = await db.execute(
        select(MerchantPaymentLink)
        .where(MerchantPaymentLink.merchant_id == merchant_id)
        .order_by(MerchantPaymentLink.created_at.asc(), MerchantPaymentLink.id.asc())
    )
    return list(result.scalars().all())


async def obtenir_lien_paiement(
    db: AsyncSession, merchant_id: uuid.UUID, link_id: uuid.UUID
) -> MerchantPaymentLink:
    link = await db.get(MerchantPaymentLink, link_id)
    if link is None or link.merchant_id != merchant_id:
        raise ConfiguredPaymentLinkNotFoundError()
    return link


async def _label_taken(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    label: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> bool:
    query = select(MerchantPaymentLink.id).where(
        MerchantPaymentLink.merchant_id == merchant_id,
        func.lower(MerchantPaymentLink.label) == label.lower(),
    )
    if exclude_id is not None:
        query = query.where(MerchantPaymentLink.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none() is not None


async def creer_lien_paiement(
    db: AsyncSession, merchant_id: uuid.UUID, label: str, url: str
) -> MerchantPaymentLink:
    existing = await lister_liens_paiement(db, merchant_id)
    if len(existing) >= MAX_PAYMENT_LINKS_PER_MERCHANT:
        raise PaymentLinkLimitExceededError()
    if await _label_taken(db, merchant_id, label):
        raise DuplicatePaymentLinkLabelError()
    row = MerchantPaymentLink(merchant_id=merchant_id, label=label, url=url)
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise DuplicatePaymentLinkLabelError() from None
    await db.refresh(row)
    return row


async def modifier_lien_paiement(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    link_id: uuid.UUID,
    label: str,
    url: str,
) -> MerchantPaymentLink:
    row = await obtenir_lien_paiement(db, merchant_id, link_id)
    if await _label_taken(db, merchant_id, label, exclude_id=link_id):
        raise DuplicatePaymentLinkLabelError()
    row.label = label
    row.url = url
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise DuplicatePaymentLinkLabelError() from None
    await db.refresh(row)
    return row


async def supprimer_lien_paiement(
    db: AsyncSession, merchant_id: uuid.UUID, link_id: uuid.UUID
) -> None:
    row = await obtenir_lien_paiement(db, merchant_id, link_id)
    await db.delete(row)
    await db.commit()


def reject_unaccepted_payment_method(
    prefs: MerchantPreferencesData, method: PaymentMethod
) -> None:
    if method == PaymentMethod.cash_on_delivery and not prefs.accepts_cash_on_delivery:
        raise PaymentMethodNotAcceptedError(accepted_payment_method_values(prefs))
    if method == PaymentMethod.online and not prefs.accepts_online_payment:
        raise PaymentMethodNotAcceptedError(accepted_payment_method_values(prefs))
