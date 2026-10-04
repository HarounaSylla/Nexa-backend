"""Inbound WhatsApp image pipeline. Never calls the sales agent."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import Conversation, Message
from app.agent.orchestrator import _get_or_create_conversation
from app.agent.service import (
    STATUS_CLOSED,
    TURN_ROLE_CUSTOMER,
    TURN_ROLE_MERCHANT,
)
from app.catalogue.models import Merchant
from app.notifications.models import NotificationRelatedType, NotificationType
from app.notifications.service import emit_notification
from app.orders.models import Order
from app.orders.service import (
    appliquer_preuve_recue,
    commandes_en_attente_de_preuve,
    phone_lookup_variants,
)
from app.proofs.matching import choisir_commande_pour_preuve
from app.proofs.models import (
    CLASSIFICATION_NOT_ANALYZED,
    CLASSIFICATION_OTHER,
    CLASSIFICATION_PAYMENT_PROOF,
    CLASSIFICATION_UNKNOWN,
    InboundImage,
)
from app.proofs.storage import (
    RejectedMediaError,
    save_inbound_image,
)
from app.proofs.vision import ImageAnalysis, ImageAnalysisError, classer_image_entrante
from app.whatsapp.service import (
    WhatsAppSendError,
    envoyer_message_commercant,
    telecharger_media_whatsapp,
)

logger = logging.getLogger(__name__)

MAX_IMAGE_ANALYSES_PER_PHONE_PER_DAY = 5
ACK_TEXT = (
    "Merci, nous avons bien reçu votre photo. "
    "La boutique va vérifier votre paiement."
)


async def lister_preuves_commande(
    db: AsyncSession, order_id: uuid.UUID
) -> list[InboundImage]:
    result = await db.execute(
        select(InboundImage)
        .where(InboundImage.order_id == order_id)
        .order_by(InboundImage.created_at.asc(), InboundImage.id.asc())
    )
    return list(result.scalars().all())


async def lister_images_par_messages(
    db: AsyncSession, message_ids: list[uuid.UUID]
) -> dict[uuid.UUID, InboundImage]:
    if not message_ids:
        return {}
    result = await db.execute(
        select(InboundImage).where(InboundImage.message_id.in_(message_ids))
    )
    return {
        image.message_id: image
        for image in result.scalars().all()
        if image.message_id is not None
    }


async def obtenir_image_commercant(
    db: AsyncSession, merchant_id: uuid.UUID, image_id: uuid.UUID
) -> InboundImage:
    image = await db.get(InboundImage, image_id)
    if image is None or image.merchant_id != merchant_id:
        from app.orders.service import NotFoundError

        raise NotFoundError(f"Image {image_id} was not found")
    return image


async def _already_stored(
    db: AsyncSession, whatsapp_message_id: str
) -> InboundImage | None:
    result = await db.execute(
        select(InboundImage).where(
            InboundImage.whatsapp_message_id == whatsapp_message_id
        )
    )
    return result.scalar_one_or_none()


async def _analyses_in_last_day(
    db: AsyncSession, merchant_id: uuid.UUID, customer_phone: str
) -> int:
    variants = phone_lookup_variants(customer_phone)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    result = await db.execute(
        select(InboundImage)
        .join(Conversation, Conversation.id == InboundImage.conversation_id)
        .where(
            InboundImage.merchant_id == merchant_id,
            Conversation.customer_phone.in_(variants),
            InboundImage.created_at >= since,
            InboundImage.classification != CLASSIFICATION_NOT_ANALYZED,
        )
    )
    return len(list(result.scalars().all()))


def _notification_copy(
    *,
    classification: str,
    phone: str,
    order: Order | None,
) -> tuple[NotificationRelatedType, dict[str, object]]:
    if classification == CLASSIFICATION_UNKNOWN:
        title = f"Photo reçue de {phone} (non analysée) : à vérifier."
        related = (
            NotificationRelatedType.order
            if order is not None
            else NotificationRelatedType.conversation
        )
        data: dict[str, object] = {
            "title": title,
            "body": title,
            "customer_phone": phone,
        }
        if order is not None:
            data["order_number"] = order.order_number
        return related, data
    if order is not None:
        title = (
            f"Preuve de paiement reçue — commande #{order.order_number} "
            ": à vérifier."
        )
        return NotificationRelatedType.order, {
            "title": title,
            "body": title,
            "customer_phone": phone,
            "order_number": order.order_number,
        }
    title = (
        f"Photo reçue de {phone} : peut-être une preuve de paiement, "
        "commande à identifier."
    )
    return NotificationRelatedType.conversation, {
        "title": title,
        "body": title,
        "customer_phone": phone,
    }


async def _choose_conversation(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    matched: Order | None,
) -> tuple[Conversation, bool]:
    """Return (conversation, bump_updated_at)."""
    if matched is not None and matched.conversation_id is not None:
        conversation = await db.get(Conversation, matched.conversation_id)
        if conversation is not None:
            bump = conversation.status != STATUS_CLOSED
            return conversation, bump
    conversation = await _get_or_create_conversation(db, merchant_id, customer_phone)
    return conversation, True


async def traiter_image_entrante(
    db: AsyncSession,
    merchant: Merchant,
    customer_phone: str,
    whatsapp_message_id: str,
    media_id: str,
    mime_type: str | None,
    caption: str | None,
) -> InboundImage | None:
    existing = await _already_stored(db, whatsapp_message_id)
    if existing is not None:
        return existing

    try:
        content, downloaded_mime = await telecharger_media_whatsapp(media_id)
    except Exception:
        logger.warning(
            "WhatsApp media download failed message_id=%s",
            whatsapp_message_id,
            exc_info=True,
        )
        return None

    resolved_mime = downloaded_mime or mime_type or ""
    try:
        relative_path = save_inbound_image(merchant.id, resolved_mime, content)
    except RejectedMediaError:
        logger.warning(
            "Rejected inbound image message_id=%s mime=%s size=%s",
            whatsapp_message_id,
            resolved_mime,
            len(content),
        )
        return None

    candidates = await commandes_en_attente_de_preuve(
        db, merchant.id, customer_phone
    )
    analyses_today = await _analyses_in_last_day(db, merchant.id, customer_phone)
    at_cap = analyses_today >= MAX_IMAGE_ANALYSES_PER_PHONE_PER_DAY
    run_vision = bool(candidates) and not at_cap

    classification = CLASSIFICATION_NOT_ANALYZED
    detected_amount: Decimal | None = None
    analysis: ImageAnalysis | None = None
    if run_vision:
        try:
            analysis = await classer_image_entrante(
                content, resolved_mime, caption
            )
            if analysis.is_payment_proof:
                classification = CLASSIFICATION_PAYMENT_PROOF
            else:
                classification = CLASSIFICATION_OTHER
            detected_amount = analysis.detected_amount
        except ImageAnalysisError:
            logger.warning(
                "Vision failed message_id=%s; storing as unknown",
                whatsapp_message_id,
                exc_info=True,
            )
            classification = CLASSIFICATION_UNKNOWN

    matched: Order | None = None
    if classification in {CLASSIFICATION_PAYMENT_PROOF, CLASSIFICATION_UNKNOWN}:
        matched = choisir_commande_pour_preuve(candidates, caption)

    conversation, bump = await _choose_conversation(
        db, merchant.id, customer_phone, matched
    )
    display = (caption or "").strip() or "Photo"
    row = Message(
        conversation_id=conversation.id,
        turn_role=TURN_ROLE_CUSTOMER,
        display_text=display,
        items=[],
    )
    db.add(row)
    await db.flush()
    if bump:
        conversation.updated_at = datetime.now(timezone.utc)

    if (
        matched is not None
        and classification == CLASSIFICATION_PAYMENT_PROOF
    ):
        await appliquer_preuve_recue(db, matched.id)

    image = InboundImage(
        merchant_id=merchant.id,
        conversation_id=conversation.id,
        message_id=row.id,
        order_id=matched.id if matched is not None else None,
        whatsapp_message_id=whatsapp_message_id,
        media_path=relative_path,
        mime_type=resolved_mime,
        caption=caption,
        classification=classification,
        detected_amount=detected_amount,
    )
    db.add(image)

    if classification in {CLASSIFICATION_PAYMENT_PROOF, CLASSIFICATION_UNKNOWN}:
        related_type, data = _notification_copy(
            classification=classification,
            phone=customer_phone,
            order=matched,
        )
        related_id = (
            matched.id
            if matched is not None and related_type == NotificationRelatedType.order
            else conversation.id
        )
        await emit_notification(
            db,
            merchant_id=merchant.id,
            notification_type=NotificationType.payment_proof_received,
            related_type=related_type,
            related_id=related_id,
            data=data,
        )

    conversation_id = conversation.id
    merchant_phone_id = merchant.whatsapp_phone_number_id
    await db.commit()
    await db.refresh(image)

    if classification == CLASSIFICATION_PAYMENT_PROOF:
        sender = Merchant(whatsapp_phone_number_id=merchant_phone_id)
        try:
            await envoyer_message_commercant(sender, customer_phone, ACK_TEXT)
        except WhatsAppSendError:
            logger.warning(
                "Acknowledgement send failed message_id=%s",
                whatsapp_message_id,
                exc_info=True,
            )
        else:
            conversation_row = await db.get(Conversation, conversation_id)
            if conversation_row is not None:
                db.add(
                    Message(
                        conversation_id=conversation_id,
                        turn_role=TURN_ROLE_MERCHANT,
                        display_text=ACK_TEXT,
                        items=[],
                    )
                )
                # A closed order thread must not gain reopen-grace from the ack.
                if conversation_row.status != STATUS_CLOSED:
                    conversation_row.updated_at = datetime.now(timezone.utc)
                await db.commit()

    return image
