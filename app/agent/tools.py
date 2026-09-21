"""OpenAI Responses API function-tool schemas and dispatcher.

Eight socle tools: rechercher_produits, lister_categories,
lister_produits_populaires, trouver_produits_similaires,
obtenir_disponibilite, verifier_zone_livraison, creer_commande,
escalader_vers_humain.
"""

from __future__ import annotations

import json
import uuid
from typing import Any


from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.models import Product
from app.catalogue.service import (
    ProductNotFoundError,
    ProductNotIndexedError,
    image_urls_for_products,
    lister_categories,
    lister_produits_populaires,
    rechercher_produits,
    trouver_produits_similaires,
)
from app.orders.models import PaymentMethod
from app.orders.service import (
    DeliveryNotAvailableError,
    InsufficientStockError,
    InvalidOrderStateError,
    NotFoundError,
    creer_commande,
    obtenir_disponibilite,
    obtenir_zone_livraison,
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "rechercher_produits",
        "description": (
            "Search this merchant's catalogue by a customer phrase. "
            "Returns products whose embedding is within the relevance "
            "threshold. Empty list means nothing relevant — do not invent."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requete": {
                    "type": "string",
                    "description": "Customer search phrase, as they said it.",
                },
                "categorie": {
                    "type": ["string", "null"],
                    "description": "Optional category filter, or null.",
                },
            },
            "required": ["requete", "categorie"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "lister_categories",
        "description": (
            "List this merchant's in-stock catalogue categories with "
            "product counts. Use when the customer asks a broad question "
            "(what do you sell, show me your products) instead of a "
            "specific product search. Do not use this for popularity or "
            "best-seller questions — use lister_produits_populaires."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "lister_produits_populaires",
        "description": (
            "Return this merchant's top-selling products (distinct "
            "non-cancelled orders, ties broken by price). Use when the "
            "customer asks what sells well, what's popular, best sellers, "
            "or wants a recommendation. Skip rupture items when "
            "presenting to the customer."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "trouver_produits_similaires",
        "description": (
            "Find similar products in the same category as a known product. "
            "Use this to offer an alternative when the requested item is "
            "unavailable or a poor match."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "produit_id": {
                    "type": "string",
                    "description": "UUID of the source product.",
                },
            },
            "required": ["produit_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "obtenir_disponibilite",
        "description": "Return qualitative stock status (disponible / stock_faible / rupture) for a product UUID. Never includes an exact quantity.",
        "parameters": {
            "type": "object",
            "properties": {
                "produit_id": {
                    "type": "string",
                    "description": "UUID of the product.",
                },
            },
            "required": ["produit_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "verifier_zone_livraison",
        "description": (
            "Check whether this merchant delivers to a city and in how "
            "many hours. Call this after the customer names a city, "
            "before confirming an order. Returns available=false if the "
            "city is not covered or is explicitly disabled."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ville": {
                    "type": "string",
                    "description": "City name as given by the customer.",
                },
            },
            "required": ["ville"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "creer_commande",
        "description": (
            "Create an order and decrement stock. Call only after the "
            "customer has explicitly confirmed product+quantity, a delivery "
            "address, a city (ville), and a payment method, and after "
            "verifier_zone_livraison returned available. Merchant and phone "
            "come from conversation context — do not ask the model to "
            "supply those."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "product_id": {"type": "string"},
                            "quantity": {"type": "integer"},
                        },
                        "required": ["product_id", "quantity"],
                        "additionalProperties": False,
                    },
                },
                "mode_paiement": {
                    "type": "string",
                    "enum": ["cash_on_delivery", "online"],
                    "description": (
                        "cash_on_delivery = à la livraison; online = en ligne."
                    ),
                },
                "adresse_livraison": {"type": "string"},
                "ville": {
                    "type": "string",
                    "description": "City, confirmed separately from the street address.",
                },
            },
            "required": ["items", "mode_paiement", "adresse_livraison", "ville"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function",
        "name": "escalader_vers_humain",
        "description": (
            "Hand the conversation to a human. Use when the customer asks "
            "for a person, or for a complaint/negotiation/dispute, or after "
            "two failed clarification attempts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "raison": {
                    "type": "string",
                    "description": "Why the agent is escalating.",
                },
            },
            "required": ["raison"],
            "additionalProperties": False,
        },
        "strict": True,
    },
]


def _stock_status(stock_qty: int) -> str:
    """Map an exact stock quantity to a qualitative status the model is
    allowed to see: "rupture" (0), "stock_faible" (1-3), "disponible"
    (4+). Threshold is a placeholder, easy to tune later — the point is
    that no agent-facing tool result ever carries the raw integer.
    """
    if stock_qty <= 0:
        return "rupture"
    if stock_qty <= 3:
        return "stock_faible"
    return "disponible"


def _product_payload(product: Product, image_url: str | None = None) -> dict[str, Any]:
    return {
        "id": str(product.id),
        "name": product.name,
        "category": product.category,
        "price": str(product.price) if product.price is not None else None,
        "stock_status": _stock_status(product.stock_qty),
        "image_url": image_url,
    }


def _popular_product_payload(
    row: dict[str, Any], image_url: str | None = None
) -> dict[str, Any]:
    payload = dict(row)
    stock_qty = int(payload.pop("stock_qty"))
    payload["stock_status"] = _stock_status(stock_qty)
    payload["image_url"] = image_url
    return payload


def _parse_uuid(value: str, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {value}") from exc


def _payment_method(raw: str) -> PaymentMethod:
    normalized = raw.strip().lower().replace("é", "e").replace(" ", "_")
    aliases = {
        "cash_on_delivery": PaymentMethod.cash_on_delivery,
        "a_la_livraison": PaymentMethod.cash_on_delivery,
        "livraison": PaymentMethod.cash_on_delivery,
        "online": PaymentMethod.online,
        "en_ligne": PaymentMethod.online,
    }
    if normalized not in aliases:
        raise ValueError(f"Unknown payment method: {raw}")
    return aliases[normalized]


async def execute_tool(
    db: AsyncSession,
    tool_name: str,
    tool_args: dict[str, Any],
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> str:
    """Route to the right service function.

    Domain errors are returned as {"error": "..."} JSON so the model can
    react in French instead of crashing the turn.
    """
    try:
        payload = await _dispatch(
            db, tool_name, tool_args, merchant_id, conversation_id
        )
        return json.dumps(payload, default=str)
    except (
        InsufficientStockError,
        NotFoundError,
        ProductNotFoundError,
        ProductNotIndexedError,
        InvalidOrderStateError,
        DeliveryNotAvailableError,
        ValueError,
    ) as exc:
        return json.dumps({"error": str(exc)})


async def _dispatch(
    db: AsyncSession,
    tool_name: str,
    tool_args: dict[str, Any],
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> dict[str, Any]:
    if tool_name == "rechercher_produits":
        products = await rechercher_produits(
            db,
            merchant_id=merchant_id,
            requete=str(tool_args["requete"]),
            categorie=tool_args.get("categorie"),
        )
        urls = await image_urls_for_products(db, [product.id for product in products])
        return {
            "products": [
                _product_payload(product, urls.get(product.id))
                for product in products
            ]
        }

    if tool_name == "lister_categories":
        return {"categories": await lister_categories(db, merchant_id)}

    if tool_name == "lister_produits_populaires":
        rows = await lister_produits_populaires(db, merchant_id, limit=10)
        urls = await image_urls_for_products(
            db, [row["product_id"] for row in rows]
        )
        return {
            "products": [
                _popular_product_payload(row, urls.get(row["product_id"]))
                for row in rows
            ]
        }

    if tool_name == "trouver_produits_similaires":
        product_id = _parse_uuid(tool_args["produit_id"], "produit_id")
        products = await trouver_produits_similaires(
            db, merchant_id=merchant_id, produit_id=product_id
        )
        urls = await image_urls_for_products(db, [product.id for product in products])
        return {
            "products": [
                _product_payload(product, urls.get(product.id))
                for product in products
            ]
        }

    if tool_name == "obtenir_disponibilite":
        product_id = _parse_uuid(tool_args["produit_id"], "produit_id")
        stock_qty = await obtenir_disponibilite(db, product_id)
        urls = await image_urls_for_products(db, [product_id])
        return {
            "product_id": str(product_id),
            "stock_status": _stock_status(stock_qty),
            "image_url": urls.get(product_id),
        }

    if tool_name == "verifier_zone_livraison":
        zone = await obtenir_zone_livraison(
            db, merchant_id, str(tool_args["ville"])
        )
        if zone is None or not zone.available:
            return {"available": False}
        return {
            "available": True,
            "min_delivery_hours": zone.min_delivery_hours,
            "max_delivery_hours": zone.max_delivery_hours,
        }

    if tool_name == "creer_commande":
        from app.agent.models import Conversation

        conversation = await db.get(Conversation, conversation_id)
        if conversation is None:
            raise NotFoundError(f"Conversation {conversation_id} was not found")
        raw_items = tool_args.get("items") or []
        items = [
            (
                _parse_uuid(item["product_id"], "product_id"),
                int(item["quantity"]),
            )
            for item in raw_items
        ]
        order = await creer_commande(
            db,
            merchant_id=merchant_id,
            customer_phone=conversation.customer_phone,
            items=items,
            payment_method=_payment_method(str(tool_args["mode_paiement"])),
            delivery_address=str(tool_args["adresse_livraison"]),
            ville=str(tool_args["ville"]),
        )
        return {
            "order_id": str(order.id),
            "status": order.status.value,
            "payment_method": order.payment_method.value,
            "city": order.city,
            "items": [
                {
                    "product_id": str(item.product_id),
                    "quantity": item.quantity,
                    "unit_price": str(item.unit_price),
                }
                for item in order.items
            ],
        }

    if tool_name == "escalader_vers_humain":
        from app.agent.service import escalader_vers_humain

        conversation = await escalader_vers_humain(
            db, conversation_id, str(tool_args["raison"])
        )
        return {"status": conversation.status, "raison": tool_args["raison"]}

    raise ValueError(f"Unknown tool: {tool_name}")
