"""Merchant thread payload exposes the quoted WhatsApp message. No network."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, event, select

from app.agent.handover import (
    KIND_CUSTOMER_PHOTO,
    KIND_CUSTOMER_TEXT,
    KIND_SHOP_PHOTO,
    KIND_SHOP_TEXT,
    trim_quote_excerpt,
)
from app.agent.models import Conversation, Message, WhatsAppMessageRef
from app.agent.service import (
    STATUS_CLOSED,
    TURN_ROLE_AGENT,
    TURN_ROLE_CUSTOMER,
    record_whatsapp_message_ref,
)
from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal, engine
from app.main import app
from app.proofs.models import (
    CLASSIFICATION_PRODUCT_PHOTO,
    MATCH_LEVEL_NONE,
    MATCH_LEVEL_STRONG,
    InboundImage,
)


def _auth(clerk_user_id: str):
    return patch(
        "app.auth.deps.verify_clerk_session_token",
        return_value=clerk_user_id,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid.token"}


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
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
                delete(InboundImage).where(
                    InboundImage.conversation_id.in_(conversation_ids)
                )
            )
            await db.execute(
                delete(WhatsAppMessageRef).where(
                    WhatsAppMessageRef.conversation_id.in_(conversation_ids)
                )
            )
            await db.execute(
                delete(Message).where(Message.conversation_id.in_(conversation_ids))
            )
            await db.execute(
                delete(Conversation).where(Conversation.id.in_(conversation_ids))
            )
        await db.execute(delete(Product).where(Product.merchant_id.in_(merchant_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_merchant(name: str, clerk_user_id: str) -> Merchant:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name, clerk_user_id=clerk_user_id)
        db.add(merchant)
        await db.commit()
        await db.refresh(merchant)
        return merchant


async def _seed_product(merchant_id: uuid.UUID, name: str) -> Product:
    async with AsyncSessionLocal() as db:
        product = Product(
            merchant_id=merchant_id,
            name=name,
            price=Decimal("9500"),
            stock_qty=4,
        )
        db.add(product)
        await db.commit()
        await db.refresh(product)
        return product


async def _add_conversation(
    merchant_id: uuid.UUID,
    phone: str,
    *,
    status: str = "active",
) -> Conversation:
    async with AsyncSessionLocal() as db:
        conversation = Conversation(
            merchant_id=merchant_id,
            customer_phone=phone,
            status=status,
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        return conversation


async def _add_message(
    conversation_id: uuid.UUID,
    turn_role: str,
    text: str,
) -> Message:
    async with AsyncSessionLocal() as db:
        row = Message(
            conversation_id=conversation_id,
            turn_role=turn_role,
            display_text=text,
            items=[],
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _ref(
    conversation_id: uuid.UUID,
    wamid: str,
    kind: str,
    *,
    message_id: uuid.UUID | None = None,
    product_id: uuid.UUID | None = None,
    excerpt: str | None = None,
    reply_to: str | None = None,
) -> None:
    async with AsyncSessionLocal() as db:
        await record_whatsapp_message_ref(
            db,
            conversation_id,
            wamid,
            kind,
            message_id=message_id,
            product_id=product_id,
            excerpt=excerpt,
            reply_to_wamid=reply_to,
            commit=True,
        )


async def _quote(
    conversation_id: uuid.UUID,
    text: str,
    wamid: str,
    reply_to: str,
    *,
    kind: str = KIND_CUSTOMER_TEXT,
) -> Message:
    row = await _add_message(conversation_id, TURN_ROLE_CUSTOMER, text)
    await _ref(
        conversation_id,
        wamid,
        kind,
        message_id=row.id,
        excerpt=text,
        reply_to=reply_to,
    )
    return row


async def _get_thread(clerk: str, conversation_id: uuid.UUID):
    with _auth(clerk):
        async with await _client() as client:
            response = await client.get(
                f"/conversations/{conversation_id}/messages",
                headers=_headers(),
            )
    return response


def _by_id(payload: list[dict], message_id: uuid.UUID) -> dict:
    for row in payload:
        if row["id"] == str(message_id):
            return row
    raise AssertionError(f"message {message_id} missing from thread")


@pytest.mark.asyncio
async def test_quoted_shop_text_and_shop_photo() -> None:
    clerk = f"user_quote_shop_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-quote-api-{uuid.uuid4()}", clerk)
    product = await _seed_product(merchant.id, "Montre femme or rose")
    try:
        conversation = await _add_conversation(merchant.id, "+221770001801")
        shop = await _add_message(
            conversation.id, TURN_ROLE_AGENT, "La montre est à 9 500 F"
        )
        await _ref(
            conversation.id,
            "wamid.shop-text",
            KIND_SHOP_TEXT,
            message_id=shop.id,
            excerpt="La montre est à 9 500 F",
        )
        await _ref(
            conversation.id,
            "wamid.shop-photo",
            KIND_SHOP_PHOTO,
            product_id=product.id,
        )
        quoting_text = await _quote(
            conversation.id, "non, 2", "wamid.reply-text", "wamid.shop-text"
        )
        quoting_photo = await _quote(
            conversation.id, "celle-ci", "wamid.reply-photo", "wamid.shop-photo"
        )

        response = await _get_thread(clerk, conversation.id)
        assert response.status_code == 200, response.text
        body = response.json()
        text_quote = _by_id(body, quoting_text.id)["quoted"]
        assert text_quote == {
            "kind": KIND_SHOP_TEXT,
            "excerpt": "La montre est à 9 500 F",
            "product_name": None,
            "from_earlier_conversation": False,
            "message_id": str(shop.id),
        }
        photo_quote = _by_id(body, quoting_photo.id)["quoted"]
        assert photo_quote["kind"] == KIND_SHOP_PHOTO
        assert photo_quote["excerpt"] is None
        assert photo_quote["product_name"] == "Montre femme or rose"
        assert photo_quote["from_earlier_conversation"] is False
        assert photo_quote["message_id"] is None
        assert _by_id(body, shop.id)["quoted"] is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_quoted_customer_photo_recognised_and_not() -> None:
    clerk = f"user_quote_own_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-quote-own-{uuid.uuid4()}", clerk)
    product = await _seed_product(merchant.id, "Sac à main en cuir camel")
    try:
        conversation = await _add_conversation(merchant.id, "+221770001802")
        async with AsyncSessionLocal() as db:
            db.add(
                InboundImage(
                    merchant_id=merchant.id,
                    conversation_id=conversation.id,
                    whatsapp_message_id="wamid.own-strong",
                    mime_type="image/png",
                    classification=CLASSIFICATION_PRODUCT_PHOTO,
                    match_level=MATCH_LEVEL_STRONG,
                    matched_product_id=product.id,
                )
            )
            db.add(
                InboundImage(
                    merchant_id=merchant.id,
                    conversation_id=conversation.id,
                    whatsapp_message_id="wamid.own-none",
                    mime_type="image/png",
                    classification=CLASSIFICATION_PRODUCT_PHOTO,
                    match_level=MATCH_LEVEL_NONE,
                )
            )
            await db.commit()
        await _ref(conversation.id, "wamid.own-strong", KIND_CUSTOMER_PHOTO)
        await _ref(conversation.id, "wamid.own-none", KIND_CUSTOMER_PHOTO)
        yes = await _quote(
            conversation.id, "je la prends", "wamid.reply-strong", "wamid.own-strong"
        )
        no = await _quote(
            conversation.id, "et ça ?", "wamid.reply-none", "wamid.own-none"
        )

        response = await _get_thread(clerk, conversation.id)
        assert response.status_code == 200, response.text
        body = response.json()
        recognised = _by_id(body, yes.id)["quoted"]
        assert recognised["kind"] == KIND_CUSTOMER_PHOTO
        assert recognised["product_name"] == "Sac à main en cuir camel"
        assert recognised["excerpt"] is None
        unknown = _by_id(body, no.id)["quoted"]
        assert unknown["kind"] == KIND_CUSTOMER_PHOTO
        assert unknown["product_name"] is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_quoted_earlier_conversation_hides_message_id() -> None:
    clerk = f"user_quote_old_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-quote-old-{uuid.uuid4()}", clerk)
    try:
        closed = await _add_conversation(
            merchant.id, "+221770001803", status=STATUS_CLOSED
        )
        current = await _add_conversation(merchant.id, "+221770001803")
        old_shop = await _add_message(closed.id, TURN_ROLE_AGENT, "Ancien recap")
        await _ref(
            closed.id,
            "wamid.old-shop",
            KIND_SHOP_TEXT,
            message_id=old_shop.id,
            excerpt="Ancien recap",
        )
        quoting = await _quote(
            current.id, "non, 1", "wamid.reply-old", "wamid.old-shop"
        )

        response = await _get_thread(clerk, current.id)
        assert response.status_code == 200, response.text
        quoted = _by_id(response.json(), quoting.id)["quoted"]
        assert quoted["kind"] == KIND_SHOP_TEXT
        assert quoted["excerpt"] == "Ancien recap"
        assert quoted["from_earlier_conversation"] is True
        assert quoted["message_id"] is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_unresolvable_and_foreign_wamids_are_none() -> None:
    clerk = f"user_quote_scope_{uuid.uuid4()}"
    other_clerk = f"user_quote_scope_b_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-quote-scope-{uuid.uuid4()}", clerk)
    other = await _seed_merchant(f"pytest-quote-scope-b-{uuid.uuid4()}", other_clerk)
    try:
        own = await _add_conversation(merchant.id, "+221770001804")
        other_customer = await _add_conversation(merchant.id, "+221770001805")
        other_shop = await _add_conversation(other.id, "+221770001804")
        await _ref(
            other_customer.id,
            "wamid.other-customer",
            KIND_SHOP_TEXT,
            excerpt="secret other customer",
        )
        await _ref(
            other_shop.id,
            "wamid.other-merchant",
            KIND_SHOP_TEXT,
            excerpt="secret other merchant",
        )
        unknown = await _quote(
            own.id, "oui", "wamid.reply-unknown", "wamid.does-not-exist"
        )
        leaked_customer = await _quote(
            own.id, "ok", "wamid.reply-cust", "wamid.other-customer"
        )
        leaked_shop = await _quote(
            own.id, "d'accord", "wamid.reply-shop", "wamid.other-merchant"
        )

        response = await _get_thread(clerk, own.id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert _by_id(body, unknown.id)["quoted"] is None
        assert _by_id(body, leaked_customer.id)["quoted"] is None
        assert _by_id(body, leaked_shop.id)["quoted"] is None
    finally:
        await _cleanup(merchant.id, other.id)


@pytest.mark.asyncio
async def test_excerpt_is_trimmed_and_falls_back_to_display_text() -> None:
    clerk = f"user_quote_trim_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-quote-trim-{uuid.uuid4()}", clerk)
    try:
        conversation = await _add_conversation(merchant.id, "+221770001806")
        long_text = "Bonjour " + ("robe " * 80)
        shop = await _add_message(conversation.id, TURN_ROLE_AGENT, long_text)
        await _ref(
            conversation.id,
            "wamid.long",
            KIND_SHOP_TEXT,
            message_id=shop.id,
            excerpt=long_text,
        )
        silent = await _add_message(
            conversation.id, TURN_ROLE_AGENT, "Texte affiché seulement"
        )
        await _ref(
            conversation.id,
            "wamid.fallback",
            KIND_SHOP_TEXT,
            message_id=silent.id,
        )
        long_quote = await _quote(
            conversation.id, "ok", "wamid.reply-long", "wamid.long"
        )
        fallback_quote = await _quote(
            conversation.id, "ça", "wamid.reply-fb", "wamid.fallback"
        )

        response = await _get_thread(clerk, conversation.id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert _by_id(body, long_quote.id)["quoted"]["excerpt"] == trim_quote_excerpt(
            long_text
        )
        assert (
            _by_id(body, fallback_quote.id)["quoted"]["excerpt"]
            == "Texte affiché seulement"
        )
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_thread_without_quotes_has_null_quoted() -> None:
    clerk = f"user_quote_none_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-quote-none-{uuid.uuid4()}", clerk)
    try:
        conversation = await _add_conversation(merchant.id, "+221770001807")
        await _add_message(conversation.id, TURN_ROLE_CUSTOMER, "bonjour")
        await _add_message(conversation.id, TURN_ROLE_AGENT, "oui ?")

        response = await _get_thread(clerk, conversation.id)
        assert response.status_code == 200, response.text
        body = response.json()
        assert [row["quoted"] for row in body] == [None, None]
        assert {key for row in body for key in row} >= {
            "id",
            "turn_role",
            "display_text",
            "created_at",
            "image",
            "quoted",
        }
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_quote_query_count_does_not_grow_with_quotes() -> None:
    clerk = f"user_quote_n_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-quote-n-{uuid.uuid4()}", clerk)
    try:
        small = await _add_conversation(merchant.id, "+221770001808")
        shop_small = await _add_message(small.id, TURN_ROLE_AGENT, "Proposition")
        await _ref(
            small.id,
            "wamid.n-small",
            KIND_SHOP_TEXT,
            message_id=shop_small.id,
            excerpt="Proposition",
        )
        await _quote(small.id, "oui", "wamid.n-small-r", "wamid.n-small")

        big = await _add_conversation(merchant.id, "+221770001809")
        for index in range(8):
            shop = await _add_message(big.id, TURN_ROLE_AGENT, f"Proposition {index}")
            await _ref(
                big.id,
                f"wamid.n-big-{index}",
                KIND_SHOP_TEXT,
                message_id=shop.id,
                excerpt=f"Proposition {index}",
            )
            await _quote(
                big.id,
                f"ok {index}",
                f"wamid.n-big-r-{index}",
                f"wamid.n-big-{index}",
            )

        async def _measure(conversation_id: uuid.UUID) -> tuple[int, httpx.Response]:
            captured: list[str] = []

            def _on_execute(conn, cursor, statement, parameters, context, executemany):
                captured.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
            try:
                response = await _get_thread(clerk, conversation_id)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", _on_execute)
            return len(captured), response

        small_count, small_response = await _measure(small.id)
        big_count, big_response = await _measure(big.id)
        assert small_response.status_code == 200, small_response.text
        assert big_response.status_code == 200, big_response.text
        assert sum(1 for row in big_response.json() if row["quoted"]) == 8
        assert small_count <= 20
        assert big_count <= 20
        assert abs(big_count - small_count) <= 2
    finally:
        await _cleanup(merchant.id)
