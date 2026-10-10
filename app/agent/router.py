import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import Conversation, Message
from app.agent.orchestrator import traiter_message_entrant
from app.agent.images import ProductImageRef, extract_product_images
from app.agent.handover import (
    KIND_CUSTOMER_PHOTO,
    KIND_CUSTOMER_TEXT,
    KIND_SHOP_PHOTO,
    KIND_SHOP_TEXT,
    trim_quote_excerpt,
)
from app.agent.service import (
    ConversationNotEscalatedError,
    citations_pour_messages,
    lister_conversations_commercant,
    lister_messages,
    lister_messages_commercant,
    obtenir_conversation_commercant,
    reprendre_par_agent,
    repondre_en_humain,
)
from app.proofs.models import InboundImage
from app.proofs.service import lister_images_par_messages
from app.auth.deps import get_current_merchant
from app.catalogue.models import Merchant
from app.catalogue.service import noms_produits
from app.core.db import get_db
from app.core.phone import InvalidPhoneNumberError, normalize_phone
from app.orders.service import NotFoundError, phone_lookup_variants
from app.whatsapp.service import WhatsAppSendError

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

    @field_validator("customer_phone")
    @classmethod
    def canonicalize_phone(cls, value: str) -> str:
        try:
            return normalize_phone(value)
        except InvalidPhoneNumberError as exc:
            raise ValueError(str(exc)) from exc


class SimulateResponse(BaseModel):
    conversation_id: uuid.UUID
    reply: str
    images: list[ProductImageRef] = []


class MessageOut(BaseModel):
    id: uuid.UUID
    turn_role: str
    display_text: str
    items: list
    created_at: datetime | None = None


class ConversationOrderOut(BaseModel):
    id: uuid.UUID
    order_number: int
    status: str
    payment_status: str


class ConversationListItem(BaseModel):
    id: uuid.UUID
    customer_phone: str
    status: str
    last_message_preview: str | None
    last_message_at: datetime | None
    message_count: int
    orders: list[ConversationOrderOut] = []


class MessageImageOut(BaseModel):
    id: uuid.UUID
    classification: str
    order_id: uuid.UUID | None
    detected_amount: str | None
    deleted: bool = False
    match_level: str | None = None
    matched_product_id: uuid.UUID | None = None
    matched_product_name: str | None = None
    match_kind: Literal["exact", "similar"] | None = None


class QuotedMessageOut(BaseModel):
    kind: Literal[
        "shop_text",
        "shop_photo",
        "customer_text",
        "customer_photo",
    ]
    excerpt: str | None = None
    product_name: str | None = None
    from_earlier_conversation: bool
    message_id: uuid.UUID | None = None


def _verdict_for_matched_product(image: InboundImage) -> str | None:
    if image.matched_product_id is None:
        return None
    wanted = str(image.matched_product_id)
    for row in image.match_candidates or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("product_id")) != wanted:
            continue
        verdict = row.get("verdict")
        return verdict if isinstance(verdict, str) else None
    return None


def _match_kind_for_image(image: InboundImage) -> Literal["exact", "similar"] | None:
    if image.matched_product_id is None:
        return None
    verdict = _verdict_for_matched_product(image)
    if verdict == "similar":
        return "similar"
    if verdict == "same" or image.match_level == "strong":
        return "exact"
    return None


_QUOTED_KINDS = {
    KIND_SHOP_TEXT,
    KIND_SHOP_PHOTO,
    KIND_CUSTOMER_TEXT,
    KIND_CUSTOMER_PHOTO,
}


def _quoted_message_out(info) -> QuotedMessageOut | None:
    if info.kind not in _QUOTED_KINDS:
        return None
    raw = info.excerpt
    if info.kind in {KIND_SHOP_PHOTO, KIND_CUSTOMER_PHOTO} and not (raw or "").strip():
        excerpt = None
    else:
        excerpt = trim_quote_excerpt(raw) if raw else None
        if not excerpt:
            excerpt = None
    return QuotedMessageOut(
        kind=info.kind,
        excerpt=excerpt,
        product_name=info.product_name,
        from_earlier_conversation=info.from_earlier_conversation,
        message_id=info.message_id,
    )


def _message_image_out(
    image: InboundImage, names: dict[uuid.UUID, str]
) -> MessageImageOut:
    product_id = image.matched_product_id
    return MessageImageOut(
        id=image.id,
        classification=image.classification,
        order_id=image.order_id,
        detected_amount=(
            str(image.detected_amount) if image.detected_amount is not None else None
        ),
        deleted=image.media_deleted_at is not None,
        match_level=image.match_level,
        matched_product_id=product_id,
        matched_product_name=names.get(product_id) if product_id is not None else None,
        match_kind=_match_kind_for_image(image),
    )


class MerchantMessageOut(BaseModel):
    id: uuid.UUID
    turn_role: str
    display_text: str
    created_at: datetime
    image: MessageImageOut | None = None
    quoted: QuotedMessageOut | None = None


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
            Conversation.customer_phone.in_(
                phone_lookup_variants(body.customer_phone)
            ),
        ).order_by(Conversation.updated_at.desc())
    )
    conversation = result.scalars().first()
    if conversation is None:
        raise HTTPException(status_code=500, detail="Conversation was not persisted")
    if reply is None:
        return SimulateResponse(
            conversation_id=conversation.id,
            reply="",
            images=[],
        )
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
    "/{conversation_id}",
    response_model=ConversationListItem,
)
async def get_merchant_conversation(
    conversation_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> ConversationListItem:
    try:
        await obtenir_conversation_commercant(db, merchant.id, conversation_id)
    except NotFoundError as exc:
        raise _not_found_conversation() from exc
    rows = await lister_conversations_commercant(db, merchant.id)
    for row in rows:
        if row["id"] == conversation_id:
            return ConversationListItem.model_validate(row)
    raise _not_found_conversation()


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
    images = await lister_images_par_messages(db, [message.id for message in messages])
    names = await noms_produits(
        db,
        merchant.id,
        [
            image.matched_product_id
            for image in images.values()
            if image.matched_product_id is not None
        ],
    )
    citations = await citations_pour_messages(
        db, conversation_id, [message.id for message in messages]
    )
    out: list[MerchantMessageOut] = []
    for message in messages:
        image = images.get(message.id)
        image_out = None
        if image is not None:
            image_out = _message_image_out(image, names)
        quoted_info = citations.get(message.id)
        out.append(
            MerchantMessageOut(
                id=message.id,
                turn_role=message.turn_role,
                display_text=message.display_text,
                created_at=message.created_at,
                image=image_out,
                quoted=(
                    _quoted_message_out(quoted_info)
                    if quoted_info is not None
                    else None
                ),
            )
        )
    return out


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
            db, merchant, conversation_id, body.message
        )
    except NotFoundError as exc:
        raise _not_found_conversation() from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WhatsAppSendError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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
