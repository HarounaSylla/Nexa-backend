"""Merchant profile and onboarding for the dashboard.

POST /merchants/link-demo is a temporary dogfooding helper so testers can
attach their Clerk account to the seeded Boutique Awa catalogue. Remove
before pilot — same class of scaffolding as /agent/simulate.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_clerk_user_id, get_current_merchant
from app.catalogue.models import Merchant
from app.core.db import get_db
from app.merchants.models import DEFAULT_SHOP_TIMEZONE
from app.merchants.service import (
    DemoMerchantAlreadyLinkedError,
    DemoMerchantNotFoundError,
    MerchantPreferencesData,
    get_preferences,
    link_demo_merchant,
    onboard_merchant,
    update_preferences,
)

router = APIRouter(prefix="/merchants", tags=["merchants"])


class OnboardingRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class MerchantOut(BaseModel):
    id: uuid.UUID
    name: str
    clerk_user_id: str | None
    created_at: datetime


def _to_out(merchant: Merchant) -> MerchantOut:
    return MerchantOut(
        id=merchant.id,
        name=merchant.name,
        clerk_user_id=merchant.clerk_user_id,
        created_at=merchant.created_at,
    )


@router.post("/onboarding", response_model=MerchantOut)
async def onboarding(
    body: OnboardingRequest,
    clerk_user_id: str = Depends(get_current_clerk_user_id),
    db: AsyncSession = Depends(get_db),
) -> MerchantOut:
    merchant = await onboard_merchant(db, clerk_user_id, body.name)
    return _to_out(merchant)


@router.get("/me", response_model=MerchantOut)
async def me(merchant: Merchant = Depends(get_current_merchant)) -> MerchantOut:
    return _to_out(merchant)


@router.post("/link-demo", response_model=MerchantOut)
async def link_demo(
    clerk_user_id: str = Depends(get_current_clerk_user_id),
    db: AsyncSession = Depends(get_db),
) -> MerchantOut:
    try:
        merchant = await link_demo_merchant(db, clerk_user_id)
    except DemoMerchantNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DemoMerchantAlreadyLinkedError as exc:
        raise HTTPException(
            status_code=409, detail="demo_merchant_already_linked"
        ) from exc
    return _to_out(merchant)


TEXT_FIELD_MAX = 1000
_TEXT_FIELDS = (
    "shop_address",
    "opening_hours",
    "return_policy",
    "delivery_fee_note",
    "extra_info",
)


class PreferencesIn(BaseModel):
    accepts_cash_on_delivery: bool
    accepts_online_payment: bool
    timezone: str | None = None
    shop_address: str | None = None
    opening_hours: str | None = None
    return_policy: str | None = None
    delivery_fee_note: str | None = None
    extra_info: str | None = None

    @field_validator(*_TEXT_FIELDS, mode="before")
    @classmethod
    def trim_optional_text(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("must be a string")
        trimmed = value.strip()
        if len(trimmed) > TEXT_FIELD_MAX:
            raise ValueError(f"must be at most {TEXT_FIELD_MAX} characters")
        return trimmed or None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            return None
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("must be a valid IANA timezone name") from exc
        return name

    @model_validator(mode="after")
    def at_least_one_payment_method(self) -> PreferencesIn:
        if not self.accepts_cash_on_delivery and not self.accepts_online_payment:
            raise ValueError("at least one payment method must be accepted")
        return self


class PreferencesOut(BaseModel):
    accepts_cash_on_delivery: bool
    accepts_online_payment: bool
    timezone: str
    shop_address: str | None
    opening_hours: str | None
    return_policy: str | None
    delivery_fee_note: str | None
    extra_info: str | None


def _preferences_out(data: MerchantPreferencesData) -> PreferencesOut:
    return PreferencesOut(
        accepts_cash_on_delivery=data.accepts_cash_on_delivery,
        accepts_online_payment=data.accepts_online_payment,
        timezone=data.timezone,
        shop_address=data.shop_address,
        opening_hours=data.opening_hours,
        return_policy=data.return_policy,
        delivery_fee_note=data.delivery_fee_note,
        extra_info=data.extra_info,
    )


@router.get("/me/preferences", response_model=PreferencesOut)
async def read_my_preferences(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> PreferencesOut:
    return _preferences_out(await get_preferences(db, merchant.id))


@router.put("/me/preferences", response_model=PreferencesOut)
async def replace_my_preferences(
    body: PreferencesIn,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> PreferencesOut:
    current = await get_preferences(db, merchant.id)
    timezone = body.timezone or current.timezone or DEFAULT_SHOP_TIMEZONE
    updated = await update_preferences(
        db,
        merchant.id,
        MerchantPreferencesData(
            accepts_cash_on_delivery=body.accepts_cash_on_delivery,
            accepts_online_payment=body.accepts_online_payment,
            timezone=timezone,
            shop_address=body.shop_address,
            opening_hours=body.opening_hours,
            return_policy=body.return_policy,
            delivery_fee_note=body.delivery_fee_note,
            extra_info=body.extra_info,
        ),
    )
    return _preferences_out(updated)
