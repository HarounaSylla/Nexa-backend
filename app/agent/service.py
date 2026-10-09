"""Agent-side helpers that are not catalogue or orders concerns."""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.handover import (
    KIND_CUSTOMER_PHOTO,
    KIND_SHOP_TEXT,
    item_human_reply,
    quote_replay_marker,
)
from app.agent.models import Conversation, Message, SentProductImage, WhatsAppMessageRef
from app.catalogue.models import Merchant, Product
from app.notifications.service import (
    NotificationRelatedType,
    NotificationType,
    emit_notification,
    has_unread_notification,
)
from app.orders.service import NotFoundError, phone_lookup_variants
from app.whatsapp.service import envoyer_message_commercant, truncate_wamid

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
STATUS_ESCALATED = "escalated"
STATUS_CLOSED = "closed"
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


async def obtenir_dernier_message_agent(
    db: AsyncSession, merchant_id: uuid.UUID, customer_phone: str
) -> Message | None:
    """The latest agent turn for this merchant + phone (same lookup as simulate)."""
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.merchant_id == merchant_id,
            Conversation.customer_phone.in_(phone_lookup_variants(customer_phone)),
        )
        .order_by(Conversation.updated_at.desc())
    )
    conversation = result.scalars().first()
    if conversation is None:
        return None
    last_agent = await db.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation.id,
            Message.turn_role == TURN_ROLE_AGENT,
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    return last_agent.scalars().first()


async def already_sent_product_ids(
    db: AsyncSession, conversation_id: uuid.UUID, product_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of these products already had their photo sent in this conversation."""
    if not product_ids:
        return set()
    result = await db.execute(
        select(SentProductImage.product_id).where(
            SentProductImage.conversation_id == conversation_id,
            SentProductImage.product_id.in_(product_ids),
        )
    )
    return {row[0] for row in result.all()}


async def record_sent_product_image(
    db: AsyncSession, conversation_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    db.add(
        SentProductImage(conversation_id=conversation_id, product_id=product_id)
    )
    await db.commit()


async def record_whatsapp_message_ref(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    wamid: str | None,
    kind: str,
    *,
    message_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    excerpt: str | None = None,
    reply_to_wamid: str | None = None,
    commit: bool = True,
) -> None:
    """Persist a wamid mapping. Never raises: a missing id must not break a turn."""
    if not wamid or not conversation_id:
        return
    try:
        async with db.begin_nested():
            db.add(
                WhatsAppMessageRef(
                    conversation_id=conversation_id,
                    wamid=wamid,
                    kind=kind,
                    message_id=message_id,
                    product_id=product_id,
                    excerpt=excerpt,
                    reply_to_wamid=reply_to_wamid,
                )
            )
            await db.flush()
        if commit:
            await db.commit()
    except Exception:
        logger.exception(
            "Failed to record WhatsApp message ref wamid=%s conversation_id=%s",
            truncate_wamid(wamid),
            conversation_id,
        )
        if commit:
            try:
                await db.rollback()
            except Exception:
                logger.exception(
                    "Rollback after WhatsApp ref persist failed conversation_id=%s",
                    conversation_id,
                )


async def resolve_quote_marker(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    quoted_wamid: str | None,
) -> dict[str, str] | None:
    """Replay marker for one quoted wamid, or None if it cannot be resolved here.

    Looks up the wamid across conversations of the same merchant and the
    same customer phone. Another customer's or another merchant's row is
    never resolved.
    """
    if not quoted_wamid:
        return None
    current = await db.get(Conversation, conversation_id)
    if current is None:
        return None
    variants = phone_lookup_variants(current.customer_phone)
    result = await db.execute(
        select(WhatsAppMessageRef)
        .join(Conversation, Conversation.id == WhatsAppMessageRef.conversation_id)
        .where(
            WhatsAppMessageRef.wamid == quoted_wamid,
            Conversation.merchant_id == current.merchant_id,
            Conversation.customer_phone.in_(variants),
        )
        .order_by(
            case(
                (WhatsAppMessageRef.conversation_id == conversation_id, 0),
                else_=1,
            ),
            WhatsAppMessageRef.created_at.desc(),
        )
        .limit(1)
    )
    quoted = result.scalar_one_or_none()
    if quoted is None:
        return None
    from_earlier = quoted.conversation_id != conversation_id
    excerpt = quoted.excerpt
    if not excerpt and quoted.message_id is not None:
        message = await db.get(Message, quoted.message_id)
        if message is not None:
            excerpt = message.display_text
    product_name = None
    product_id = quoted.product_id
    photo_recognition = None
    if quoted.kind == KIND_CUSTOMER_PHOTO:
        from app.proofs.service import quoted_customer_photo_recognition

        recognition = await quoted_customer_photo_recognition(
            db, current.merchant_id, quoted_wamid
        )
        if recognition is not None:
            photo_recognition = recognition.get("state")
            if recognition.get("product_name"):
                product_name = recognition["product_name"]
            if recognition.get("product_id") is not None:
                product_id = recognition["product_id"]
    elif product_id is not None:
        product = await db.get(Product, product_id)
        if product is not None:
            product_name = product.name
    return quote_replay_marker(
        kind=quoted.kind,
        excerpt=excerpt,
        product_name=product_name,
        product_id=product_id,
        from_earlier_conversation=from_earlier,
        photo_recognition=photo_recognition,
    )


async def load_quote_replay_markers(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    messages: list[Message],
) -> dict[uuid.UUID, dict[str, str]]:
    """message.id → replay marker, for customer rows that quoted a known wamid."""
    message_ids = [row.id for row in messages]
    if not message_ids:
        return {}
    inbound = list(
        (
            await db.execute(
                select(WhatsAppMessageRef).where(
                    WhatsAppMessageRef.conversation_id == conversation_id,
                    WhatsAppMessageRef.message_id.in_(message_ids),
                    WhatsAppMessageRef.reply_to_wamid.is_not(None),
                )
            )
        ).scalars().all()
    )
    markers: dict[uuid.UUID, dict[str, str]] = {}
    for ref in inbound:
        if ref.message_id is None:
            continue
        marker = await resolve_quote_marker(
            db, conversation_id, ref.reply_to_wamid
        )
        if marker is not None:
            markers[ref.message_id] = marker
    return markers


async def fermer_conversation_si_active(
    db: AsyncSession, conversation_id: uuid.UUID
) -> None:
    """Close an active conversation; no-op if missing, escalated, or already closed."""
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        return
    if conversation.status == STATUS_ACTIVE:
        conversation.status = STATUS_CLOSED


async def trouver_conversation_escaladee(
    db: AsyncSession, merchant_id: uuid.UUID, customer_phone: str
) -> Conversation | None:
    variants = phone_lookup_variants(customer_phone)
    if not variants:
        return None
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.merchant_id == merchant_id,
            Conversation.customer_phone.in_(variants),
            Conversation.status == STATUS_ESCALATED,
        )
        .order_by(Conversation.updated_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def trouver_conversation_ouverte(
    db: AsyncSession, merchant_id: uuid.UUID, customer_phone: str
) -> Conversation | None:
    """The open (escalated, else newest active) conversation for this phone."""
    variants = phone_lookup_variants(customer_phone)
    if not variants:
        return None
    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.merchant_id == merchant_id,
            Conversation.customer_phone.in_(variants),
            Conversation.status.in_([STATUS_ACTIVE, STATUS_ESCALATED]),
        )
        .order_by(
            case((Conversation.status == STATUS_ESCALATED, 0), else_=1),
            Conversation.updated_at.desc(),
            Conversation.id.desc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def notifier_message_escalade(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    conversation: Conversation,
    phone: str,
) -> None:
    """One unread escalated_customer_message per conversation until it is read."""
    if conversation.status != STATUS_ESCALATED:
        return
    if await has_unread_notification(
        db,
        merchant_id=merchant_id,
        notification_type=NotificationType.escalated_customer_message,
        related_id=conversation.id,
    ):
        return
    await emit_notification(
        db,
        merchant_id=merchant_id,
        notification_type=NotificationType.escalated_customer_message,
        related_type=NotificationRelatedType.conversation,
        related_id=conversation.id,
        data={"customer_phone": phone},
    )


async def fermer_conversations_actives_du_client(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    extra_conversation_id: uuid.UUID | None = None,
) -> None:
    """Close active conversations for this merchant+phone. Never touches escalated."""
    ids: set[uuid.UUID] = set()
    if extra_conversation_id is not None:
        ids.add(extra_conversation_id)
    variants = phone_lookup_variants(customer_phone)
    if variants:
        result = await db.execute(
            select(Conversation.id).where(
                Conversation.merchant_id == merchant_id,
                Conversation.customer_phone.in_(variants),
                Conversation.status == STATUS_ACTIVE,
            )
        )
        ids.update(result.scalars().all())
    for conversation_id in ids:
        await fermer_conversation_si_active(db, conversation_id)


async def fermer_conversations_inactives(
    db: AsyncSession, inactivity_timeout: timedelta
) -> int:
    """Close active conversations idle longer than `inactivity_timeout`.

    Returns the number of conversations closed. Escalated and already
    closed conversations are never touched.
    """
    result = await db.execute(
        update(Conversation)
        .where(
            Conversation.status == STATUS_ACTIVE,
            Conversation.updated_at < func.now() - inactivity_timeout,
        )
        .values(
            status=STATUS_CLOSED,
            # Keep the original idle timestamp. Conversation.updated_at has
            # onupdate=func.now(); a naive UPDATE would look "just closed"
            # and the 15-minute reopen grace would put the customer back
            # into a 48h+ stale thread.
            updated_at=Conversation.updated_at,
        )
    )
    await db.commit()
    return result.rowcount or 0


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
                "orders": [],
            }
        )
    from app.orders.service import lister_commandes_par_conversations

    by_conversation = await lister_commandes_par_conversations(
        db, merchant_id, ids
    )
    for row in rows:
        orders = by_conversation.get(row["id"], [])
        row["orders"] = [
            {
                "id": order.id,
                "order_number": order.order_number,
                "status": order.status.value,
                "payment_status": order.payment_status.value,
            }
            for order in orders
        ]
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


async def enregistrer_message_commercant(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    text: str,
    items: list | None = None,
) -> Message | None:
    """Persist a merchant message already delivered. Does not commit.

    `items` is the replayable Responses API input for the agent. Pass a
    handover builder (never a raw URL). `display_text` stays the WhatsApp
    text the customer actually received.
    """
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None:
        return None
    row = Message(
        conversation_id=conversation.id,
        turn_role=TURN_ROLE_MERCHANT,
        display_text=text,
        items=list(items or []),
    )
    db.add(row)
    conversation.updated_at = datetime.now(timezone.utc)
    await db.flush()
    return row


async def repondre_en_humain(
    db: AsyncSession,
    merchant: Merchant,
    conversation_id: uuid.UUID,
    message: str,
) -> Message:
    """Send a merchant reply on WhatsApp, then store it. Does not call the agent."""
    merchant_id = merchant.id
    sender = Merchant(
        whatsapp_phone_number_id=merchant.whatsapp_phone_number_id
    )
    conversation = await _owned_conversation(db, merchant_id, conversation_id)
    text = message.strip()
    if not text:
        raise ValueError("message must not be empty")
    customer_phone = conversation.customer_phone
    await db.rollback()
    wamid = await envoyer_message_commercant(sender, customer_phone, text)
    conversation = await _owned_conversation(db, merchant_id, conversation_id)
    row = Message(
        conversation_id=conversation.id,
        turn_role=TURN_ROLE_MERCHANT,
        display_text=text,
        items=[item_human_reply(text)],
    )
    db.add(row)
    await db.flush()
    await record_whatsapp_message_ref(
        db,
        conversation.id,
        wamid,
        KIND_SHOP_TEXT,
        message_id=row.id,
        excerpt=text,
        commit=False,
    )
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
