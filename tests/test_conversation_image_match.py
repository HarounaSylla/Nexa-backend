"""Thread image payload exposes visual-search match fields. No network."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, event, select

from app.agent.handover import item_customer_photo
from app.agent.models import Conversation, Message
from app.agent.service import TURN_ROLE_CUSTOMER
from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal, engine
from app.main import app
from app.proofs.models import (
    CLASSIFICATION_PRODUCT_PHOTO,
    MATCH_LEVEL_ERROR,
    MATCH_LEVEL_NONE,
    MATCH_LEVEL_POSSIBLE,
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
            price=Decimal("25000.00"),
            stock_qty=4,
        )
        db.add(product)
        await db.commit()
        await db.refresh(product)
        return product


async def _add_photo(
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    *,
    wamid: str,
    match_level: str | None,
    matched_product_id: uuid.UUID | None,
    match_candidates: list | None,
) -> InboundImage:
    async with AsyncSessionLocal() as db:
        message = Message(
            conversation_id=conversation_id,
            turn_role=TURN_ROLE_CUSTOMER,
            display_text="Photo",
            items=[item_customer_photo(classification=CLASSIFICATION_PRODUCT_PHOTO)],
            created_at=datetime.now(timezone.utc),
        )
        db.add(message)
        await db.flush()
        image = InboundImage(
            merchant_id=merchant_id,
            conversation_id=conversation_id,
            message_id=message.id,
            whatsapp_message_id=wamid,
            media_path=f"{merchant_id}/{uuid.uuid4()}.png",
            mime_type="image/png",
            classification=CLASSIFICATION_PRODUCT_PHOTO,
            match_level=match_level,
            matched_product_id=matched_product_id,
            match_candidates=match_candidates,
        )
        db.add(image)
        await db.commit()
        await db.refresh(image)
        return image


def _product_selects(statements: list[str]) -> list[str]:
    found = []
    for statement in statements:
        compact = " ".join(statement.lower().split())
        if "from products" in compact:
            found.append(statement)
    return found


@pytest.mark.asyncio
async def test_thread_images_expose_match_level_name_and_kind() -> None:
    clerk = f"user_img_match_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-img-match-{uuid.uuid4()}", clerk)
    product = await _seed_product(merchant.id, "Robe longue rouge de soirée")
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001201",
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
        strong = await _add_photo(
            merchant.id,
            conversation.id,
            wamid="wamid.strong",
            match_level=MATCH_LEVEL_STRONG,
            matched_product_id=product.id,
            match_candidates=[
                {
                    "product_id": str(product.id),
                    "distance": 0.12,
                    "verdict": "same",
                    "verifier": "gpt-test",
                }
            ],
        )
        similar = await _add_photo(
            merchant.id,
            conversation.id,
            wamid="wamid.similar",
            match_level=MATCH_LEVEL_POSSIBLE,
            matched_product_id=product.id,
            match_candidates=[
                {
                    "product_id": str(product.id),
                    "distance": 0.22,
                    "verdict": "similar",
                    "verifier": "gpt-test",
                }
            ],
        )
        none = await _add_photo(
            merchant.id,
            conversation.id,
            wamid="wamid.none",
            match_level=MATCH_LEVEL_NONE,
            matched_product_id=None,
            match_candidates=[{"product_id": str(product.id), "distance": 0.80}],
        )
        error = await _add_photo(
            merchant.id,
            conversation.id,
            wamid="wamid.error",
            match_level=MATCH_LEVEL_ERROR,
            matched_product_id=None,
            match_candidates=[],
        )
        with _auth(clerk):
            async with await _client() as client:
                response = await client.get(
                    f"/conversations/{conversation.id}/messages",
                    headers=_headers(),
                )
        assert response.status_code == 200, response.text
        by_id = {
            row["image"]["id"]: row["image"]
            for row in response.json()
            if row["image"] is not None
        }
        assert by_id[str(strong.id)]["match_level"] == "strong"
        assert by_id[str(strong.id)]["matched_product_id"] == str(product.id)
        assert by_id[str(strong.id)]["matched_product_name"] == "Robe longue rouge de soirée"
        assert by_id[str(strong.id)]["match_kind"] == "exact"
        assert "distance" not in by_id[str(strong.id)]
        assert "match_candidates" not in by_id[str(strong.id)]
        assert "verifier" not in by_id[str(strong.id)]
        assert by_id[str(similar.id)]["match_kind"] == "similar"
        assert by_id[str(similar.id)]["matched_product_name"] == product.name
        assert by_id[str(none.id)]["match_level"] == "none"
        assert by_id[str(none.id)]["matched_product_id"] is None
        assert by_id[str(none.id)]["matched_product_name"] is None
        assert by_id[str(none.id)]["match_kind"] is None
        assert by_id[str(error.id)]["match_level"] == "error"
        assert by_id[str(error.id)]["matched_product_id"] is None
        assert by_id[str(error.id)]["matched_product_name"] is None
        assert by_id[str(error.id)]["match_kind"] is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_other_merchant_product_is_never_named() -> None:
    owner_clerk = f"user_img_owner_{uuid.uuid4()}"
    other_clerk = f"user_img_other_{uuid.uuid4()}"
    owner = await _seed_merchant(f"pytest-img-owner-{uuid.uuid4()}", owner_clerk)
    other = await _seed_merchant(f"pytest-img-other-{uuid.uuid4()}", other_clerk)
    foreign = await _seed_product(other.id, "Secret other shop robe")
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=owner.id,
                customer_phone="+221770001202",
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
        image = await _add_photo(
            owner.id,
            conversation.id,
            wamid="wamid.foreign",
            match_level=MATCH_LEVEL_STRONG,
            matched_product_id=foreign.id,
            match_candidates=[
                {"product_id": str(foreign.id), "distance": 0.11, "verdict": "same"}
            ],
        )
        with _auth(owner_clerk):
            async with await _client() as client:
                response = await client.get(
                    f"/conversations/{conversation.id}/messages",
                    headers=_headers(),
                )
        payload = next(
            row["image"] for row in response.json() if row["image"] is not None
        )
        assert payload["id"] == str(image.id)
        assert payload["matched_product_id"] == str(foreign.id)
        assert payload["matched_product_name"] is None
        assert "Secret" not in response.text
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_legacy_image_rows_serialise_new_fields_as_null() -> None:
    clerk = f"user_img_legacy_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-img-legacy-{uuid.uuid4()}", clerk)
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001203",
            )
            db.add(conversation)
            await db.flush()
            message = Message(
                conversation_id=conversation.id,
                turn_role=TURN_ROLE_CUSTOMER,
                display_text="Photo",
                items=[item_customer_photo()],
            )
            db.add(message)
            await db.flush()
            image = InboundImage(
                merchant_id=merchant.id,
                conversation_id=conversation.id,
                message_id=message.id,
                whatsapp_message_id="wamid.legacy",
                media_path=f"{merchant.id}/{uuid.uuid4()}.png",
                mime_type="image/png",
                classification="payment_proof",
            )
            db.add(image)
            await db.commit()
            await db.refresh(conversation)
            await db.refresh(image)
        with _auth(clerk):
            async with await _client() as client:
                response = await client.get(
                    f"/conversations/{conversation.id}/messages",
                    headers=_headers(),
                )
        payload = next(
            row["image"] for row in response.json() if row["image"] is not None
        )
        assert payload["id"] == str(image.id)
        assert payload["classification"] == "payment_proof"
        assert payload["order_id"] is None
        assert payload["detected_amount"] is None
        assert payload["deleted"] is False
        assert payload["match_level"] is None
        assert payload["matched_product_id"] is None
        assert payload["matched_product_name"] is None
        assert payload["match_kind"] is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_product_names_use_one_query_regardless_of_image_count() -> None:
    clerk = f"user_img_n1_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-img-n1-{uuid.uuid4()}", clerk)
    product = await _seed_product(merchant.id, "Sac à main camel")
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001204",
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
        for index in range(5):
            await _add_photo(
                merchant.id,
                conversation.id,
                wamid=f"wamid.n1-{index}",
                match_level=MATCH_LEVEL_STRONG,
                matched_product_id=product.id,
                match_candidates=[
                    {"product_id": str(product.id), "distance": 0.1, "verdict": "same"}
                ],
            )
        captured: list[str] = []

        def _on_execute(conn, cursor, statement, parameters, context, executemany):
            captured.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
        try:
            with _auth(clerk):
                async with await _client() as client:
                    response = await client.get(
                        f"/conversations/{conversation.id}/messages",
                        headers=_headers(),
                    )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _on_execute)
        assert response.status_code == 200, response.text
        photos = [row["image"] for row in response.json() if row["image"] is not None]
        assert len(photos) == 5
        assert all(row["matched_product_name"] == "Sac à main camel" for row in photos)
        assert len(_product_selects(captured)) == 1
    finally:
        await _cleanup(merchant.id)
