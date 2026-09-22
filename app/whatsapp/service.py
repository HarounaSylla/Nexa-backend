"""WhatsApp Cloud API helpers: signature check, payload parse, send.

The webhook handler stays thin: verify, parse, enqueue. The worker calls
`traiter_message_entrant` and then `envoyer_texte_whatsapp` / image send.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from typing import Any

import httpx

from app.catalogue.service import chemin_photo_locale
from app.core.config import settings

logger = logging.getLogger(__name__)

FALLBACK_REPLY = (
    "Désolé, un souci technique — réessayez dans un instant."
)
SEEN_KEY_PREFIX = "whatsapp:seen:"
SEEN_TTL_SECONDS = 48 * 60 * 60


def signature_verification_enabled() -> bool:
    return bool(settings.whatsapp_app_secret)


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Validate X-Hub-Signature-256 (sha256=<hex>) against the app secret."""
    secret = settings.whatsapp_app_secret
    if not secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    provided = header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def extract_text_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Pull inbound text messages from a Cloud API webhook body.

    Status / delivery events are ignored. Non-text inbound messages are
    logged and skipped (no media handling in this slice).
    """
    found: list[dict[str, str]] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") or {}
            phone_number_id = ""
            if isinstance(metadata, dict):
                phone_number_id = str(metadata.get("phone_number_id") or "")
            for message in value.get("messages") or []:
                if not isinstance(message, dict):
                    continue
                message_type = message.get("type")
                if message_type != "text":
                    logger.info(
                        "Ignoring non-text WhatsApp message type=%s id=%s",
                        message_type,
                        message.get("id"),
                    )
                    continue
                text_obj = message.get("text") or {}
                body = ""
                if isinstance(text_obj, dict):
                    body = str(text_obj.get("body") or "")
                message_id = str(message.get("id") or "")
                customer_phone = str(message.get("from") or "")
                if not message_id or not customer_phone or not body:
                    logger.warning(
                        "Skipping incomplete text message id=%s from=%s",
                        message_id,
                        customer_phone,
                    )
                    continue
                found.append(
                    {
                        "message_id": message_id,
                        "customer_phone": customer_phone,
                        "message_text": body,
                        "phone_number_id": phone_number_id,
                    }
                )
    return found


def claim_inbound_message(message_id: str) -> bool:
    """SETNX a Redis seen-key. True if this delivery should be processed."""
    from redis import Redis

    redis = Redis.from_url(settings.redis_url)
    return bool(
        redis.set(
            f"{SEEN_KEY_PREFIX}{message_id}",
            "1",
            nx=True,
            ex=SEEN_TTL_SECONDS,
        )
    )


def enqueue_inbound_text(
    message_id: str,
    customer_phone: str,
    message_text: str,
    phone_number_id: str,
) -> None:
    from redis import Redis
    from rq import Queue

    from app.workers.whatsapp import process_inbound_whatsapp_text

    queue = Queue("whatsapp", connection=Redis.from_url(settings.redis_url))
    queue.enqueue(
        process_inbound_whatsapp_text,
        message_id,
        customer_phone,
        message_text,
        phone_number_id,
    )


async def envoyer_texte_whatsapp(
    customer_phone: str,
    body: str,
    phone_number_id: str,
) -> None:
    """Send a text message from the given Cloud API phone number id."""
    if not settings.whatsapp_access_token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is empty")
    if not phone_number_id:
        raise RuntimeError("phone_number_id is empty")
    to = customer_phone.lstrip("+")
    url = (
        f"https://graph.facebook.com/{settings.whatsapp_api_version}"
        f"/{phone_number_id}/messages"
    )
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.whatsapp_access_token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            logger.exception(
                "WhatsApp send failed status=%s body=%s",
                response.status_code,
                response.text,
            )
            raise


_IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


async def envoyer_image_whatsapp(
    customer_phone: str,
    product_id: uuid.UUID,
    phone_number_id: str,
    caption: str | None = None,
) -> None:
    """Upload a local product photo and send it as a WhatsApp image (media_id).

    Failures are logged and swallowed so a missing photo never blocks the
    text reply already sent.
    """
    try:
        path = chemin_photo_locale(product_id)
        if path is None or not path.is_file():
            logger.warning(
                "No local photo on disk for product %s; skipping WhatsApp image",
                product_id,
            )
            return
        content_type = _IMAGE_CONTENT_TYPES.get(path.suffix.lower())
        if content_type is None:
            logger.warning(
                "Unsupported photo suffix %s for product %s; skipping",
                path.suffix,
                product_id,
            )
            return
        if not settings.whatsapp_access_token:
            raise RuntimeError("WHATSAPP_ACCESS_TOKEN is empty")
        if not phone_number_id:
            raise RuntimeError("phone_number_id is empty")

        to = customer_phone.lstrip("+")
        media_url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{phone_number_id}/media"
        )
        message_url = (
            f"https://graph.facebook.com/{settings.whatsapp_api_version}"
            f"/{phone_number_id}/messages"
        )
        headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
        content = path.read_bytes()
        async with httpx.AsyncClient(timeout=60.0) as client:
            upload = await client.post(
                media_url,
                headers=headers,
                files={"file": (path.name, content, content_type)},
                data={
                    "messaging_product": "whatsapp",
                    "type": content_type,
                },
            )
            try:
                upload.raise_for_status()
            except httpx.HTTPStatusError:
                logger.exception(
                    "WhatsApp media upload failed product=%s status=%s body=%s",
                    product_id,
                    upload.status_code,
                    upload.text,
                )
                return
            media_id = (upload.json() or {}).get("id")
            if not media_id:
                logger.error(
                    "WhatsApp media upload returned no id product=%s body=%s",
                    product_id,
                    upload.text,
                )
                return
            image_payload: dict[str, Any] = {"id": str(media_id)}
            if caption:
                image_payload["caption"] = caption
            sent = await client.post(
                message_url,
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "image",
                    "image": image_payload,
                },
            )
            try:
                sent.raise_for_status()
            except httpx.HTTPStatusError:
                logger.exception(
                    "WhatsApp image send failed product=%s status=%s body=%s",
                    product_id,
                    sent.status_code,
                    sent.text,
                )
                return
    except Exception:
        logger.exception(
            "WhatsApp image send failed product=%s", product_id
        )
