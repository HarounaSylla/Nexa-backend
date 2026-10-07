"""Inbound WhatsApp image pipeline."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.handover import item_automatic_proof_ack, item_customer_photo
from app.agent.models import Conversation, Message
from app.agent.orchestrator import _get_or_create_conversation, traiter_photo_produit
from app.agent.service import (
    STATUS_CLOSED,
    STATUS_ESCALATED,
    TURN_ROLE_CUSTOMER,
    TURN_ROLE_MERCHANT,
    notifier_message_escalade,
    trouver_conversation_escaladee,
)
from app.catalogue.models import Merchant
from app.core.config import settings
from app.core.phone import try_normalize_phone
from app.notifications.models import NotificationRelatedType, NotificationType
from app.notifications.service import emit_notification
from app.orders.models import Order, OrderStatus, PaymentStatus
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
    CLASSIFICATION_PRODUCT_PHOTO,
    CLASSIFICATION_UNKNOWN,
    MATCH_LEVEL_ERROR,
    MATCH_LEVEL_NONE,
    InboundImage,
)
from app.proofs.recognition import (
    RecognitionResult,
    build_product_photo_developer_item,
    persist_recognition,
    recognise_product_photo,
)
from app.proofs.storage import (
    RejectedMediaError,
    delete_inbound_image_file,
    save_inbound_image,
)
from app.proofs.vision import (
    IMAGE_KIND_PRODUCT_PHOTO,
    ImageAnalysis,
    ImageAnalysisError,
    classer_image_entrante,
)
from app.whatsapp.service import (
    WhatsAppSendError,
    envoyer_message_commercant,
    telecharger_media_whatsapp,
)

logger = logging.getLogger(__name__)

MAX_IMAGE_ANALYSES_PER_PHONE_PER_DAY = 10
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
    variants = phone_lookup_variants(try_normalize_phone(customer_phone))
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


async def _emit_unrecognized_product_photo(
    db: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    phone: str,
) -> None:
    title = "Photo de produit non reconnue"
    body = (
        f"Le client {phone} a envoyé une photo de produit que le catalogue "
        "n'a pas reconnue. Une vente est peut-être possible."
    )
    await emit_notification(
        db,
        merchant_id=merchant_id,
        notification_type=NotificationType.product_photo_unrecognized,
        related_type=NotificationRelatedType.conversation,
        related_id=conversation_id,
        data={
            "title": title,
            "body": body,
            "customer_phone": phone,
        },
    )


async def _choose_conversation(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    matched: Order | None,
) -> tuple[Conversation, bool]:
    """Return (conversation, bump_updated_at)."""
    phone = try_normalize_phone(customer_phone)
    escalated = await trouver_conversation_escaladee(db, merchant_id, phone)
    if escalated is not None:
        return escalated, True
    if matched is not None and matched.conversation_id is not None:
        conversation = await db.get(Conversation, matched.conversation_id)
        if conversation is not None:
            bump = conversation.status != STATUS_CLOSED
            return conversation, bump
    conversation = await _get_or_create_conversation(db, merchant_id, phone)
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

    customer_phone = try_normalize_phone(customer_phone)

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
    escalated = await trouver_conversation_escaladee(
        db, merchant.id, customer_phone
    )
    analyses_today = await _analyses_in_last_day(db, merchant.id, customer_phone)
    at_cap = analyses_today >= settings.max_image_analyses_per_phone_per_day
    awaiting_proof = bool(candidates)
    run_vision = not at_cap and not (
        escalated is not None and not awaiting_proof
    )

    classification = CLASSIFICATION_NOT_ANALYZED
    detected_amount: Decimal | None = None
    analysis: ImageAnalysis | None = None
    if run_vision:
        try:
            analysis = await classer_image_entrante(
                content, resolved_mime, caption
            )
            if analysis.image_kind == IMAGE_KIND_PRODUCT_PHOTO:
                classification = CLASSIFICATION_PRODUCT_PHOTO
            elif analysis.is_payment_proof:
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

    skip_recognition = escalated is not None
    matched: Order | None = None
    if classification in {CLASSIFICATION_PAYMENT_PROOF, CLASSIFICATION_UNKNOWN}:
        matched = choisir_commande_pour_preuve(candidates, caption)

    conversation, bump = await _choose_conversation(
        db, merchant.id, customer_phone, matched
    )
    display = (caption or "").strip() or "Photo"
    photo_item = item_customer_photo(
        caption=caption,
        classification=classification,
        order_number=matched.order_number if matched is not None else None,
    )
    row = Message(
        conversation_id=conversation.id,
        turn_role=TURN_ROLE_CUSTOMER,
        display_text=display,
        items=[photo_item],
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

    recognition: RecognitionResult | None = None
    if classification == CLASSIFICATION_PRODUCT_PHOTO and not skip_recognition:
        recognition = await recognise_product_photo(
            db, merchant.id, content, resolved_mime
        )
        persist_recognition(image, recognition)
        if recognition.level in {MATCH_LEVEL_NONE, MATCH_LEVEL_ERROR}:
            await _emit_unrecognized_product_photo(
                db,
                merchant_id=merchant.id,
                conversation_id=conversation.id,
                phone=customer_phone,
            )

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

    if conversation.status == STATUS_ESCALATED:
        await notifier_message_escalade(
            db, merchant.id, conversation, customer_phone
        )

    conversation_id = conversation.id
    merchant_phone_id = merchant.whatsapp_phone_number_id
    merchant_id = merchant.id
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
                        items=[item_automatic_proof_ack()],
                    )
                )
                # A closed order thread must not gain reopen-grace from the ack.
                if conversation_row.status != STATUS_CLOSED:
                    conversation_row.updated_at = datetime.now(timezone.utc)
                await db.commit()

    if (
        classification == CLASSIFICATION_PRODUCT_PHOTO
        and not skip_recognition
        and recognition is not None
    ):
        image._send_agent = True
        try:
            developer_item = build_product_photo_developer_item(
                caption=caption,
                analysis=analysis,
                result=recognition,
            )
            image._agent_reply = await traiter_photo_produit(
                db, merchant_id, customer_phone, developer_item
            )
        except Exception:
            logger.exception(
                "Product-photo agent turn failed message_id=%s",
                whatsapp_message_id,
            )
            image._agent_failed = True

    return image


async def purger_images_expirees(
    db: AsyncSession, now: datetime | None = None, limit: int = 500
) -> int:
    """Delete expired inbound image files. Rows stay; path and timestamp are cleared.

    Eligible when `created_at` is older than the retention window and the
    image has no order, or its order is paid or cancelled. Pending /
    proof_received orders keep their files. One failing file is logged and
    skipped. Returns the number of rows purged.
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = moment - timedelta(days=settings.payment_proof_retention_days)
    result = await db.execute(
        select(InboundImage)
        .outerjoin(Order, Order.id == InboundImage.order_id)
        .where(
            InboundImage.media_deleted_at.is_(None),
            InboundImage.created_at < cutoff,
            or_(
                InboundImage.order_id.is_(None),
                Order.payment_status == PaymentStatus.paid,
                Order.status == OrderStatus.cancelled,
            ),
        )
        .order_by(InboundImage.created_at.asc(), InboundImage.id.asc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    ids = [row.id for row in rows]
    purged = 0
    for image_id in ids:
        image = await db.get(InboundImage, image_id)
        if image is None or image.media_deleted_at is not None:
            continue
        try:
            delete_inbound_image_file(image.media_path)
            image.media_path = None
            image.media_deleted_at = moment
            await db.commit()
            purged += 1
        except Exception:
            await db.rollback()
            logger.warning(
                "Failed to purge inbound image %s",
                image_id,
                exc_info=True,
            )
    return purged
