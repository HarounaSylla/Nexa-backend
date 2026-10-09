"""Structured product photos from an agent turn's tool outputs.

`/agent/simulate` and the WhatsApp worker both consume this list. The
chat text never contains URLs.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from typing import Any, Literal

from pydantic import BaseModel

from app.core.formatting import format_fcfa

MAX_WHATSAPP_IMAGES = 3

PhotoStatus = Literal["will_be_sent", "already_sent_earlier", "none"]
PHOTO_STATUS_WILL_BE_SENT: PhotoStatus = "will_be_sent"
PHOTO_STATUS_ALREADY_SENT: PhotoStatus = "already_sent_earlier"
PHOTO_STATUS_NONE: PhotoStatus = "none"


class ProductImageRef(BaseModel):
    product_id: uuid.UUID
    image_url: str


def extract_product_images(items: list[Any] | None) -> list[ProductImageRef]:
    """Collect {product_id, image_url} from tool outputs in an agent turn."""
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


def product_names_from_items(items: list[Any] | None) -> dict[uuid.UUID, str]:
    """Product names already present in the same tool outputs (no extra query)."""
    names: dict[uuid.UUID, str] = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
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
            raw_id = product.get("id") or product.get("product_id")
            name = product.get("name")
            if raw_id and name:
                names[uuid.UUID(str(raw_id))] = str(name)
        raw_id = payload.get("product_id")
        name = payload.get("name")
        if raw_id and name:
            names[uuid.UUID(str(raw_id))] = str(name)
    return names


def product_descriptions_from_items(items: list[Any] | None) -> dict[uuid.UUID, str]:
    """'<name> — <price> FCFA' per product, built from the same tool output
    data already used for extract_product_images / product_names_from_items
    — no extra query, and the price always matches what the model saw.
    """
    descriptions: dict[uuid.UUID, str] = {}
    for item in items or []:
        if not isinstance(item, dict) or item.get("type") != "function_call_output":
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
            raw_id = product.get("id") or product.get("product_id")
            name = product.get("name")
            price = product.get("price")
            if raw_id and name and price is not None:
                descriptions[uuid.UUID(str(raw_id))] = (
                    f"{name} — {format_fcfa(price)}"
                )
        raw_id = payload.get("product_id")
        name = payload.get("name")
        price = payload.get("price")
        if raw_id and name and price is not None:
            descriptions[uuid.UUID(str(raw_id))] = (
                f"{name} — {format_fcfa(price)}"
            )
    return descriptions


def normalize_for_reply_match(text: str) -> str:
    """Casefold, strip accents, fold punctuation and whitespace to one space."""
    collapsed = " ".join((text or "").split())
    decomposed = unicodedata.normalize("NFKD", collapsed)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    no_punct = re.sub(r"[^\w\s]", " ", without_accents, flags=re.UNICODE)
    return " ".join(no_punct.split()).casefold()


def reply_names_product(reply: str, product_name: str) -> bool:
    """True when the normalised full product name is in the normalised reply."""
    needle = normalize_for_reply_match(product_name)
    if not needle:
        return False
    return needle in normalize_for_reply_match(reply)


def images_named_in_reply(
    images: list[ProductImageRef],
    names: dict[uuid.UUID, str],
    reply: str,
) -> list[ProductImageRef]:
    """Keep tool-output order. Empty means the caller should fall back."""
    kept: list[ProductImageRef] = []
    for image in images:
        name = names.get(image.product_id)
        if name and reply_names_product(reply, name):
            kept.append(image)
    return kept


def photo_delivery_plan(
    product_ids: list[uuid.UUID],
    already_sent: set[uuid.UUID],
    max_images: int = MAX_WHATSAPP_IMAGES,
) -> dict[uuid.UUID, PhotoStatus]:
    """Per-product send/skip decision used by the WhatsApp worker and tools.

    `product_ids` must be the same ordered list the worker would extract
    (products that have a non-null image_url). Single-product turns ignore
    the cap; multi-product turns send at most `max_images` new photos.
    Already-sent products do not consume the cap.
    """
    plan: dict[uuid.UUID, PhotoStatus] = {}
    multi_mode = len(product_ids) > 1
    photos_sent = 0
    for product_id in product_ids:
        if product_id in already_sent:
            plan[product_id] = PHOTO_STATUS_ALREADY_SENT
            continue
        if multi_mode and photos_sent >= max_images:
            plan[product_id] = PHOTO_STATUS_NONE
            continue
        plan[product_id] = PHOTO_STATUS_WILL_BE_SENT
        photos_sent += 1
    return plan
