"""Public notification helpers. Callers own the surrounding transaction.

`emit_notification` flushes only — it must not commit, so a failed
`creer_commande` / `escalader_vers_humain` rolls the row back with the rest.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.notifications.models import (
    Notification,
    NotificationRelatedType,
    NotificationType,
)

__all__ = [
    "NotFoundError",
    "NotificationRelatedType",
    "NotificationType",
    "emit_notification",
    "lister_notifications",
    "marquer_comme_lue",
    "marquer_toutes_comme_lues",
]


class NotFoundError(LookupError):
    """Raised when a notification does not exist for this merchant."""


def _as_value(value: NotificationType | NotificationRelatedType | str) -> str:
    if isinstance(value, enum.Enum):
        return str(value.value)
    return str(value)


async def emit_notification(
    db: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    notification_type: NotificationType | str,
    related_type: NotificationRelatedType | str,
    related_id: uuid.UUID,
    data: dict[str, Any],
) -> Notification:
    """Insert a notification in the current transaction. Does not commit."""
    row = Notification(
        merchant_id=merchant_id,
        type=_as_value(notification_type),
        related_type=_as_value(related_type),
        related_id=related_id,
        data=data,
    )
    db.add(row)
    await db.flush()
    return row


async def lister_notifications(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[Notification]:
    """This merchant's notifications, newest first.

    No pagination — same future-limit note as the product, order, and
    conversation lists.
    """
    result = await db.execute(
        select(Notification)
        .where(Notification.merchant_id == merchant_id)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
    )
    return list(result.scalars().all())


async def marquer_comme_lue(
    db: AsyncSession, merchant_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    row = await db.get(Notification, notification_id)
    if row is None or row.merchant_id != merchant_id:
        raise NotFoundError(f"Notification {notification_id} was not found")
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(row)
    return row


async def marquer_toutes_comme_lues(
    db: AsyncSession, merchant_id: uuid.UUID
) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Notification)
        .where(
            Notification.merchant_id == merchant_id,
            Notification.read_at.is_(None),
        )
        .values(read_at=now)
    )
    await db.commit()
    return int(result.rowcount or 0)
