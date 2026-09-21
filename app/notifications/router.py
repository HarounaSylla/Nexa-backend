import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_merchant
from app.catalogue.models import Merchant
from app.core.db import get_db
from app.notifications.models import Notification
from app.notifications.service import (
    NotFoundError,
    lister_notifications,
    marquer_comme_lue,
    marquer_toutes_comme_lues,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    related_type: str
    related_id: uuid.UUID
    data: dict[str, Any]
    read_at: datetime | None
    created_at: datetime


class MarkAllReadOut(BaseModel):
    updated: int


def _to_out(row: Notification) -> NotificationOut:
    return NotificationOut(
        id=row.id,
        type=row.type,
        related_type=row.related_type,
        related_id=row.related_id,
        data=dict(row.data or {}),
        read_at=row.read_at,
        created_at=row.created_at,
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Notification was not found")


@router.get("", response_model=list[NotificationOut])
async def list_merchant_notifications(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> list[NotificationOut]:
    rows = await lister_notifications(db, merchant.id)
    return [_to_out(row) for row in rows]


@router.post("/read-all", response_model=MarkAllReadOut)
async def mark_all_notifications_read(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> MarkAllReadOut:
    updated = await marquer_toutes_comme_lues(db, merchant.id)
    return MarkAllReadOut(updated=updated)


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_notification_read(
    notification_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> NotificationOut:
    try:
        row = await marquer_comme_lue(db, merchant.id, notification_id)
    except NotFoundError as exc:
        raise _not_found() from exc
    return _to_out(row)
