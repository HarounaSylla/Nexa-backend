"""RQ job: one inbound WhatsApp text → existing agent → Graph API reply.

Does not touch `messages` / `conversations` itself. `traiter_message_entrant`
owns that, same as `POST /agent/simulate`.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from app.agent.images import (
    MAX_WHATSAPP_IMAGES,
    PHOTO_STATUS_ALREADY_SENT,
    PHOTO_STATUS_WILL_BE_SENT,
    extract_product_images,
    images_named_in_reply,
    photo_delivery_plan,
    product_descriptions_from_items,
    product_names_from_items,
)
from app.agent.handover import KIND_SHOP_PHOTO, KIND_SHOP_TEXT
from app.agent.orchestrator import traiter_message_entrant
from app.agent.service import (
    already_sent_product_ids,
    obtenir_dernier_message_agent,
    record_sent_product_image,
    record_whatsapp_message_ref,
)
from app.core.db import AsyncSessionLocal, engine
from app.core.phone import try_normalize_phone
from app.merchants.service import get_merchant_by_whatsapp_phone_number_id
from app.whatsapp.service import (
    FALLBACK_REPLY,
    claim_inbound_message,
    envoyer_image_whatsapp,
    envoyer_texte_whatsapp,
    truncate_wamid,
)

logger = logging.getLogger(__name__)


async def _record_outbound_ref(
    conversation_id: uuid.UUID | None,
    wamid: str | None,
    kind: str,
    *,
    message_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    excerpt: str | None = None,
) -> None:
    if conversation_id is None or not wamid:
        return
    try:
        async with AsyncSessionLocal() as db:
            await record_whatsapp_message_ref(
                db,
                conversation_id,
                wamid,
                kind,
                message_id=message_id,
                product_id=product_id,
                excerpt=excerpt,
            )
    except Exception:
        logger.exception(
            "Outbound WhatsApp ref persist failed wamid=%s",
            truncate_wamid(wamid),
        )


async def envoyer_reponse_whatsapp_agent(
    customer_phone: str,
    reply: str,
    phone_number_id: str,
    merchant_id: uuid.UUID,
    message_id: str,
) -> None:
    """Send the agent text then product photos (shared by text and image jobs)."""
    images = []
    names = {}
    descriptions = {}
    conversation_id = None
    agent_message_id = None
    already_sent: set[uuid.UUID] = set()
    async with AsyncSessionLocal() as db:
        last = await obtenir_dernier_message_agent(
            db, merchant_id, customer_phone
        )
        if last is not None:
            images = extract_product_images(last.items)
            names = product_names_from_items(last.items)
            descriptions = product_descriptions_from_items(last.items)
            conversation_id = last.conversation_id
            agent_message_id = last.id
            already_sent = await already_sent_product_ids(
                db,
                conversation_id,
                [image.product_id for image in images],
            )

    reply_wamid = await envoyer_texte_whatsapp(
        customer_phone, reply, phone_number_id
    )
    await _record_outbound_ref(
        conversation_id,
        reply_wamid,
        KIND_SHOP_TEXT,
        message_id=agent_message_id,
        excerpt=reply,
    )

    tool_image_count = len(images)
    multi_mode = tool_image_count > 1
    named_in_reply = False
    if multi_mode:
        named = images_named_in_reply(images, names, reply)
        if named:
            named_in_reply = True
            logger.info(
                "Filtering multi-product WhatsApp send from %s to %s "
                "message_id=%s",
                tool_image_count,
                len(named),
                truncate_wamid(message_id),
            )
            images = named
    plan = photo_delivery_plan(
        [image.product_id for image in images],
        already_sent,
    )
    if multi_mode:
        cap_logged = False
        for image in images:
            status = plan.get(image.product_id)
            skip_already_sent = status == PHOTO_STATUS_ALREADY_SENT
            send_description = not (named_in_reply and skip_already_sent)
            description = descriptions.get(image.product_id) if send_description else None
            if description:
                desc_wamid = await envoyer_texte_whatsapp(
                    customer_phone, description, phone_number_id
                )
                await _record_outbound_ref(
                    conversation_id,
                    desc_wamid,
                    KIND_SHOP_TEXT,
                    product_id=image.product_id,
                    excerpt=description,
                )
            if skip_already_sent:
                logger.info(
                    "Skipping already-sent product photo product_id=%s "
                    "conversation_id=%s message_id=%s",
                    image.product_id,
                    conversation_id,
                    truncate_wamid(message_id),
                )
                continue
            if status != PHOTO_STATUS_WILL_BE_SENT:
                if not cap_logged:
                    logger.info(
                        "Capping WhatsApp images from %s to %s message_id=%s",
                        len(images),
                        MAX_WHATSAPP_IMAGES,
                        truncate_wamid(message_id),
                    )
                    cap_logged = True
                continue
            photo_wamid = await envoyer_image_whatsapp(
                customer_phone,
                image.product_id,
                phone_number_id,
            )
            await _record_outbound_ref(
                conversation_id,
                photo_wamid,
                KIND_SHOP_PHOTO,
                product_id=image.product_id,
            )
            if conversation_id is not None:
                async with AsyncSessionLocal() as db:
                    await record_sent_product_image(
                        db, conversation_id, image.product_id
                    )
    elif len(images) == 1:
        image = images[0]
        status = plan.get(image.product_id)
        if status == PHOTO_STATUS_WILL_BE_SENT:
            photo_wamid = await envoyer_image_whatsapp(
                customer_phone,
                image.product_id,
                phone_number_id,
                caption=names.get(image.product_id),
            )
            await _record_outbound_ref(
                conversation_id,
                photo_wamid,
                KIND_SHOP_PHOTO,
                product_id=image.product_id,
            )
            if conversation_id is not None:
                async with AsyncSessionLocal() as db:
                    await record_sent_product_image(
                        db, conversation_id, image.product_id
                    )
        elif status == PHOTO_STATUS_ALREADY_SENT:
            logger.info(
                "Skipping already-sent product photo product_id=%s "
                "conversation_id=%s message_id=%s",
                image.product_id,
                conversation_id,
                truncate_wamid(message_id),
            )


def process_inbound_whatsapp_text(
    message_id: str,
    customer_phone: str,
    message_text: str,
    phone_number_id: str,
    reply_to_message_id: str | None = None,
) -> None:
    """RQ entry point. Sync wrapper around the async pipeline."""
    try:
        asyncio.run(
            process_inbound_whatsapp_text_async(
                message_id,
                customer_phone,
                message_text,
                phone_number_id,
                reply_to_message_id,
            )
        )
    except Exception:
        logger.exception(
            "WhatsApp job crashed message_id=%s from=%s",
            truncate_wamid(message_id),
            customer_phone,
        )
        try:
            asyncio.run(
                envoyer_texte_whatsapp(
                    customer_phone, FALLBACK_REPLY, phone_number_id
                )
            )
        except Exception:
            logger.exception(
                "WhatsApp fallback send also failed message_id=%s",
                truncate_wamid(message_id),
            )


async def process_inbound_whatsapp_text_async(
    message_id: str,
    customer_phone: str,
    message_text: str,
    phone_number_id: str,
    reply_to_message_id: str | None = None,
) -> None:
    try:
        if not claim_inbound_message(message_id):
            logger.info(
                "Skipping duplicate WhatsApp message_id=%s",
                truncate_wamid(message_id),
            )
            return

        customer_phone = try_normalize_phone(customer_phone)

        async with AsyncSessionLocal() as db:
            merchant = await get_merchant_by_whatsapp_phone_number_id(
                db, phone_number_id
            )
            if merchant is None:
                logger.warning(
                    "Worker drop message_id=%s: no merchant for phone_number_id=%s",
                    truncate_wamid(message_id),
                    phone_number_id,
                )
                return
            merchant_id = merchant.id
            try:
                reply = await traiter_message_entrant(
                    db,
                    merchant_id,
                    customer_phone,
                    message_text,
                    inbound_whatsapp_message_id=message_id,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception:
                logger.exception(
                    "traiter_message_entrant failed message_id=%s merchant=%s",
                    truncate_wamid(message_id),
                    merchant_id,
                )
                await envoyer_texte_whatsapp(
                    customer_phone, FALLBACK_REPLY, phone_number_id
                )
                return

        if reply is None:
            return

        await envoyer_reponse_whatsapp_agent(
            customer_phone,
            reply,
            phone_number_id,
            merchant_id,
            message_id,
        )
    finally:
        await engine.dispose()


def process_inbound_whatsapp_image(
    customer_phone: str,
    whatsapp_message_id: str,
    media_id: str,
    mime_type: str,
    caption: str,
    phone_number_id: str,
    reply_to_message_id: str | None = None,
) -> None:
    try:
        asyncio.run(
            process_inbound_whatsapp_image_async(
                customer_phone,
                whatsapp_message_id,
                media_id,
                mime_type,
                caption,
                phone_number_id,
                reply_to_message_id,
            )
        )
    except Exception:
        logger.exception(
            "WhatsApp image job crashed message_id=%s from=%s",
            truncate_wamid(whatsapp_message_id),
            customer_phone,
        )
        try:
            asyncio.run(
                envoyer_texte_whatsapp(
                    customer_phone, FALLBACK_REPLY, phone_number_id
                )
            )
        except Exception:
            logger.exception(
                "WhatsApp image fallback send also failed message_id=%s",
                truncate_wamid(whatsapp_message_id),
            )


async def process_inbound_whatsapp_image_async(
    customer_phone: str,
    whatsapp_message_id: str,
    media_id: str,
    mime_type: str,
    caption: str,
    phone_number_id: str,
    reply_to_message_id: str | None = None,
) -> None:
    try:
        if not claim_inbound_message(whatsapp_message_id):
            logger.info(
                "Skipping duplicate WhatsApp image message_id=%s",
                truncate_wamid(whatsapp_message_id),
            )
            return
        customer_phone = try_normalize_phone(customer_phone)
        async with AsyncSessionLocal() as db:
            merchant = await get_merchant_by_whatsapp_phone_number_id(
                db, phone_number_id
            )
            if merchant is None:
                logger.warning(
                    "Worker drop image message_id=%s: no merchant for phone_number_id=%s",
                    truncate_wamid(whatsapp_message_id),
                    phone_number_id,
                )
                return
            merchant_id = merchant.id
            from app.proofs.service import traiter_image_entrante

            try:
                stored = await traiter_image_entrante(
                    db,
                    merchant,
                    customer_phone,
                    whatsapp_message_id,
                    media_id,
                    mime_type or None,
                    caption or None,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception:
                logger.exception(
                    "traiter_image_entrante failed message_id=%s merchant=%s",
                    truncate_wamid(whatsapp_message_id),
                    merchant_id,
                )
                await envoyer_texte_whatsapp(
                    customer_phone, FALLBACK_REPLY, phone_number_id
                )
                return
        if stored is None:
            return
        if not getattr(stored, "_send_agent", False):
            return
        if getattr(stored, "_agent_failed", False) or not getattr(
            stored, "_agent_reply", None
        ):
            await envoyer_texte_whatsapp(
                customer_phone, FALLBACK_REPLY, phone_number_id
            )
            return
        await envoyer_reponse_whatsapp_agent(
            customer_phone,
            stored._agent_reply,
            phone_number_id,
            merchant_id,
            whatsapp_message_id,
        )
    finally:
        await engine.dispose()
