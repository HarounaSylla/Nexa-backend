"""Replayable history items for human-handover and inbound-photo turns.

Stored `items` are plain dicts the Responses API accepts as input
(`{"role": ..., "content": "<text>"}`) and that
`_is_replayable_history_item` already keeps. Prefixes are French because
the model reads them next to French chats.

The handover developer note is computed at replay time and is never
persisted as a Message row. Older assistant turns that skipped
"autre chose ?" and jumped to the address are rewritten the same way
(copy only; stored rows are not updated).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

PREFIX_BOUTIQUE = "[Boutique]"
PREFIX_AUTOMATIC = "[Message automatique de la boutique]"
PHOTO_MARKER_PREFIX = "[Le client a envoyé une photo"
QUOTE_MARKER_PREFIX = "[Le client répond à"

KIND_SHOP_TEXT = "shop_text"
KIND_SHOP_PHOTO = "shop_photo"
KIND_CUSTOMER_TEXT = "customer_text"
KIND_CUSTOMER_PHOTO = "customer_photo"

PHOTO_RECOGNITION_RECOGNIZED = "recognized"
PHOTO_RECOGNITION_POSSIBLE = "possible"
PHOTO_RECOGNITION_UNRECOGNIZED = "unrecognized"
PHOTO_RECOGNITION_UNANALYSED = "unanalysed"

HANDOVER_DEVELOPER_TEXT = (
    "A member of the shop team answered part of this conversation (see the "
    '"[Boutique]" messages above). Read the customer\'s new message in light '
    "of that exchange. Do not assume the customer still means a product "
    "discussed before the handover. A bare shop reply such as \"oui\", "
    "\"ok\" or \"d'accord\" does not name a product. If the latest customer "
    "message refers to a photo whose marker contains \"(non analysée)\", "
    "follow rule 18: call analyser_photo_client when required, and do not "
    "ask for name, colour or type until that tool has returned a non-usable "
    "result. If the message refers to something else you cannot identify "
    "from the text (\"celle-ci\", \"la robe ci-haut\") and there is no "
    "unanalysed photo marker, ask one short clarifying question."
)

_CAPTION_MAX_CHARS = 200
_QUOTE_EXCERPT_MAX_CHARS = 200


def trim_quote_excerpt(text: str, limit: int = _QUOTE_EXCERPT_MAX_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _earlier_note(from_earlier_conversation: bool) -> str:
    if from_earlier_conversation:
        return " (conversation précédente)"
    return ""


def _customer_photo_marker_text(
    *,
    from_earlier_conversation: bool,
    photo_recognition: str | None,
    product_name: str | None,
    product_id: uuid.UUID | None,
) -> str:
    name = (product_name or "").strip()
    if from_earlier_conversation:
        subject = "son ancienne photo (conversation précédente)"
    else:
        subject = "sa propre photo"
    if photo_recognition == PHOTO_RECOGNITION_RECOGNIZED and name:
        content = (
            f"[Le client répond à {subject}, reconnue comme « {name} »]"
        )
    elif photo_recognition == PHOTO_RECOGNITION_POSSIBLE and name:
        content = (
            f"[Le client répond à {subject}, correspondance possible « {name} »]"
        )
    elif photo_recognition == PHOTO_RECOGNITION_UNRECOGNIZED:
        content = f"[Le client répond à {subject} (non reconnue)]"
    elif photo_recognition == PHOTO_RECOGNITION_UNANALYSED:
        content = f"[Le client répond à {subject} (non analysée)]"
    else:
        content = f"[Le client répond à {subject}]"
    if product_id is not None and photo_recognition in {
        PHOTO_RECOGNITION_RECOGNIZED,
        PHOTO_RECOGNITION_POSSIBLE,
    }:
        content += f" (product_id={product_id})"
    return content


def quote_replay_marker(
    *,
    kind: str,
    excerpt: str | None = None,
    product_name: str | None = None,
    product_id: uuid.UUID | None = None,
    from_earlier_conversation: bool = False,
    photo_recognition: str | None = None,
) -> dict[str, str] | None:
    """Replay-only user item describing what the customer quoted. None if unusable."""
    earlier = _earlier_note(from_earlier_conversation)
    if kind == KIND_SHOP_PHOTO:
        name = (product_name or "").strip()
        if not name:
            return None
        content = (
            "[Le client répond à la photo du produit "
            f"« {name} » envoyée par la boutique{earlier}]"
        )
        if product_id is not None:
            content += f" (product_id={product_id})"
        return {"role": "user", "content": content}
    if kind == KIND_CUSTOMER_PHOTO:
        return {
            "role": "user",
            "content": _customer_photo_marker_text(
                from_earlier_conversation=from_earlier_conversation,
                photo_recognition=photo_recognition,
                product_name=product_name,
                product_id=product_id,
            ),
        }
    clipped = trim_quote_excerpt(excerpt or "")
    if not clipped:
        return None
    if kind == KIND_CUSTOMER_TEXT:
        return {
            "role": "user",
            "content": (
                "[Le client répond à son propre message"
                f"{earlier} : « {clipped} »]"
            ),
        }
    if kind == KIND_SHOP_TEXT:
        return {
            "role": "user",
            "content": (
                "[Le client répond à ce message de la boutique"
                f"{earlier} : « {clipped} »]"
            ),
        }
    return None


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
        elif classification == "not_analyzed":
            if legend:
                clipped = legend[:_CAPTION_MAX_CHARS]
                content = (
                    "[Le client a envoyé une photo (non analysée) "
                    f'— légende : "{clipped}"]'
                )
            else:
                content = "[Le client a envoyé une photo (non analysée)]"
        elif legend:
            clipped = legend[:_CAPTION_MAX_CHARS]
            content = f'[Le client a envoyé une photo — légende : "{clipped}"]'
        elif classification in {"other", "unknown", None}:
            content = "[Le client a envoyé une photo (non analysée)]"
        else:
            content = "[Le client a envoyé une photo]"
    return {"role": "user", "content": content}


_QTY_THEN_ADDRESS = re.compile(
    r"(?is)^(Je note\s+\d+\b.*?)"
    r"(\n+Pourriez-vous me communiquer votre adresse.*)$"
)
_COMPREND_THEN_ADDRESS = re.compile(
    r"(?is)^(.*?Votre commande comprend\s+\d+\b.*?)"
    r"(\n+Pourriez-vous me communiquer votre adresse.*)$"
)
_AUTRE_CHOSE = re.compile(r"autre chose", re.I)
_RECAP_CONFIRM = re.compile(r"confirmez-vous", re.I)
_THAT_IS_ALL = re.compile(
    r"(?i)c['\u2019]est tout|ça sera tout|ca sera tout|"
    r"rien d['\u2019]autre|non c['\u2019]est bon|non merci|\bfin\b"
)
_PAYMENT_ASIDE = re.compile(r"(?i)\b(?:à|a)\s+la\s+livraison\b|\ben ligne\b")
_ADDRESS_OR_CITY = re.compile(r"(?i)\b(?:adresse|ville|quartier|livraison)\b")
_REWRITTEN_QTY_TAIL = " Souhaitez-vous autre chose ?"


def _assistant_plain_text(item: dict[str, Any]) -> str:
    if item.get("type") in {"function_call", "function_call_output", "reasoning"}:
        return ""
    if item.get("role") not in (None, "assistant"):
        return ""
    if item.get("type") not in (None, "message") and item.get("role") != "assistant":
        return ""
    return _item_text(item)


def _with_content_text(item: dict[str, Any], new_text: str) -> dict[str, Any]:
    copied = dict(item)
    content = item.get("content")
    if isinstance(content, list):
        parts: list[Any] = []
        replaced = False
        for part in content:
            if replaced:
                parts.append(part)
            elif isinstance(part, str):
                parts.append(new_text)
                replaced = True
            elif isinstance(part, dict) and "text" in part:
                new_part = dict(part)
                new_part["text"] = new_text
                parts.append(new_part)
                replaced = True
            else:
                parts.append(part)
        copied["content"] = parts if replaced else new_text
        return copied
    copied["content"] = new_text
    return copied


def _preceding_user_text(items: list[Any], index: int) -> str:
    for prev in reversed(items[:index]):
        if not isinstance(prev, dict):
            continue
        if prev.get("type") in {"function_call", "function_call_output", "reasoning"}:
            continue
        if prev.get("role") == "user":
            return _item_text(prev)
    return ""


def _customer_said_that_is_all(text: str) -> bool:
    return bool(_THAT_IS_ALL.search(text.replace("\u2019", "'").replace("\u2018", "'")))


def _customer_gave_address_or_city(text: str) -> bool:
    folded = text.replace("\u2019", "'").replace("\u2018", "'")
    without_payment = _PAYMENT_ASIDE.sub(" ", folded)
    return bool(_ADDRESS_OR_CITY.search(without_payment))


def rewrite_outdated_quantity_skips(items: list[Any]) -> list[Any]:
    """Replay-only: stop the model imitating pre-rule-17 quantity turns.

    Copies matching assistant items; never mutates the dicts stored on
    Message rows. A bare quantity (or confirmation) that jumped straight
    to the address is shown as the rule-17 question instead. Do not
    rewrite a matching turn when the customer message just before it
    already said that is all, or already gave address/city details.
    """
    rewritten: list[Any] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            rewritten.append(item)
            continue
        text = _assistant_plain_text(item)
        if (
            not text
            or _AUTRE_CHOSE.search(text)
            or _RECAP_CONFIRM.search(text)
        ):
            rewritten.append(item)
            continue
        match = _QTY_THEN_ADDRESS.match(text) or _COMPREND_THEN_ADDRESS.match(text)
        if match is None:
            rewritten.append(item)
            continue
        preceding = _preceding_user_text(items, index)
        if _customer_said_that_is_all(preceding) or _customer_gave_address_or_city(
            preceding
        ):
            rewritten.append(item)
            continue
        new_text = match.group(1).rstrip() + _REWRITTEN_QTY_TAIL
        rewritten.append(_with_content_text(item, new_text))
    return rewritten


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
