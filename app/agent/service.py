"""Agent-side helpers that are not catalogue or orders concerns."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import Conversation, Message
from app.notifications.service import (
    NotificationRelatedType,
    NotificationType,
    emit_notification,
)
from app.orders.service import NotFoundError

STATUS_ACTIVE = "active"
STATUS_ESCALATED = "escalated"
TURN_ROLE_CUSTOMER = "customer"
TURN_ROLE_AGENT = "agent"
TURN_ROLE_MERCHANT = "merchant"


class ConversationNotEscalatedError(ValueError):
    """Raised when return-to-agent is called on a non-escalated conversation."""

    def __init__(self, conversation_id: uuid.UUID, status: str) -> None:
        super().__init__(
            f"Conversation {conversation_id} is not escalated (status={status})"
        )
        self.conversation_id = conversation_id
        self.status = status


async def escalader_vers_humain(
    db: AsyncSession, conversation_id: uuid.UUID, raison: str
) -> Conversation:
    """Mark the conversation as escalated and log the reason as an agent message."""
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFoundError(f"Conversation {conversation_id} was not found")
    conversation.status = STATUS_ESCALATED
    db.add(
        Message(
            conversation_id=conversation.id,
            turn_role=TURN_ROLE_AGENT,
            display_text=f"[escalade] {raison}",
            items=[],
        )
    )
    await emit_notification(
        db,
        merchant_id=conversation.merchant_id,
        notification_type=NotificationType.conversation_escalated,
        related_type=NotificationRelatedType.conversation,
        related_id=conversation.id,
        data={"customer_phone": conversation.customer_phone},
    )
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def lister_messages(db: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
    """Return the thread in chronological order (same query as the temp test route)."""
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
    )
    return list(result.scalars().all())


async def _owned_conversation(
    db: AsyncSession, merchant_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.merchant_id != merchant_id:
        raise NotFoundError(f"Conversation {conversation_id} was not found")
    return conversation


async def lister_conversations_commercant(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[dict]:
    """List this merchant's conversations.

    Escalated threads first, then most recently updated. No pagination —
    same future-limit note as the product and order lists.
    """
    result = await db.execute(
        select(Conversation)
        .where(Conversation.merchant_id == merchant_id)
        .order_by(
            case((Conversation.status == STATUS_ESCALATED, 0), else_=1),
            Conversation.updated_at.desc(),
            Conversation.id.desc(),
        )
    )
    conversations = list(result.scalars().all())
    if not conversations:
        return []

    ids = [conversation.id for conversation in conversations]
    counts = dict(
        (
            await db.execute(
                select(Message.conversation_id, func.count())
                .where(Message.conversation_id.in_(ids))
                .group_by(Message.conversation_id)
            )
        ).all()
    )
    last_rows = (
        await db.execute(
            select(Message)
            .where(Message.conversation_id.in_(ids))
            .order_by(Message.created_at.desc(), Message.id.desc())
        )
    ).scalars().all()
    last_by_conversation: dict[uuid.UUID, Message] = {}
    for message in last_rows:
        if message.conversation_id not in last_by_conversation:
            last_by_conversation[message.conversation_id] = message

    rows: list[dict] = []
    for conversation in conversations:
        last = last_by_conversation.get(conversation.id)
        preview = None
        last_at = None
        if last is not None:
            preview = last.display_text
            if len(preview) > 140:
                preview = preview[:137] + "..."
            last_at = last.created_at
        rows.append(
            {
                "id": conversation.id,
                "customer_phone": conversation.customer_phone,
                "status": conversation.status,
                "last_message_preview": preview,
                "last_message_at": last_at,
                "message_count": int(counts.get(conversation.id, 0)),
            }
        )
    return rows


async def obtenir_conversation_commercant(
    db: AsyncSession, merchant_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    return await _owned_conversation(db, merchant_id, conversation_id)


async def lister_messages_commercant(
    db: AsyncSession, merchant_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[Message]:
    await _owned_conversation(db, merchant_id, conversation_id)
    return await lister_messages(db, conversation_id)


async def repondre_en_humain(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    message: str,
) -> Message:
    """Append a merchant message. Does not call the agent or any tool."""
    conversation = await _owned_conversation(db, merchant_id, conversation_id)
    text = message.strip()
    if not text:
        raise ValueError("message must not be empty")
    row = Message(
        conversation_id=conversation.id,
        turn_role=TURN_ROLE_MERCHANT,
        display_text=text,
        items=[],
    )
    db.add(row)
    conversation.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(row)
    return row


async def reprendre_par_agent(
    db: AsyncSession, merchant_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    """Clear escalated status so the next inbound customer message can resume the agent.

    Rejects with ConversationNotEscalatedError if the conversation is not
    currently escalated (not a no-op).
    """
    conversation = await _owned_conversation(db, merchant_id, conversation_id)
    if conversation.status != STATUS_ESCALATED:
        raise ConversationNotEscalatedError(conversation.id, conversation.status)
    conversation.status = STATUS_ACTIVE
    conversation.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(conversation)
    return conversation
