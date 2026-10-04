"""Pure matching of a proof image to an awaiting-proof order."""

from __future__ import annotations

import re
from typing import Protocol

_ORDER_NUMBER_RE = re.compile(
    r"(?:n[°oº]\s*|#\s*|commande\s+)(\d+)|(?<!\d)(\d+)(?!\d)",
    re.IGNORECASE,
)


class OrderNumbered(Protocol):
    order_number: int


def extract_caption_order_numbers(caption: str | None) -> set[int]:
    if not caption:
        return set()
    found: set[int] = set()
    for match in _ORDER_NUMBER_RE.finditer(caption):
        raw = match.group(1) or match.group(2)
        if raw:
            found.add(int(raw))
    return found


def choisir_commande_pour_preuve[T: OrderNumbered](
    candidates: list[T], caption: str | None
) -> T | None:
    """Pick one candidate or None. Never guess when several remain."""
    if not candidates:
        return None
    mentioned = extract_caption_order_numbers(caption)
    if mentioned:
        matched = [item for item in candidates if item.order_number in mentioned]
        if len(matched) == 1:
            return matched[0]
        return None
    if len(candidates) == 1:
        return candidates[0]
    return None
