"""Structured product photos from an agent turn's tool outputs.

`/agent/simulate` and the WhatsApp worker both consume this list. The
chat text never contains URLs.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel


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
