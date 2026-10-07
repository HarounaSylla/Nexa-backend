"""Replayable history items for human-handover and inbound-photo turns.

Stored `items` are plain dicts the Responses API accepts as input
(`{"role": ..., "content": "<text>"}`) and that
`_is_replayable_history_item` already keeps. Prefixes are French because
the model reads them next to French chats.

The handover developer note is computed at replay time and is never
persisted as a Message row.
"""

from __future__ import annotations

from typing import Any

PREFIX_BOUTIQUE = "[Boutique]"
PREFIX_AUTOMATIC = "[Message automatique de la boutique]"
PHOTO_MARKER_PREFIX = "[Le client a envoyé une photo"

HANDOVER_DEVELOPER_TEXT = (
    "A member of the shop team answered part of this conversation (see the "
    '"[Boutique]" messages above). Read the customer\'s new message in light '
    "of that exchange. Do not assume the customer still means a product "
    "discussed before the handover. If the message refers to something you "
    "cannot identify from the text (a photo, \"celle-ci\", \"la robe ci-haut\"), "
    "ask one short clarifying question."
)

_CAPTION_MAX_CHARS = 200


def item_human_reply(text: str) -> dict[str, str]:
    return {"role": "assistant", "content": f"{PREFIX_BOUTIQUE} {text}"}


def item_automatic_payment_link(label: str | None, order_number: int) -> dict[str, str]:
    label_word = (label or "").strip()
    if label_word:
        description = (
            f"lien de paiement {label_word} envoyé pour la commande n°{order_number}."
        )
    else:
        description = f"lien de paiement envoyé pour la commande n°{order_number}."
    return {
        "role": "assistant",
        "content": f"{PREFIX_AUTOMATIC} {description}",
    }


def item_automatic_proof_ack() -> dict[str, str]:
    return {
        "role": "assistant",
        "content": (
            f"{PREFIX_AUTOMATIC} preuve de paiement bien reçue ; "
            "la boutique va vérifier."
        ),
    }


def item_customer_text(text: str) -> dict[str, str]:
    return {"role": "user", "content": text}


def item_customer_photo(
    *,
    caption: str | None = None,
    classification: str | None = None,
    order_number: int | None = None,
) -> dict[str, str]:
    if classification == "payment_proof" and order_number is not None:
        content = (
            "[Le client a envoyé une photo de preuve de paiement "
            f"pour la commande n°{order_number}]"
        )
    else:
        legend = (caption or "").strip()
        if classification == "product_photo":
            if legend:
                clipped = legend[:_CAPTION_MAX_CHARS]
                content = (
                    "[Le client a envoyé une photo de produit "
                    f'— légende : "{clipped}"]'
                )
            else:
                content = "[Le client a envoyé une photo de produit]"
        elif legend:
            clipped = legend[:_CAPTION_MAX_CHARS]
            content = f'[Le client a envoyé une photo — légende : "{clipped}"]'
        elif classification in {"not_analyzed", "other", "unknown", None}:
            content = "[Le client a envoyé une photo (non analysée)]"
        else:
            content = "[Le client a envoyé une photo]"
    return {"role": "user", "content": content}


def handover_developer_item(prior_items: list[Any]) -> dict[str, str] | None:
    """One developer note when a human replied after the last native agent turn.

    Automatic merchant messages alone do not count. Returns at most one item.
    """
    last_native = -1
    boutique_after = False
    for index, item in enumerate(prior_items):
        if not isinstance(item, dict):
            continue
        if _is_native_agent_item(item):
            last_native = index
            boutique_after = False
        elif _is_boutique_item(item) and index > last_native:
            boutique_after = True
    if boutique_after:
        return {"role": "developer", "content": HANDOVER_DEVELOPER_TEXT}
    return None


def _item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("text"):
                parts.append(str(part["text"]))
        return "".join(parts)
    return ""


def _is_boutique_item(item: dict[str, Any]) -> bool:
    return _item_text(item).startswith(PREFIX_BOUTIQUE)


def _is_automatic_item(item: dict[str, Any]) -> bool:
    return _item_text(item).startswith(PREFIX_AUTOMATIC)


def _is_native_agent_item(item: dict[str, Any]) -> bool:
    """A turn produced by the model, not a shop-team or automatic row."""
    if item.get("type") == "function_call":
        return True
    role = item.get("role")
    item_type = item.get("type")
    if item_type in {"function_call_output", "reasoning"}:
        return False
    if item_type == "message" or role == "assistant":
        if role not in (None, "assistant"):
            return False
        text = _item_text(item)
        if text.startswith(PREFIX_BOUTIQUE) or text.startswith(PREFIX_AUTOMATIC):
            return False
        return True
    return False
