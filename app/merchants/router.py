"""Merchant profile and onboarding for the dashboard.

POST /merchants/link-demo is a temporary dogfooding helper so testers can
attach their Clerk account to the seeded Boutique Awa catalogue. Remove
before pilot — same class of scaffolding as /agent/simulate.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_clerk_user_id, get_current_merchant
from app.catalogue.models import Merchant
from app.core.db import get_db
from app.merchants.service import (
    DemoMerchantAlreadyLinkedError,
    DemoMerchantNotFoundError,
    link_demo_merchant,
    onboard_merchant,
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
