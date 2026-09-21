"""Merchant onboarding and demo-account linking."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.models import Merchant

DEMO_MERCHANT_NAME = "Boutique Awa"


class DemoMerchantAlreadyLinkedError(Exception):
    def __init__(self, merchant_id: object) -> None:
        super().__init__("demo_merchant_already_linked")
        self.merchant_id = merchant_id


class DemoMerchantNotFoundError(LookupError):
    def __init__(self) -> None:
        super().__init__(f"Demo merchant {DEMO_MERCHANT_NAME!r} was not found")


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
