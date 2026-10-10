"""Human-handover replay, photo markers, handover note, photo_status."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.agent.handover import (
    HANDOVER_DEVELOPER_TEXT,
    PREFIX_AUTOMATIC,
    PREFIX_BOUTIQUE,
    handover_developer_item,
    item_automatic_payment_link,
    item_automatic_proof_ack,
    item_customer_photo,
    item_customer_text,
    item_human_reply,
    rewrite_outdated_quantity_skips,
)
from app.agent.images import (
    MAX_WHATSAPP_IMAGES,
    PHOTO_STATUS_ALREADY_SENT,
    PHOTO_STATUS_NONE,
    PHOTO_STATUS_WILL_BE_SENT,
    extract_product_images,
    photo_delivery_plan,
)
from app.agent.models import Conversation, Message, SentProductImage
from app.agent.orchestrator import (
    _is_replayable_history_item,
    history_items_from_messages,
    prior_items_for_agent,
    traiter_message_entrant,
)
from app.agent.prompts import build_system_prompt
from app.agent.service import (
    STATUS_ESCALATED,
    TURN_ROLE_CUSTOMER,
    TURN_ROLE_MERCHANT,
    enregistrer_message_commercant,
    escalader_vers_humain,
    repondre_en_humain,
)
from app.agent.tools import execute_tool
from app.catalogue.models import Merchant, Product, ProductImage
from app.core.db import AsyncSessionLocal
from app.merchants.service import MerchantPreferencesData
from app.notifications.models import Notification
from app.orders.models import (
    DeliveryZone,
    Order,
    OrderItem,
    PaymentMethod,
    StockMovement,
)
from app.orders.service import creer_commande, normalize_city
from app.proofs.models import (
    CLASSIFICATION_NOT_ANALYZED,
    CLASSIFICATION_OTHER,
    CLASSIFICATION_PAYMENT_PROOF,
    InboundImage,
)
from app.proofs.service import ACK_TEXT, traiter_image_entrante
from app.proofs.vision import ImageAnalysis

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _vector(index: int) -> list[float]:
    values = [0.0] * 1024
    values[index] = 1.0
    return values


def _message(turn_role: str, display: str, items: list) -> Message:
    return Message(
        conversation_id=uuid.uuid4(),
        turn_role=turn_role,
        display_text=display,
        items=items,
    )


def _assert_reasoning_pairs_intact(items: list) -> None:
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "reasoning":
            continue
        assert index + 1 < len(items), "reasoning item is last; successor missing"
        successor = items[index + 1]
        assert isinstance(successor, dict)
        assert successor.get("type") != "reasoning"
        assert not str(successor.get("content") or "").startswith(PREFIX_BOUTIQUE)
        assert successor.get("role") != "developer"


class _CapturingGraph:
    def __init__(self) -> None:
        self.input_lists: list[list] = []

    async def ainvoke(self, initial, config=None):
        self.input_lists.append(list(initial["input_list"]))
        return {
            **initial,
            "new_items": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "Réponse test",
                }
            ],
            "output_text": "Réponse test",
        }


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(Notification).where(Notification.merchant_id.in_(merchant_ids))
        )
        image_ids = list(
            (
                await db.execute(
                    select(InboundImage.id).where(
                        InboundImage.merchant_id.in_(merchant_ids)
                    )
                )
            ).scalars().all()
        )
        if image_ids:
            await db.execute(delete(InboundImage).where(InboundImage.id.in_(image_ids)))
        order_ids = list(
            (
                await db.execute(
                    select(Order.id).where(Order.merchant_id.in_(merchant_ids))
                )
            ).scalars().all()
        )
        if order_ids:
            await db.execute(
                delete(StockMovement).where(StockMovement.order_id.in_(order_ids))
            )
            await db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            await db.execute(delete(Order).where(Order.id.in_(order_ids)))
        conversation_ids = list(
            (
                await db.execute(
                    select(Conversation.id).where(
                        Conversation.merchant_id.in_(merchant_ids)
                    )
                )
            ).scalars().all()
        )
        if conversation_ids:
            await db.execute(
                delete(SentProductImage).where(
                    SentProductImage.conversation_id.in_(conversation_ids)
                )
            )
            await db.execute(
                delete(Message).where(Message.conversation_id.in_(conversation_ids))
            )
            await db.execute(
                delete(Conversation).where(Conversation.id.in_(conversation_ids))
            )
        await db.execute(
            delete(DeliveryZone).where(DeliveryZone.merchant_id.in_(merchant_ids))
        )
        product_ids = list(
            (
                await db.execute(
                    select(Product.id).where(Product.merchant_id.in_(merchant_ids))
                )
            ).scalars().all()
        )
        if product_ids:
            await db.execute(
                delete(ProductImage).where(ProductImage.product_id.in_(product_ids))
            )
        await db.execute(delete(Product).where(Product.merchant_id.in_(merchant_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_merchant() -> Merchant:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=f"pytest-handover-{uuid.uuid4()}",
            whatsapp_phone_number_id=f"pnid-handover-{uuid.uuid4()}",
        )
        db.add(merchant)
        await db.flush()
        db.add(
            DeliveryZone(
                merchant_id=merchant.id,
                city="Dakar",
                city_normalized=normalize_city("Dakar"),
                available=True,
                min_delivery_hours=24,
                max_delivery_hours=48,
            )
        )
        await db.commit()
        await db.refresh(merchant)
        return merchant


def test_handover_builders_are_plain_replayable_dicts() -> None:
    human = item_human_reply("oui on l'a")
    auto_link = item_automatic_payment_link("Wave", 58)
    auto_ack = item_automatic_proof_ack()
    customer = item_customer_text("Okay")
    photo = item_customer_photo(classification="not_analyzed")
    for item in (human, auto_link, auto_ack, customer, photo):
        assert set(item) == {"role", "content"}
        assert isinstance(item["content"], str)
    assert human == {"role": "assistant", "content": f"{PREFIX_BOUTIQUE} oui on l'a"}
    assert auto_link["content"].startswith(PREFIX_AUTOMATIC)
    assert "http" not in auto_link["content"]
    assert "Wave" in auto_link["content"]
    assert "n°58" in auto_link["content"]
    assert auto_ack["content"].startswith(PREFIX_AUTOMATIC)
    assert "preuve de paiement bien reçue" in auto_ack["content"]
    assert customer == {"role": "user", "content": "Okay"}
    assert photo["content"] == "[Le client a envoyé une photo (non analysée)]"
    assert "analyser_photo_client" in HANDOVER_DEVELOPER_TEXT
    assert "does not name a product" in HANDOVER_DEVELOPER_TEXT
    product_photo = item_customer_photo(classification="product_photo")
    assert product_photo["content"] == "[Le client a envoyé une photo de produit]"
    product_captioned = item_customer_photo(
        classification="product_photo", caption="Vous avez cette robe?"
    )
    assert product_captioned["content"] == (
        '[Le client a envoyé une photo de produit — légende : "Vous avez cette robe?"]'
    )
    captioned = item_customer_photo(caption="la robe noire")
    assert captioned["content"] == (
        '[Le client a envoyé une photo — légende : "la robe noire"]'
    )
    unanalysed_captioned = item_customer_photo(
        classification="not_analyzed", caption="Vous avez ca?"
    )
    assert unanalysed_captioned["content"] == (
        '[Le client a envoyé une photo (non analysée) — légende : "Vous avez ca?"]'
    )
    long_caption = item_customer_photo(caption="x" * 250)
    assert 'légende : "' in long_caption["content"]
    assert long_caption["content"].endswith('"]')
    assert len(long_caption["content"].split('légende : "')[1][:-2]) == 200
    proof = item_customer_photo(
        classification="payment_proof",
        order_number=58,
        caption="ignored",
    )
    assert proof["content"] == (
        "[Le client a envoyé une photo de preuve de paiement "
        "pour la commande n°58]"
    )
    other = item_customer_photo(classification="other")
    assert other["content"] == "[Le client a envoyé une photo (non analysée)]"


def _qty_then_address(n: int = 1) -> dict:
    return {
        "role": "assistant",
        "content": (
            f"Je note {n} article à 9 500 F.\n\n"
            "Pourriez-vous me communiquer votre adresse complète, "
            "la ville et votre mode de paiement : à la livraison ou en ligne ?"
        ),
    }


def test_bare_quantity_skip_is_rewritten_on_replay_only() -> None:
    skip = _qty_then_address(1)
    original = skip["content"]
    user = item_customer_text("1")
    rewritten = rewrite_outdated_quantity_skips([user, skip])
    assert rewritten[0] is user
    assert rewritten[1] is not skip
    assert "Souhaitez-vous autre chose ?" in rewritten[1]["content"]
    assert "adresse" not in rewritten[1]["content"].lower()
    assert skip["content"] == original
    history = [
        _message("customer", "1", [user]),
        _message("agent", original, [skip]),
    ]
    replayed = prior_items_for_agent(history)
    assert any("Souhaitez-vous autre chose ?" in str(item.get("content")) for item in replayed)
    assert skip["content"] == original
    assert user["content"] == "1"


def test_cest_tout_quantity_skip_is_not_rewritten() -> None:
    skip = _qty_then_address(2)
    original = skip["content"]
    user = item_customer_text("Je prends 2, c'est tout")
    rewritten = rewrite_outdated_quantity_skips([user, skip])
    assert rewritten[1] is skip
    assert rewritten[1]["content"] == original
    assert "Pourriez-vous me communiquer votre adresse" in rewritten[1]["content"]


def test_early_address_quantity_skip_is_not_rewritten() -> None:
    skip = _qty_then_address(2)
    original = skip["content"]
    user = item_customer_text(
        "Je veux 2 articles, livraison aux Parcelles à Dakar"
    )
    rewritten = rewrite_outdated_quantity_skips([user, skip])
    assert rewritten[1] is skip
    assert skip["content"] == original


def test_several_articles_rewrites_only_bare_quantity_skips() -> None:
    first = _qty_then_address(1)
    second = _qty_then_address(1)
    after_all = _qty_then_address(2)
    already_ok = {
        "role": "assistant",
        "content": "Très bien, 1 article. Souhaitez-vous autre chose ?",
    }
    recap = {
        "type": "message",
        "role": "assistant",
        "content": (
            "Vous commandez 2 articles à 9 500 F.\n"
            "La livraison est à Dakar.\n"
            "Confirmez-vous cette commande ?"
        ),
    }
    after_cest_tout = {
        "role": "assistant",
        "content": (
            "Pourriez-vous me communiquer votre adresse complète, "
            "la ville et votre mode de paiement : à la livraison ou en ligne ?"
        ),
    }
    originals = [first["content"], second["content"], after_all["content"]]
    items = [
        item_customer_text("1"),
        first,
        item_customer_text("Oui un autre article"),
        already_ok,
        item_customer_text("1 aussi"),
        second,
        item_customer_text("Non c'est tout"),
        after_all,
        after_cest_tout,
        recap,
    ]
    rewritten = rewrite_outdated_quantity_skips(items)
    assert rewritten[1] is not first
    assert "Souhaitez-vous autre chose ?" in rewritten[1]["content"]
    assert rewritten[5] is not second
    assert "Souhaitez-vous autre chose ?" in rewritten[5]["content"]
    assert rewritten[7] is after_all
    assert rewritten[8] is after_cest_tout
    assert rewritten[9] is recap
    assert first["content"] == originals[0]
    assert second["content"] == originals[1]
    assert after_all["content"] == originals[2]


def test_history_replays_new_rows_ignores_legacy_empty_and_keeps_reasoning() -> None:
    reasoning = {"type": "reasoning", "id": "rs_1"}
    function_call = {
        "type": "function_call",
        "name": "obtenir_disponibilite",
        "call_id": "c1",
        "arguments": "{}",
    }
    output = {"type": "function_call_output", "call_id": "c1", "output": "{}"}
    assistant = {
        "type": "message",
        "role": "assistant",
        "content": "La robe rouge est disponible.",
    }
    rows = [
        _message("customer", "Hello", [{"role": "user", "content": "Hello"}]),
        _message("agent", "La robe rouge.", [reasoning, function_call, output, assistant]),
        _message("merchant", "lien https://pay.example.com", []),
        _message("customer", "Photo", []),
        _message("merchant", "oui on l'a", [item_human_reply("oui on l'a")]),
        _message(
            "customer",
            "Photo",
            [item_customer_photo(classification="not_analyzed")],
        ),
        _message("agent", "[escalade] Le client souhaite un humain.", []),
    ]
    replayed = history_items_from_messages(rows)
    assert replayed[0] == {"role": "user", "content": "Hello"}
    assert replayed[1] is reasoning
    assert replayed[2] is function_call
    assert item_human_reply("oui on l'a") in replayed
    assert item_customer_photo(classification="not_analyzed") in replayed
    assert not any(
        item.get("content") == "lien https://pay.example.com" for item in replayed
    )
    _assert_reasoning_pairs_intact(replayed)
    with_note = prior_items_for_agent(rows)
    _assert_reasoning_pairs_intact(with_note)
    assert with_note.count({"role": "developer", "content": HANDOVER_DEVELOPER_TEXT}) == 1
    assert with_note[-1]["role"] == "developer"


def test_multi_item_cart_replays_earlier_article_tool_outputs() -> None:
    """Earlier rechercher_produits / obtenir_disponibilite outputs stay in replay.

    So the agent can reuse product_ids from articles confirmed several
    turns ago without a silent re-search — unless those items were never
    stored (legacy empty rows). inbound_image items are still dropped.
    """
    dress_id = str(uuid.uuid4())
    bag_id = str(uuid.uuid4())
    dress_search = {
        "type": "function_call",
        "name": "rechercher_produits",
        "call_id": "c-dress",
        "arguments": json.dumps({"requete": "robe rouge"}),
    }
    dress_search_out = {
        "type": "function_call_output",
        "call_id": "c-dress",
        "output": json.dumps(
            {
                "products": [
                    {
                        "id": dress_id,
                        "name": "Robe longue rouge de soirée",
                        "price": "25000.00",
                    }
                ]
            }
        ),
    }
    dress_avail = {
        "type": "function_call",
        "name": "obtenir_disponibilite",
        "call_id": "c-dress-av",
        "arguments": json.dumps({"produit_id": dress_id}),
    }
    dress_avail_out = {
        "type": "function_call_output",
        "call_id": "c-dress-av",
        "output": json.dumps(
            {
                "product_id": dress_id,
                "stock_status": "disponible",
                "price": "25000.00",
            }
        ),
    }
    bag_search = {
        "type": "function_call",
        "name": "rechercher_produits",
        "call_id": "c-bag",
        "arguments": json.dumps({"requete": "sac camel"}),
    }
    bag_search_out = {
        "type": "function_call_output",
        "call_id": "c-bag",
        "output": json.dumps(
            {
                "products": [
                    {
                        "id": bag_id,
                        "name": "Sac à main en cuir camel",
                        "price": "22000.00",
                    }
                ]
            }
        ),
    }
    history = [
        _message(
            "customer",
            "Vous avez la robe rouge ?",
            [{"role": "user", "content": "Vous avez la robe rouge ?"}],
        ),
        _message(
            "agent",
            "La robe rouge est disponible.",
            [
                dress_search,
                dress_search_out,
                dress_avail,
                dress_avail_out,
                {
                    "type": "message",
                    "role": "assistant",
                    "content": "La robe rouge est disponible.",
                },
            ],
        ),
        _message(
            "customer",
            "Je prends 1",
            [{"role": "user", "content": "Je prends 1"}],
        ),
        _message(
            "agent",
            "Souhaitez-vous autre chose ?",
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": (
                        "Très bien, 1 robe longue rouge de soirée. "
                        "Souhaitez-vous autre chose ?"
                    ),
                }
            ],
        ),
        _message(
            "customer",
            "Oui un sac à main camel",
            [{"role": "user", "content": "Oui un sac à main camel"}],
        ),
        _message(
            "agent",
            "Le sac camel.",
            [bag_search, bag_search_out],
        ),
        _message(
            "customer",
            "photo bytes",
            [{"type": "inbound_image", "path": "/secret.jpg"}],
        ),
    ]
    assert _is_replayable_history_item(dress_search_out) is True
    assert _is_replayable_history_item(dress_avail_out) is True
    assert _is_replayable_history_item(bag_search_out) is True
    assert _is_replayable_history_item({"type": "inbound_image"}) is False
    replayed = prior_items_for_agent(history)
    blob = json.dumps(replayed)
    assert dress_id in blob
    assert bag_id in blob
    assert dress_search_out in replayed
    assert dress_avail_out in replayed
    assert bag_search_out in replayed
    assert not any(
        isinstance(item, dict) and item.get("type") == "inbound_image"
        for item in replayed
    )


def test_handover_note_presence_rules() -> None:
    native = {
        "type": "message",
        "role": "assistant",
        "content": "Un membre de l'équipe va prendre le relais.",
    }
    function_call = {
        "type": "function_call",
        "name": "escalader_vers_humain",
        "call_id": "c-esc",
        "arguments": "{}",
    }
    boutique = item_human_reply("oui on l'a")
    automatic = item_automatic_payment_link("Wave", 58)
    photo = item_customer_photo(classification="not_analyzed")

    after_human = [
        function_call,
        native,
        boutique,
        photo,
    ]
    note = handover_developer_item(after_human)
    assert note == {"role": "developer", "content": HANDOVER_DEVELOPER_TEXT}
    composed = [*after_human]
    if handover_developer_item(composed):
        composed.append(handover_developer_item(composed))
    assert sum(1 for item in composed if item.get("role") == "developer") == 1

    only_auto = [native, automatic, photo]
    assert handover_developer_item(only_auto) is None

    no_human = [native, {"role": "user", "content": "Okay"}]
    assert handover_developer_item(no_human) is None

    boutique_then_native = [boutique, native]
    assert handover_developer_item(boutique_then_native) is None


def test_incident_input_list_order() -> None:
    reasoning = {"type": "reasoning", "id": "rs_night"}
    search_call = {
        "type": "function_call",
        "name": "rechercher_produits",
        "call_id": "c-search",
        "arguments": "{}",
    }
    avail_call = {
        "type": "function_call",
        "name": "obtenir_disponibilite",
        "call_id": "c-avail",
        "arguments": '{"produit_id":"8717de24-9b5a-48ef-aa81-75cd023ea7cf"}',
    }
    red_reply = {
        "type": "message",
        "role": "assistant",
        "content": "La robe longue rouge de soirée est disponible.",
    }
    esc_call = {
        "type": "function_call",
        "name": "escalader_vers_humain",
        "call_id": "c-esc",
        "arguments": "{}",
    }
    esc_msg = {
        "type": "message",
        "role": "assistant",
        "content": "Un membre de l'équipe va prendre le relais.",
    }
    history = [
        _message("customer", "Vêtement de femme", [{"role": "user", "content": "Vêtement de femme"}]),
        _message("agent", "Voici quelques vêtements.", [reasoning, search_call, red_reply]),
        _message("customer", "Je peux avoir la robe rouge", [{"role": "user", "content": "Je peux avoir la robe rouge"}]),
        _message("agent", "La robe longue rouge.", [avail_call, red_reply]),
        _message("agent", "[escalade] Le client souhaite parler à un agent humain.", []),
        _message("agent", "Un membre de l'équipe va prendre le relais.", [esc_call, esc_msg]),
        _message("merchant", "oui on l'a", [item_human_reply("oui on l'a")]),
        _message("customer", "Photo", [item_customer_photo(classification="not_analyzed")]),
        _message("customer", "Okay", [item_customer_text("Okay")]),
    ]
    last_customer = {"role": "user", "content": "Je veux commande pour la robe ci-haut"}
    prior = prior_items_for_agent(history)
    input_list = [*prior, last_customer]
    contents = [
        item.get("content") if isinstance(item.get("content"), str) else item.get("name")
        for item in input_list
    ]
    boutique_index = contents.index(f"{PREFIX_BOUTIQUE} oui on l'a")
    photo_index = contents.index("[Le client a envoyé une photo (non analysée)]")
    note_indexes = [
        index
        for index, item in enumerate(input_list)
        if item.get("role") == "developer"
        and item.get("content") == HANDOVER_DEVELOPER_TEXT
    ]
    assert len(note_indexes) == 1
    assert boutique_index < photo_index < note_indexes[0] < len(input_list) - 1
    assert input_list[-1] == last_customer
    assert "Vêtement de femme" in contents
    _assert_reasoning_pairs_intact(input_list)


def test_prompt_has_rule_15_and_photo_status_rule_14() -> None:
    text = build_system_prompt("Boutique Test", MerchantPreferencesData())["content"]
    assert "15. Customer photos appear in the history" in text
    assert "[Le client a envoyé une photo" in text
    assert "photo_status" in text
    assert "will_be_sent" in text
    assert "already_sent_earlier" in text
    assert "16. When the customer asks about an existing order" in text
    assert "17. Several articles in one order" in text
    assert (
        "The reply to every customer message that gives or confirms an "
        "article quantity MUST"
    ) in text
    assert "MUST NOT ask for address, city or payment" in text
    assert "Do not imitate older assistant turns" in text
    assert "developer note about a visual search" in text
    assert "je vous envoie la photo" in text
    assert "vous avez déjà reçu sa photo plus haut" in text
    assert text.index("15. Customer photos") < text.index("16. When the customer")
    assert text.index("16. When the customer") < text.index("17. Several articles")
    assert "follow rule 17" in text
    assert "Match level none or error" in text
    assert "never ask for the name, colour or type for that photo" in text
    assert (
        "Delivery details already given anywhere in the CURRENT exchange"
    ) in text
    assert "MUST be kept and MUST NEVER be asked again" in text
    assert "ask ONLY for what is still missing" in text
    assert "A neighbourhood already given" in text
    assert "Do not reuse an address, city or payment from an earlier" in text
    assert "never ask for \"votre adresse complète\"" in text
    assert "This rule does not require a street number or landmark" in text
    assert "18. A history marker" in text
    assert "does **not** apply when the history contains" in text
    assert "MUST call `analyser_photo_client`" in text
    assert "cite only the row whose name matches" in text
    assert "do NOT ask \"C'est bien celui-ci ?\" — the shop already named it" in text
    assert "It IS confirmation when a `[Boutique]` message" in text
    assert "does NOT name a product" in text
    assert "without « de produit » is the same situation" in text
    assert "Je ne peux pas identifier l'article à partir de la photo" in text
    assert "always state the candidate name and price" in text
    assert "analyser_photo_client" in text
    assert text.index("17. Several articles") < text.index("18. A history marker")
    assert "Use analyser_photo_client only when rule 18" in text
    numbered = re.findall(r"(?m)^(\d+)\. ", text)
    assert numbered == [str(n) for n in range(1, 21)]
    assert "19. When a customer message is immediately preceded" in text
    assert "[Le client répond à" in text
    assert "takes precedence over" in text
    assert "quoted catalogue photo identifies" in text
    assert "quoted recap with a correction" in text
    assert "calculer_total_commande" in text
    assert "Total des articles" in text
    assert "MUST ask which one before asking the quantity" in text
    assert "MUST NOT guess" in text
    assert "quantity question MUST name that article" in text
    assert "NEVER ask « Souhaitez-vous autre chose ? » again" in text
    assert "un autre modèle" in text
    assert "Count how many distinct products" in text
    assert "will_be_sent count is two or more" in text
    assert "When the count is one" in text
    assert "a product you do not name will not be sent" in text
    assert "promised another model or a photo without naming it" in text
    assert "Voici ce qu'on a, lequel vous plaît" in text
    assert "conversation précédente" in text
    assert "je pense qu'il s'agit de" in text
    assert "non reconnue" in text
    assert text.index("18. A history marker") < text.index("19. When a customer")
    assert "20. When the latest customer messages are several consecutive" in text
    assert "answer all of them in ONE reply" in text
    assert "later one wins" in text
    assert "Never answer the first and ignore the rest" in text
    assert "never pick one silently" in text
    assert "one short proposal per photo" in text
    assert text.index("19. When a customer") < text.index("20. When the latest")
    tail = text[text.index("15. Customer photos") :]
    assert "Robe longue" not in tail
    assert "Sac à main" not in tail
    assert "or rose" not in tail
    assert "25 000" not in tail
    assert "25000" not in tail
    assert "1 <article>" in text
    assert "analyser_photo_client` tool result" in text or "analyser_photo_client tool result" in text


def test_escalation_block_uses_warm_acknowledgement_examples() -> None:
    old_curt = (
        "La boutique est fermée pour le moment, un conseiller vous "
        "répondra dès l'ouverture, demain à 9h."
    )
    hours_set = build_system_prompt(
        "Boutique Test",
        MerchantPreferencesData(opening_hours="Lundi au samedi, 9h à 19h"),
    )["content"]
    hours_unset = build_system_prompt(
        "Boutique Test", MerchantPreferencesData()
    )["content"]
    assert old_curt not in hours_set
    assert old_curt not in hours_unset
    for text in (hours_set, hours_unset):
        assert "Bien sûr," in text
        assert "Absolument," in text
        assert "Avec plaisir," in text
        assert "Tout à fait," in text
        assert "Très bien," in text
        assert "7. Call escalader_vers_humain" in text
        assert "Never promise a precise response time" in text or (
            "Never promise a specific time." in text
        )
    assert "Bien sûr, je transmets votre demande à un conseiller." in hours_set
    assert "Absolument, je préviens un conseiller, il vous répondra dans quelques instants." in hours_set
    assert "quote the hours as written" in hours_set
    assert "interpret carefully" in hours_set
    assert "If today's weekday is not listed in those hours" in hours_set
    assert (
        "Bien sûr, je transmets votre demande, un conseiller vous répondra dès que possible."
        in hours_unset
    )


def test_photo_delivery_plan_three_statuses() -> None:
    first, second, third, fourth, fifth = (uuid.uuid4() for _ in range(5))
    already = {second}
    plan = photo_delivery_plan(
        [first, second, third, fourth, fifth],
        already,
        max_images=MAX_WHATSAPP_IMAGES,
    )
    assert plan[first] == PHOTO_STATUS_WILL_BE_SENT
    assert plan[second] == PHOTO_STATUS_ALREADY_SENT
    assert plan[third] == PHOTO_STATUS_WILL_BE_SENT
    assert plan[fourth] == PHOTO_STATUS_WILL_BE_SENT
    assert plan[fifth] == PHOTO_STATUS_NONE
    single = uuid.uuid4()
    assert photo_delivery_plan([single], set())[single] == PHOTO_STATUS_WILL_BE_SENT
    assert photo_delivery_plan([single], {single})[single] == PHOTO_STATUS_ALREADY_SENT


@pytest.mark.asyncio
async def test_writers_store_replayable_items_and_keep_display_text() -> None:
    merchant = await _seed_merchant()
    phone = "+221770099101"
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status=STATUS_ESCALATED,
            )
            db.add(conversation)
            await db.commit()
            conversation_id = conversation.id

        graph = _CapturingGraph()
        with patch("app.agent.orchestrator._build_graph", return_value=graph):
            async with AsyncSessionLocal() as db:
                reply = await traiter_message_entrant(
                    db, merchant.id, phone, "Vous avez cette robe?"
                )
        assert reply is None
        assert graph.input_lists == []

        async with AsyncSessionLocal() as db:
            merchant_row = await db.get(Merchant, merchant.id)
            assert merchant_row is not None
            with patch(
                "app.agent.service.envoyer_message_commercant",
                new_callable=AsyncMock,
            ):
                human = await repondre_en_humain(
                    db, merchant_row, conversation_id, "oui on l'a"
                )
            assert human.display_text == "oui on l'a"
            assert human.items == [item_human_reply("oui on l'a")]

            await enregistrer_message_commercant(
                db,
                conversation_id,
                "Bonjour, voici le lien Wave https://pay.example.com/x",
                items=[item_automatic_payment_link("Wave", 58)],
            )
            await db.commit()

            updated = await escalader_vers_humain(
                db, conversation_id, "Le client souhaite parler à un agent humain."
            )
            assert updated.status == STATUS_ESCALATED

        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(Message)
                        .where(Message.conversation_id == conversation_id)
                        .order_by(Message.created_at, Message.id)
                    )
                ).scalars().all()
            )
        by_display = {row.display_text: row for row in rows}
        escalated = by_display["Vous avez cette robe?"]
        assert escalated.turn_role == TURN_ROLE_CUSTOMER
        assert escalated.items == [item_customer_text("Vous avez cette robe?")]
        assert by_display["oui on l'a"].items == [item_human_reply("oui on l'a")]
        link_row = by_display["Bonjour, voici le lien Wave https://pay.example.com/x"]
        assert link_row.turn_role == TURN_ROLE_MERCHANT
        assert "https://pay.example.com/x" in link_row.display_text
        assert link_row.items == [item_automatic_payment_link("Wave", 58)]
        assert "http" not in link_row.items[0]["content"]
        escalade = next(row for row in rows if row.display_text.startswith("[escalade]"))
        assert escalade.items == []
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_photo_and_ack_writers(tmp_path: Path) -> None:
    merchant = await _seed_merchant()
    phone = "+221770099102"
    try:
        async with AsyncSessionLocal() as db:
            product = Product(
                merchant_id=merchant.id,
                name="Article preuve",
                category="tests",
                price=Decimal("4000.00"),
                stock_qty=8,
            )
            db.add(product)
            await db.commit()
            await db.refresh(product)
            product_id = product.id

        proof_phone = "+221770099106"
        async with AsyncSessionLocal() as db:
            order = await creer_commande(
                db,
                merchant_id=merchant.id,
                customer_phone=proof_phone,
                items=[(product_id, 1)],
                payment_method=PaymentMethod.online,
                delivery_address="Dakar",
                ville="Dakar",
            )
            order.payment_link = "https://pay.example.com/x"
            order.payment_link_sent_at = order.created_at
            await db.commit()
            order_number = order.order_number

        media_dir = str(tmp_path / "media")
        vision_other = AsyncMock(return_value=ImageAnalysis(False, None))
        vision_proof = AsyncMock(return_value=ImageAnalysis(True, Decimal("4000")))
        send = AsyncMock()
        with (
            patch("app.core.config.settings.media_dir", media_dir),
            patch(
                "app.proofs.service.telecharger_media_whatsapp",
                new=AsyncMock(return_value=(TINY_PNG, "image/png")),
            ),
            patch("app.proofs.service.envoyer_message_commercant", send),
        ):
            with patch("app.proofs.service.classer_image_entrante", vision_other):
                async with AsyncSessionLocal() as db:
                    merchant_row = await db.get(Merchant, merchant.id)
                    unanalysed = await traiter_image_entrante(
                        db, merchant_row, phone, "wamid.p1", "m1", "image/png", None
                    )
                    captioned = await traiter_image_entrante(
                        db,
                        merchant_row,
                        phone,
                        "wamid.p2",
                        "m2",
                        "image/png",
                        "la robe noire",
                    )
                    other = await traiter_image_entrante(
                        db,
                        merchant_row,
                        proof_phone,
                        "wamid.p3",
                        "m3",
                        "image/png",
                        None,
                    )
            with patch("app.proofs.service.classer_image_entrante", vision_proof):
                async with AsyncSessionLocal() as db:
                    merchant_row = await db.get(Merchant, merchant.id)
                    proof = await traiter_image_entrante(
                        db,
                        merchant_row,
                        proof_phone,
                        "wamid.p4",
                        "m4",
                        "image/png",
                        f"n°{order_number}",
                    )

        assert unanalysed.classification == CLASSIFICATION_OTHER
        assert captioned.classification == CLASSIFICATION_OTHER
        assert other.classification == CLASSIFICATION_OTHER
        assert proof.classification == CLASSIFICATION_PAYMENT_PROOF
        async with AsyncSessionLocal() as db:
            conversation_ids = list(
                (
                    await db.execute(
                        select(Conversation.id).where(
                            Conversation.merchant_id == merchant.id
                        )
                    )
                ).scalars().all()
            )
            rows = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.conversation_id.in_(conversation_ids)
                        )
                    )
                ).scalars().all()
            )
        by_display = {}
        for row in rows:
            by_display.setdefault(row.display_text, []).append(row)
        photo_plain = by_display["Photo"]
        assert any(
            row.items == [item_customer_photo(classification="not_analyzed")]
            for row in photo_plain
        )
        assert any(
            row.items == [item_customer_photo(classification="other")]
            for row in photo_plain
        )
        caption_row = by_display["la robe noire"][0]
        assert caption_row.display_text == "la robe noire"
        assert caption_row.items == [item_customer_photo(caption="la robe noire")]
        proof_row = by_display[f"n°{order_number}"][0]
        assert proof_row.display_text == f"n°{order_number}"
        assert proof_row.items == [
            item_customer_photo(
                classification="payment_proof",
                order_number=order_number,
            )
        ]
        ack_row = by_display[ACK_TEXT][0]
        assert ack_row.display_text == ACK_TEXT
        assert ack_row.items == [item_automatic_proof_ack()]
        send.assert_awaited()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_traiter_message_entrant_inserts_handover_note_before_customer() -> None:
    merchant = await _seed_merchant()
    phone = "+221770099104"
    graph = _CapturingGraph()
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status="active",
            )
            db.add(conversation)
            await db.flush()
            base = datetime.now(timezone.utc)
            db.add_all(
                [
                    Message(
                        conversation_id=conversation.id,
                        turn_role="agent",
                        display_text="Un membre de l'équipe va prendre le relais.",
                        items=[
                            {
                                "type": "function_call",
                                "name": "escalader_vers_humain",
                                "call_id": "c-esc",
                                "arguments": "{}",
                            },
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": "Un membre de l'équipe va prendre le relais.",
                            },
                        ],
                        created_at=base,
                    ),
                    Message(
                        conversation_id=conversation.id,
                        turn_role=TURN_ROLE_MERCHANT,
                        display_text="oui on l'a",
                        items=[item_human_reply("oui on l'a")],
                        created_at=base + timedelta(seconds=1),
                    ),
                    Message(
                        conversation_id=conversation.id,
                        turn_role=TURN_ROLE_CUSTOMER,
                        display_text="Photo",
                        items=[item_customer_photo(classification="not_analyzed")],
                        created_at=base + timedelta(seconds=2),
                    ),
                ]
            )
            await db.commit()

        with patch("app.agent.orchestrator._build_graph", return_value=graph):
            async with AsyncSessionLocal() as db:
                reply = await traiter_message_entrant(
                    db,
                    merchant.id,
                    phone,
                    "Je veux commande pour la robe ci-haut",
                )
        assert reply == "Réponse test"
        assert len(graph.input_lists) == 1
        input_list = graph.input_lists[0]
        assert input_list[0]["role"] == "developer"
        assert input_list[-1] == {
            "role": "user",
            "content": "Je veux commande pour la robe ci-haut",
        }
        notes = [
            item
            for item in input_list
            if item.get("role") == "developer"
            and item.get("content") == HANDOVER_DEVELOPER_TEXT
        ]
        assert len(notes) == 1
        assert input_list[-2] == notes[0]
        boutique = next(
            item
            for item in input_list
            if isinstance(item.get("content"), str)
            and item["content"].startswith(PREFIX_BOUTIQUE)
        )
        assert input_list.index(boutique) < input_list.index(notes[0])
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_execute_tool_photo_status_on_all_image_tools() -> None:
    query = _vector(0)
    merchant = await _seed_merchant()
    try:
        async with AsyncSessionLocal() as db:
            products = []
            for index in range(6):
                embedding = _vector(0)
                embedding[index + 1] = 0.02
                product = Product(
                    merchant_id=merchant.id,
                    name=f"Article photo {index}",
                    category="tests",
                    price=Decimal(str(40000 - index * 1000)),
                    stock_qty=6,
                    embedding=embedding,
                )
                products.append(product)
                db.add(product)
            bare = Product(
                merchant_id=merchant.id,
                name="Sans photo",
                category="tests",
                price=Decimal("1000.00"),
                stock_qty=6,
            )
            db.add(bare)
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770099105",
                status="active",
            )
            db.add(conversation)
            await db.flush()
            for product in products:
                db.add(
                    ProductImage(
                        product_id=product.id,
                        url=f"/static/product_images/{product.id}.png",
                    )
                )
            db.add(
                SentProductImage(
                    conversation_id=conversation.id,
                    product_id=products[0].id,
                )
            )
            await db.commit()
            ids = [product.id for product in products]
            bare_id = bare.id
            conversation_id = conversation.id
            merchant_id = merchant.id

        with patch("app.catalogue.service.embed_query", return_value=query):
            async with AsyncSessionLocal() as db:
                search = json.loads(
                    await execute_tool(
                        db,
                        "rechercher_produits",
                        {"requete": "article", "categorie": None},
                        merchant_id,
                        conversation_id,
                    )
                )
                popular = json.loads(
                    await execute_tool(
                        db,
                        "lister_produits_populaires",
                        {},
                        merchant_id,
                        conversation_id,
                    )
                )
                similar = json.loads(
                    await execute_tool(
                        db,
                        "trouver_produits_similaires",
                        {"produit_id": str(ids[1])},
                        merchant_id,
                        conversation_id,
                    )
                )
                avail_sent = json.loads(
                    await execute_tool(
                        db,
                        "obtenir_disponibilite",
                        {"produit_id": str(ids[0])},
                        merchant_id,
                        conversation_id,
                    )
                )
                avail_new = json.loads(
                    await execute_tool(
                        db,
                        "obtenir_disponibilite",
                        {"produit_id": str(ids[1])},
                        merchant_id,
                        conversation_id,
                    )
                )
                avail_none = json.loads(
                    await execute_tool(
                        db,
                        "obtenir_disponibilite",
                        {"produit_id": str(bare_id)},
                        merchant_id,
                        conversation_id,
                    )
                )

        def _by_id(payload: dict) -> dict[str, dict]:
            return {
                item["id"]: item
                for item in payload.get("products") or []
                if "id" in item
            }

        search_by_id = _by_id(search)
        popular_by_id = {
            item.get("product_id") or item.get("id"): item
            for item in popular["products"]
        }
        similar_by_id = _by_id(similar)
        for tool_rows in (search_by_id, popular_by_id, similar_by_id):
            imaged = [row for row in tool_rows.values() if row.get("image_url")]
            queue = [
                uuid.UUID(str(row.get("id") or row.get("product_id")))
                for row in imaged
            ]
            expected = photo_delivery_plan(queue, {ids[0]})
            for row in imaged:
                product_id = uuid.UUID(str(row.get("id") or row.get("product_id")))
                assert row["photo_status"] == expected[product_id]
                assert row["image_url"]

        for name, payload in (
            ("search", search),
            ("popular", popular),
            ("similar", similar),
        ):
            rows = payload["products"]
            statuses = {item["photo_status"] for item in rows}
            assert PHOTO_STATUS_WILL_BE_SENT in statuses, name
            assert PHOTO_STATUS_ALREADY_SENT in statuses, name
            assert PHOTO_STATUS_NONE in statuses, name

        assert avail_sent["photo_status"] == PHOTO_STATUS_ALREADY_SENT
        assert avail_sent["image_url"]
        assert avail_new["photo_status"] == PHOTO_STATUS_WILL_BE_SENT
        assert avail_none["photo_status"] == PHOTO_STATUS_NONE
        assert avail_none["image_url"] is None

        extract_items = [
            {
                "type": "function_call_output",
                "output": json.dumps(avail_sent),
            }
        ]
        images = extract_product_images(extract_items)
        assert len(images) == 1
        assert images[0].product_id == ids[0]
    finally:
        await _cleanup(merchant.id)
