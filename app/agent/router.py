import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import Conversation, Message
from app.agent.orchestrator import traiter_message_entrant
from app.core.db import get_db

# Temporary scaffolding to drive the agent by HTTP before the WhatsApp
# webhook exists (Jalon 3 part 2). Not the final API surface; no auth.
router = APIRouter(prefix="/agent", tags=["agent"])


class SimulateRequest(BaseModel):
    merchant_id: uuid.UUID
    customer_phone: str
    message: str


class SimulateResponse(BaseModel):
    conversation_id: uuid.UUID
    reply: str


class MessageOut(BaseModel):
    id: uuid.UUID
    turn_role: str
    display_text: str
    items: list


@router.post("/simulate", response_model=SimulateResponse)
async def simulate(
    body: SimulateRequest,
    db: AsyncSession = Depends(get_db),
) -> SimulateResponse:
    try:
        reply = await traiter_message_entrant(
            db,
            merchant_id=body.merchant_id,
            customer_phone=body.customer_phone,
            message_text=body.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = await db.execute(
        select(Conversation).where(
            Conversation.merchant_id == body.merchant_id,
            Conversation.customer_phone == body.customer_phone,
        ).order_by(Conversation.updated_at.desc())
    )
    conversation = result.scalars().first()
    if conversation is None:
        raise HTTPException(status_code=500, detail="Conversation was not persisted")
    return SimulateResponse(conversation_id=conversation.id, reply=reply)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def conversation_messages(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
    )
    return [
        MessageOut(
            id=message.id,
            turn_role=message.turn_role,
            display_text=message.display_text,
            items=list(message.items or []),
        )
        for message in result.scalars().all()
    ]
