"""FastAPI dependencies for Clerk-authenticated dashboard routes."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.clerk import InvalidClerkTokenError, verify_clerk_session_token
from app.catalogue.models import Merchant
from app.core.db import get_db

_bearer = HTTPBearer(auto_error=False)


class MerchantNotOnboardedError(LookupError):
    """Valid Clerk token, but no merchants.clerk_user_id match yet."""


def _unauthorized(detail: str = "invalid_token") -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_clerk_user_id(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """Verify the Bearer token; do not require a linked merchant."""
    del request  # available for cookie-based same-origin later
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized("missing_token")
    try:
        return verify_clerk_session_token(creds.credentials)
    except InvalidClerkTokenError as exc:
        raise _unauthorized(str(exc)) from exc


async def get_current_merchant(
    clerk_user_id: str = Depends(get_current_clerk_user_id),
    db: AsyncSession = Depends(get_db),
) -> Merchant:
    """Return the merchant linked to this Clerk user, or 404 if none."""
    result = await db.execute(
        select(Merchant).where(Merchant.clerk_user_id == clerk_user_id)
    )
    merchant = result.scalar_one_or_none()
    if merchant is None:
        raise HTTPException(
            status_code=404,
            detail="merchant_not_onboarded",
        ) from MerchantNotOnboardedError(clerk_user_id)
    return merchant
