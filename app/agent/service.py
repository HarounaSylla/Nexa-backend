"""Agent-side helpers that are not catalogue or orders concerns."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import Conversation, Message
from app.orders.service import NotFoundError


async def escalader_vers_humain(
    db: AsyncSession, conversation_id: uuid.UUID, raison: str
) -> Conversation:
    """Mark the conversation as escalated and log the reason as an agent message."""
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise NotFoundError(f"Conversation {conversation_id} was not found")
    conversation.status = "escalated"
    db.add(
        Message(
            conversation_id=conversation.id,
            turn_role="agent",
            display_text=f"[escalade] {raison}",
            items=[],
        )
    )
    await db.commit()
    await db.refresh(conversation)
    return conversation
