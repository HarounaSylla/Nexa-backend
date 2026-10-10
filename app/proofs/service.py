"""Inbound WhatsApp image pipeline."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.handover import (
    KIND_CUSTOMER_PHOTO,
    KIND_SHOP_TEXT,
    PHOTO_RECOGNITION_POSSIBLE,
    PHOTO_RECOGNITION_RECOGNIZED,
    PHOTO_RECOGNITION_UNANALYSED,
    PHOTO_RECOGNITION_UNRECOGNIZED,
    item_automatic_proof_ack,
    item_customer_photo,
)
from app.agent.models import Conversation, Message
from app.agent.orchestrator import _get_or_create_conversation, traiter_photo_produit
from app.agent.service import (
    STATUS_CLOSED,
    STATUS_ESCALATED,
    TURN_ROLE_CUSTOMER,
    TURN_ROLE_MERCHANT,
    notifier_message_escalade,
    record_whatsapp_message_ref,
    trouver_conversation_escaladee,
)
from app.catalogue.models import Merchant, Product
from app.core.formatting import format_fcfa
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
    MATCH_LEVEL_POSSIBLE,
    MATCH_LEVEL_STRONG,
    InboundImage,
)
from app.catalogue.service import ImageMatch
from app.proofs.recognition import (
    PROPOSAL_KIND_EXACT,
    PROPOSAL_KIND_SIMILAR,
    RecognitionResult,
    build_product_photo_developer_item,
    candidates_tool_payload,
    persist_recognition,
    proposal_set,
    recognise_product_photo,
)
from app.proofs.storage import (
    RejectedMediaError,
    delete_inbound_image_file,
    read_inbound_image_file,
    save_inbound_image,
)
from app.proofs.vision import (
    IMAGE_KIND_PRODUCT_PHOTO,
    VERDICT_SIMILAR,
    ImageAnalysis,
    ImageAnalysisError,
    classer_image_entrante,
)
from app.whatsapp.service import (
    WhatsAppSendError,
    envoyer_message_commercant,
    telecharger_media_whatsapp,
    truncate_wamid,
)

logger = logging.getLogger(__name__)

MAX_IMAGE_ANALYSES_PER_PHONE_PER_DAY = 10
_SIMILAR_LEVELS = {MATCH_LEVEL_POSSIBLE, "similar", VERDICT_SIMILAR}


def _recognition_from_inbound_image(
    image: InboundImage,
    product: Product | None,
    merchant_id: uuid.UUID,
) -> dict[str, Any]:
    if image.classification != CLASSIFICATION_PRODUCT_PHOTO:
        return {"state": PHOTO_RECOGNITION_UNANALYSED}
    level = (image.match_level or "").strip().lower()
    if level in {"", MATCH_LEVEL_ERROR}:
        return {"state": PHOTO_RECOGNITION_UNANALYSED}
    product_id = image.matched_product_id
    product_name = None
    if product_id is not None:
        if product is None or product.merchant_id != merchant_id:
            product_id = None
        else:
            product_name = product.name
    if level == MATCH_LEVEL_NONE or not product_name:
        return {"state": PHOTO_RECOGNITION_UNRECOGNIZED}
    if level in _SIMILAR_LEVELS:
        state = PHOTO_RECOGNITION_POSSIBLE
    elif level == MATCH_LEVEL_STRONG:
        state = PHOTO_RECOGNITION_RECOGNIZED
    else:
        return {"state": PHOTO_RECOGNITION_UNANALYSED}
    return {
        "state": state,
        "product_id": product_id,
        "product_name": product_name,
    }


async def quoted_customer_photo_recognitions(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    wamids: list[str],
) -> dict[str, dict[str, Any]]:
    """Recognition for many customer-photo wamids, scoped to this merchant.

    One inbound-image query and one product query. Never includes URLs,
    paths, or bytes. Missing wamids are omitted.
    """
    wanted = [item for item in dict.fromkeys(wamids) if item]
    if not wanted:
        return {}
    images = list(
        (
            await db.execute(
                select(InboundImage).where(
                    InboundImage.whatsapp_message_id.in_(wanted),
                    InboundImage.merchant_id == merchant_id,
                )
            )
        ).scalars().all()
    )
    product_ids = [
        image.matched_product_id
        for image in images
        if image.matched_product_id is not None
    ]
    products: dict[uuid.UUID, Product] = {}
    if product_ids:
        rows = await db.execute(select(Product).where(Product.id.in_(product_ids)))
        products = {row.id: row for row in rows.scalars().all()}
    out: dict[str, dict[str, Any]] = {}
    for image in images:
        product = (
            products.get(image.matched_product_id)
            if image.matched_product_id is not None
            else None
        )
        out[image.whatsapp_message_id] = _recognition_from_inbound_image(
            image, product, merchant_id
        )
    return out


async def quoted_customer_photo_recognition(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    wamid: str,
) -> dict[str, Any] | None:
    """Recognition stored for a customer-photo wamid, scoped to this merchant.

    Never includes URLs, paths, or bytes. None if there is no inbound row.
    """
    found = await quoted_customer_photo_recognitions(db, merchant_id, [wamid])
    return found.get(wamid)


PHOTO_ANALYSE_LOOKBACK = timedelta(hours=24)
ACK_TEXT = (
    "Merci, nous avons bien reçu votre photo. "
    "La boutique va vérifier votre paiement."
)


def _format_stage_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def log_photo_analysis_timing(
    *,
    source: str,
    image_id: uuid.UUID,
    merchant_id: uuid.UUID,
    classifier_s: float | None,
    voyage_s: float | None,
    verifier_s: float | None,
    total_s: float,
    classification: str,
    level: str | None,
) -> None:
    """INFO timings and counts only — never paths, bytes, or URLs."""
    logger.info(
        "Photo analysis source=%s image_id=%s merchant_id=%s "
        "classifier_s=%s voyage_s=%s verifier_s=%s total_s=%.2f "
        "classification=%s level=%s",
        source,
        image_id,
        merchant_id,
        _format_stage_seconds(classifier_s),
        _format_stage_seconds(voyage_s),
        _format_stage_seconds(verifier_s),
        total_s,
        classification,
        level or "-",
    )


def _classification_from_analysis(analysis: ImageAnalysis) -> str:
    if analysis.image_kind == IMAGE_KIND_PRODUCT_PHOTO:
        return CLASSIFICATION_PRODUCT_PHOTO
    if analysis.is_payment_proof:
        return CLASSIFICATION_PAYMENT_PROOF
    return CLASSIFICATION_OTHER


def _product_photo_tool_payload(
    status: str,
    result: RecognitionResult,
) -> dict[str, Any]:
    return {
        "status": status,
        "level": result.level,
        "proposal_kind": result.proposal_kind,
        "candidates": candidates_tool_payload(result.proposed),
    }


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
    reply_to_message_id: str | None = None,
    defer_agent: bool = False,
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
    classifier_s: float | None = None
    vision_started = time.perf_counter()
    if run_vision:
        try:
            started_classifier = time.perf_counter()
            analysis = await classer_image_entrante(
                content, resolved_mime, caption
            )
            classifier_s = time.perf_counter() - started_classifier
            classification = _classification_from_analysis(analysis)
            detected_amount = analysis.detected_amount
        except ImageAnalysisError:
            classifier_s = time.perf_counter() - started_classifier
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
    await record_whatsapp_message_ref(
        db,
        conversation.id,
        whatsapp_message_id,
        KIND_CUSTOMER_PHOTO,
        message_id=row.id,
        reply_to_wamid=reply_to_message_id,
        commit=False,
    )
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
            db, merchant.id, content, resolved_mime, caption=caption
        )
        persist_recognition(image, recognition)
        if recognition.level in {MATCH_LEVEL_NONE, MATCH_LEVEL_ERROR}:
            await _emit_unrecognized_product_photo(
                db,
                merchant_id=merchant.id,
                conversation_id=conversation.id,
                phone=customer_phone,
            )
    if run_vision:
        log_photo_analysis_timing(
            source="live",
            image_id=image.id,
            merchant_id=merchant.id,
            classifier_s=classifier_s,
            voyage_s=recognition.voyage_seconds if recognition else None,
            verifier_s=recognition.verifier_seconds if recognition else None,
            total_s=time.perf_counter() - vision_started,
            classification=classification,
            level=recognition.level if recognition else None,
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
            ack_wamid = await envoyer_message_commercant(
                sender, customer_phone, ACK_TEXT
            )
        except WhatsAppSendError:
            logger.warning(
                "Acknowledgement send failed message_id=%s",
                truncate_wamid(whatsapp_message_id),
                exc_info=True,
            )
        else:
            conversation_row = await db.get(Conversation, conversation_id)
            if conversation_row is not None:
                ack_row = Message(
                    conversation_id=conversation_id,
                    turn_role=TURN_ROLE_MERCHANT,
                    display_text=ACK_TEXT,
                    items=[item_automatic_proof_ack()],
                )
                db.add(ack_row)
                await db.flush()
                await record_whatsapp_message_ref(
                    db,
                    conversation_id,
                    ack_wamid,
                    KIND_SHOP_TEXT,
                    message_id=ack_row.id,
                    excerpt=ACK_TEXT,
                    commit=False,
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
            image._developer_item = developer_item
            if not defer_agent:
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


async def _latest_analysable_photo(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    now: datetime,
) -> InboundImage | None:
    since = now - PHOTO_ANALYSE_LOOKBACK
    result = await db.execute(
        select(InboundImage)
        .where(
            InboundImage.merchant_id == merchant_id,
            InboundImage.conversation_id == conversation_id,
            InboundImage.created_at >= since,
            InboundImage.media_deleted_at.is_(None),
            InboundImage.media_path.isnot(None),
        )
        .order_by(InboundImage.created_at.desc(), InboundImage.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _products_by_ids(
    db: AsyncSession, product_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Product]:
    if not product_ids:
        return {}
    rows = await db.execute(select(Product).where(Product.id.in_(product_ids)))
    return {row.id: row for row in rows.scalars().all()}


async def _stored_recognition_payload(
    db: AsyncSession, image: InboundImage
) -> dict[str, Any]:
    level = image.match_level or MATCH_LEVEL_NONE
    rows = list(image.match_candidates or [])
    kind = PROPOSAL_KIND_EXACT
    if any(row.get("verdict") == VERDICT_SIMILAR for row in rows):
        kind = PROPOSAL_KIND_SIMILAR
    product_ids: list[uuid.UUID] = []
    for row in rows:
        raw = row.get("product_id")
        if not raw:
            continue
        try:
            product_ids.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    products = await _products_by_ids(db, product_ids)
    matches: list[ImageMatch] = []
    for row in rows:
        raw = row.get("product_id")
        if not raw:
            continue
        try:
            product_id = uuid.UUID(str(raw))
        except ValueError:
            continue
        product = products.get(product_id)
        if product is None:
            continue
        distance = float(row.get("distance") or 0.0)
        matches.append(
            ImageMatch(
                product=product,
                distance=distance,
                in_stock=product.stock_qty > 0,
            )
        )
    if kind == PROPOSAL_KIND_SIMILAR and image.matched_product_id is not None:
        proposed = [
            match
            for match in matches
            if match.product.id == image.matched_product_id
        ]
    elif level in {MATCH_LEVEL_STRONG, MATCH_LEVEL_POSSIBLE}:
        proposed = proposal_set(level, matches)
        if image.matched_product_id is not None and not proposed:
            proposed = [
                match
                for match in matches
                if match.product.id == image.matched_product_id
            ]
    else:
        proposed = []
    result = RecognitionResult(
        level=level,
        matches=matches,
        proposed=proposed,
        proposal_kind=kind,
    )
    return _product_photo_tool_payload("already_analysed", result)


async def analyser_photo_client(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> dict[str, Any]:
    """Analyse the latest stored customer photo of this conversation on demand.

    Never raises out of the turn. Does not send acks, run payment-proof
    matching, or emit merchant notifications.
    """
    try:
        return await _analyser_photo_client_inner(
            db, merchant_id, conversation_id
        )
    except Exception:
        logger.warning(
            "analyser_photo_client failed merchant_id=%s conversation_id=%s",
            merchant_id,
            conversation_id,
            exc_info=True,
        )
        return {"status": "unavailable"}


async def _analyser_photo_client_inner(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> dict[str, Any]:
    conversation = await db.get(Conversation, conversation_id)
    if conversation is None or conversation.merchant_id != merchant_id:
        return {"status": "no_photo"}
    now = datetime.now(timezone.utc)
    image = await _latest_analysable_photo(
        db, merchant_id, conversation_id, now=now
    )
    if image is None:
        return {"status": "no_photo"}
    if (
        image.classification == CLASSIFICATION_PRODUCT_PHOTO
        and image.match_level is not None
    ):
        return await _stored_recognition_payload(db, image)
    if image.classification != CLASSIFICATION_NOT_ANALYZED:
        return {"status": "not_a_product_photo"}

    analyses_today = await _analyses_in_last_day(
        db, merchant_id, conversation.customer_phone
    )
    if analyses_today >= settings.max_image_analyses_per_phone_per_day:
        return {"status": "cap_reached"}

    if not image.media_path:
        return {"status": "unavailable"}
    try:
        content = read_inbound_image_file(image.media_path)
    except FileNotFoundError:
        logger.warning(
            "analyser_photo_client missing file image_id=%s merchant_id=%s",
            image.id,
            merchant_id,
        )
        return {"status": "unavailable"}

    vision_started = time.perf_counter()
    classifier_s: float | None = None
    recognition: RecognitionResult | None = None
    try:
        started_classifier = time.perf_counter()
        analysis = await classer_image_entrante(
            content, image.mime_type, image.caption
        )
        classifier_s = time.perf_counter() - started_classifier
    except ImageAnalysisError:
        classifier_s = time.perf_counter() - started_classifier
        log_photo_analysis_timing(
            source="analyser_photo_client",
            image_id=image.id,
            merchant_id=merchant_id,
            classifier_s=classifier_s,
            voyage_s=None,
            verifier_s=None,
            total_s=time.perf_counter() - vision_started,
            classification=CLASSIFICATION_UNKNOWN,
            level=None,
        )
        logger.warning(
            "analyser_photo_client vision failed image_id=%s merchant_id=%s",
            image.id,
            merchant_id,
            exc_info=True,
        )
        return {"status": "unavailable"}

    classification = _classification_from_analysis(analysis)
    image.classification = classification
    image.detected_amount = analysis.detected_amount
    if classification != CLASSIFICATION_PRODUCT_PHOTO:
        log_photo_analysis_timing(
            source="analyser_photo_client",
            image_id=image.id,
            merchant_id=merchant_id,
            classifier_s=classifier_s,
            voyage_s=None,
            verifier_s=None,
            total_s=time.perf_counter() - vision_started,
            classification=classification,
            level=None,
        )
        return {"status": "not_a_product_photo"}

    recognition = await recognise_product_photo(
        db, merchant_id, content, image.mime_type, caption=image.caption
    )
    persist_recognition(image, recognition)
    log_photo_analysis_timing(
        source="analyser_photo_client",
        image_id=image.id,
        merchant_id=merchant_id,
        classifier_s=classifier_s,
        voyage_s=recognition.voyage_seconds,
        verifier_s=recognition.verifier_seconds,
        total_s=time.perf_counter() - vision_started,
        classification=classification,
        level=recognition.level,
    )
    return _product_photo_tool_payload("analysed", recognition)


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
