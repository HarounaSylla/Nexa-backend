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

from app.catalogue.models import Merchant
from app.catalogue.service import chemin_photo_locale
from app.core.config import settings
from app.core.phone import normalize_phone_webhook, try_normalize_phone

logger = logging.getLogger(__name__)

WAMID_LOG_MAX = 20

FALLBACK_REPLY = (
    "Désolé, un souci technique — réessayez dans un instant."
)
SEEN_KEY_PREFIX = "whatsapp:seen:"
SEEN_TTL_SECONDS = 48 * 60 * 60


def truncate_wamid(wamid: str | None) -> str:
    """Short id for logs. Never pass message text through here."""
    if not wamid:
        return ""
    if len(wamid) <= WAMID_LOG_MAX:
        return wamid
    return wamid[:WAMID_LOG_MAX] + "…"


def wamid_from_graph_payload(payload: Any) -> str | None:
    """messages[0].id from a Graph send response. Missing/malformed → None."""
    if not isinstance(payload, dict):
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    raw = first.get("id")
    if not raw:
        return None
    return str(raw)


def context_reply_to_id(message: dict[str, Any]) -> str | None:
    """Inbound quoted-message wamid. Ignores forwarded / referred_product."""
    context = message.get("context")
    if not isinstance(context, dict):
        return None
    raw = context.get("id")
    if not raw:
        return None
    return str(raw)


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
                    if message_type != "image":
                        logger.info(
                            "Ignoring non-text WhatsApp message type=%s id=%s",
                            message_type,
                            truncate_wamid(str(message.get("id") or "")),
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
                        truncate_wamid(message_id),
                        customer_phone,
                    )
                    continue
                row = {
                    "message_id": message_id,
                    "customer_phone": normalize_phone_webhook(customer_phone),
                    "message_text": body,
                    "phone_number_id": phone_number_id,
                }
                reply_to = context_reply_to_id(message)
                if reply_to:
                    row["reply_to_message_id"] = reply_to
                found.append(row)
    return found


def extract_image_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """Pull inbound image messages from a Cloud API webhook body."""
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
                if message.get("type") != "image":
                    continue
                image_obj = message.get("image") or {}
                if not isinstance(image_obj, dict):
                    continue
                media_id = str(image_obj.get("id") or "")
                mime_type = str(image_obj.get("mime_type") or "")
                caption = image_obj.get("caption")
                caption_text = str(caption) if caption is not None else ""
                message_id = str(message.get("id") or "")
                customer_phone = str(message.get("from") or "")
                if not message_id or not customer_phone or not media_id:
                    logger.warning(
                        "Skipping incomplete image message id=%s from=%s",
                        truncate_wamid(str(message.get("id") or "")),
                        message.get("from"),
                    )
                    continue
                row = {
                    "customer_phone": normalize_phone_webhook(customer_phone),
                    "whatsapp_message_id": message_id,
                    "media_id": media_id,
                    "mime_type": mime_type,
                    "caption": caption_text,
                    "phone_number_id": phone_number_id,
                }
                reply_to = context_reply_to_id(message)
                if reply_to:
                    row["reply_to_message_id"] = reply_to
                found.append(row)
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
    reply_to_message_id: str | None = None,
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
        reply_to_message_id,
    )


def enqueue_inbound_image(
    customer_phone: str,
    whatsapp_message_id: str,
    media_id: str,
    mime_type: str,
    caption: str,
    phone_number_id: str,
    reply_to_message_id: str | None = None,
) -> None:
    from redis import Redis
    from rq import Queue

    from app.workers.whatsapp import process_inbound_whatsapp_image

    queue = Queue("whatsapp", connection=Redis.from_url(settings.redis_url))
    queue.enqueue(
        process_inbound_whatsapp_image,
        customer_phone,
        whatsapp_message_id,
        media_id,
        mime_type,
        caption,
        phone_number_id,
        reply_to_message_id,
    )


async def telecharger_media_whatsapp(media_id: str) -> tuple[bytes, str]:
    """Download inbound media bytes from Graph. Never logs the bytes."""
    if not settings.whatsapp_access_token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is empty")
    if not media_id:
        raise RuntimeError("media_id is empty")
    meta_url = (
        f"https://graph.facebook.com/{settings.whatsapp_api_version}/{media_id}"
    )
    headers = {"Authorization": f"Bearer {settings.whatsapp_access_token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        meta = await client.get(meta_url, headers=headers)
        try:
            meta.raise_for_status()
        except httpx.HTTPStatusError:
            logger.exception(
                "WhatsApp media metadata failed media_id=%s status=%s",
                media_id,
                meta.status_code,
            )
            raise
        payload = meta.json() if meta.content else {}
        url = ""
        mime_type = ""
        if isinstance(payload, dict):
            url = str(payload.get("url") or "")
            mime_type = str(payload.get("mime_type") or "")
        if not url:
            raise RuntimeError("WhatsApp media metadata returned no url")
        download = await client.get(url, headers=headers)
        try:
            download.raise_for_status()
        except httpx.HTTPStatusError:
            logger.exception(
                "WhatsApp media download failed media_id=%s status=%s",
                media_id,
                download.status_code,
            )
            raise
        return download.content, mime_type


async def envoyer_texte_whatsapp(
    customer_phone: str,
    body: str,
    phone_number_id: str,
) -> str | None:
    """Send a text message from the given Cloud API phone number id.

    Returns the Graph wamid, or None if the response omitted it.
    """
    if not settings.whatsapp_access_token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is empty")
    if not phone_number_id:
        raise RuntimeError("phone_number_id is empty")
    to = try_normalize_phone(customer_phone)
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
        try:
            payload = response.json()
        except ValueError:
            return None
        return wamid_from_graph_payload(payload)


GRAPH_WINDOW_CLOSED_CODE = 131047


class WhatsAppSendError(Exception):
    """code is one of: whatsapp_not_configured, whatsapp_window_closed, whatsapp_send_failed"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _graph_error_code(response: httpx.Response) -> int | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    try:
        return int(error["code"])
    except (KeyError, TypeError, ValueError):
        return None


async def envoyer_message_commercant(
    merchant: Merchant, customer_phone: str, text: str
) -> str | None:
    """Send a merchant-authored text to a customer on WhatsApp."""
    phone_number_id = (merchant.whatsapp_phone_number_id or "").strip()
    if not phone_number_id or not settings.whatsapp_access_token:
        raise WhatsAppSendError("whatsapp_not_configured")
    try:
        return await envoyer_texte_whatsapp(customer_phone, text, phone_number_id)
    except RuntimeError as exc:
        raise WhatsAppSendError("whatsapp_not_configured") from exc
    except httpx.HTTPStatusError as exc:
        if _graph_error_code(exc.response) == GRAPH_WINDOW_CLOSED_CODE:
            raise WhatsAppSendError("whatsapp_window_closed") from exc
        raise WhatsAppSendError("whatsapp_send_failed") from exc
    except httpx.RequestError as exc:
        raise WhatsAppSendError("whatsapp_send_failed") from exc


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
) -> str | None:
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

        to = try_normalize_phone(customer_phone)
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
                return None
            try:
                payload = sent.json()
            except ValueError:
                return None
            return wamid_from_graph_payload(payload)
    except Exception:
        logger.exception(
            "WhatsApp image send failed product=%s", product_id
        )
        return None
