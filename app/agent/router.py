import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import Conversation, Message
from app.agent.orchestrator import traiter_message_entrant
from app.agent.service import (
    ConversationNotEscalatedError,
    lister_conversations_commercant,
    lister_messages,
    lister_messages_commercant,
    reprendre_par_agent,
    repondre_en_humain,
)
from app.auth.deps import get_current_merchant
from app.catalogue.models import Merchant
from app.core.db import get_db
from app.orders.service import NotFoundError

# Temporary scaffolding to drive the agent by HTTP before the WhatsApp
# webhook exists (Jalon 3 part 2). Not the final API surface; no auth.
# Merchant dashboard conversations live on /conversations (different
# prefix), so GET /agent/conversations/{id}/messages stays as the
# unauthenticated test harness alongside POST /agent/simulate.
router = APIRouter(prefix="/agent", tags=["agent"])
conversations_router = APIRouter(prefix="/conversations", tags=["conversations"])


class SimulateRequest(BaseModel):
    merchant_id: uuid.UUID
    customer_phone: str
    message: str


class ProductImageRef(BaseModel):
    product_id: uuid.UUID
    image_url: str


class SimulateResponse(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    images: list[ProductImageRef] = []


def extract_product_images(items: list[Any] | None) -> list[ProductImageRef]:
    """Collect {product_id, image_url} from tool outputs in an agent turn.

    The chat text must not contain URLs; WhatsApp media send (Jalon 3
    part 2) will consume this list later.
    """
    seen: dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "function_call_output":
            continue
        raw = item.get("output")
        payload: Any = raw
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if not isinstance(payload, dict):
            continue
        for product in payload.get("products") or []:
            if not isinstance(product, dict):
                continue
            product_id = product.get("id") or product.get("product_id")
            image_url = product.get("image_url")
            if product_id and image_url:
                seen[str(product_id)] = str(image_url)
        product_id = payload.get("product_id")
        image_url = payload.get("image_url")
        if product_id and image_url:
            seen[str(product_id)] = str(image_url)
    return [
        ProductImageRef(product_id=uuid.UUID(product_id), image_url=image_url)
        for product_id, image_url in seen.items()
    ]


class MessageOut(BaseModel):
    id: uuid.UUID
    turn_role: str
    display_text: str
    items: list
    created_at: datetime | None = None


class ConversationListItem(BaseModel):
    id: uuid.UUID
    customer_phone: str
    status: str
    last_message_preview: str | None
    last_message_at: datetime | None
    message_count: int


class MerchantMessageOut(BaseModel):
    id: uuid.UUID
    turn_role: str
    display_text: str
    created_at: datetime


class HumanReplyRequest(BaseModel):
    message: str = Field(min_length=1)


class ConversationStatusOut(BaseModel):
    id: uuid.UUID
    status: str


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
    last_agent = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.turn_role == "agent",
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    last_message = last_agent.scalars().first()
    return SimulateResponse(
        conversation_id=conversation.id,
        reply=reply,
        images=extract_product_images(
            last_message.items if last_message is not None else []
        ),
    )


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageOut])
async def conversation_messages(
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await lister_messages(db, conversation_id)
    return [
        MessageOut(
            id=message.id,
            turn_role=message.turn_role,
            display_text=message.display_text,
            items=list(message.items or []),
            created_at=message.created_at,
        )
        for message in messages
    ]


def _not_found_conversation() -> HTTPException:
    return HTTPException(status_code=404, detail="Conversation was not found")


@conversations_router.get("", response_model=list[ConversationListItem])
async def list_merchant_conversations(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationListItem]:
    rows = await lister_conversations_commercant(db, merchant.id)
    return [ConversationListItem.model_validate(row) for row in rows]


@conversations_router.get(
    "/{conversation_id}/messages",
    response_model=list[MerchantMessageOut],
)
async def get_merchant_conversation_messages(
    conversation_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> list[MerchantMessageOut]:
    try:
        messages = await lister_messages_commercant(db, merchant.id, conversation_id)
    except NotFoundError as exc:
        raise _not_found_conversation() from exc
    return [
        MerchantMessageOut(
            id=message.id,
            turn_role=message.turn_role,
            display_text=message.display_text,
            created_at=message.created_at,
        )
        for message in messages
    ]


@conversations_router.post(
    "/{conversation_id}/reply",
    response_model=MerchantMessageOut,
)
async def reply_as_merchant(
    conversation_id: uuid.UUID,
    body: HumanReplyRequest,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> MerchantMessageOut:
    try:
        message = await repondre_en_humain(
            db, merchant.id, conversation_id, body.message
        )
    except NotFoundError as exc:
        raise _not_found_conversation() from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MerchantMessageOut(
        id=message.id,
        turn_role=message.turn_role,
        display_text=message.display_text,
        created_at=message.created_at,
    )


@conversations_router.post(
    "/{conversation_id}/return-to-agent",
    response_model=ConversationStatusOut,
)
async def return_conversation_to_agent(
    conversation_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> ConversationStatusOut:
    try:
        conversation = await reprendre_par_agent(db, merchant.id, conversation_id)
    except NotFoundError as exc:
        raise _not_found_conversation() from exc
    except ConversationNotEscalatedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ConversationStatusOut(id=conversation.id, status=conversation.status)
