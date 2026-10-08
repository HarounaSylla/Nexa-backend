"""On-demand analyser_photo_client tool. Vision / Voyage / LLM are stubbed."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, func, select

from app.agent.images import extract_product_images
from app.agent.models import Conversation, Message
from app.agent.orchestrator import _is_replayable_history_item
from app.agent.tools import TOOLS, execute_tool
from app.catalogue.models import Merchant, Product
from app.catalogue.service import ImageMatch
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.notifications.models import Notification, NotificationType
from app.proofs.models import (
    CLASSIFICATION_NOT_ANALYZED,
    CLASSIFICATION_OTHER,
    CLASSIFICATION_PAYMENT_PROOF,
    CLASSIFICATION_PRODUCT_PHOTO,
    MATCH_LEVEL_ERROR,
    MATCH_LEVEL_NONE,
    MATCH_LEVEL_POSSIBLE,
    MATCH_LEVEL_STRONG,
    InboundImage,
)
from app.proofs.recognition import (
    PROPOSAL_KIND_EXACT,
    PROPOSAL_KIND_SIMILAR,
    RecognitionResult,
    persist_recognition,
)
from app.proofs.storage import save_inbound_image
from app.proofs.vision import (
    IMAGE_KIND_OTHER,
    IMAGE_KIND_PAYMENT_PROOF,
    IMAGE_KIND_PRODUCT_PHOTO,
    VERDICT_SAME,
    VERDICT_SIMILAR,
    ImageAnalysis,
    ImageAnalysisError,
)

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PHONE = "+221770051300"


def test_analyser_photo_client_schema_is_strict_and_parameterless() -> None:
    spec = next(item for item in TOOLS if item["name"] == "analyser_photo_client")
    assert spec["strict"] is True
    assert spec["parameters"]["properties"] == {}
    assert spec["parameters"]["required"] == []
    assert spec["parameters"]["additionalProperties"] is False


def test_analyser_photo_client_output_is_replayable() -> None:
    item = {
        "type": "function_call",
        "name": "analyser_photo_client",
        "call_id": "c1",
        "arguments": "{}",
    }
    output = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": json.dumps({"status": "analysed", "level": "strong"}),
    }
    assert _is_replayable_history_item(item) is True
    assert _is_replayable_history_item(output) is True


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(Notification).where(Notification.merchant_id.in_(merchant_ids))
        )
        await db.execute(
            delete(InboundImage).where(InboundImage.merchant_id.in_(merchant_ids))
        )
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


async def _seed(tmp_path: Path) -> tuple[Merchant, Product, Conversation]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-analyser-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Robe rouge test",
            description="test",
            category="tests",
            price=Decimal("25000.00"),
            stock_qty=8,
        )
        conversation = Conversation(
            merchant_id=merchant.id,
            customer_phone=PHONE,
            status="active",
        )
        db.add_all([product, conversation])
        await db.commit()
        await db.refresh(merchant)
        await db.refresh(product)
        await db.refresh(conversation)
    return merchant, product, conversation


def _add_photo(
    tmp_path: Path,
    merchant: Merchant,
    conversation: Conversation,
    *,
    classification: str = CLASSIFICATION_NOT_ANALYZED,
    wamid: str | None = None,
    created_at: datetime | None = None,
    purged: bool = False,
) -> InboundImage:
    with patch.object(settings, "media_dir", str(tmp_path / "media")):
        relative = None if purged else save_inbound_image(
            merchant.id, "image/png", TINY_PNG
        )
    image = InboundImage(
        merchant_id=merchant.id,
        conversation_id=conversation.id,
        whatsapp_message_id=wamid or f"wamid.{uuid.uuid4()}",
        media_path=relative,
        mime_type="image/png",
        classification=classification,
        created_at=created_at or datetime.now(timezone.utc),
        media_deleted_at=datetime.now(timezone.utc) if purged else None,
    )
    return image


def _match(product: Product, distance: float = 0.08) -> ImageMatch:
    return ImageMatch(product=product, distance=distance, in_stock=True)


def _result(
    product: Product,
    *,
    level: str = MATCH_LEVEL_STRONG,
    kind: str = PROPOSAL_KIND_EXACT,
) -> RecognitionResult:
    match = _match(product)
    proposed = [match] if level in {MATCH_LEVEL_STRONG, MATCH_LEVEL_POSSIBLE} else []
    return RecognitionResult(
        level=level,
        matches=[match] if level != MATCH_LEVEL_ERROR else [],
        proposed=proposed,
        proposal_kind=kind,
        voyage_seconds=0.11,
        verifier_seconds=0.22,
    )


async def _call(merchant_id: uuid.UUID, conversation_id: uuid.UUID) -> dict:
    async with AsyncSessionLocal() as db:
        raw = await execute_tool(
            db,
            "analyser_photo_client",
            {},
            merchant_id,
            conversation_id,
        )
        await db.commit()
    return json.loads(raw)


@pytest.mark.asyncio
async def test_analyser_no_photo_and_merchant_isolation(tmp_path: Path) -> None:
    merchant, _product, conversation = await _seed(tmp_path)
    other = Merchant(name=f"pytest-analyser-other-{uuid.uuid4()}")
    try:
        async with AsyncSessionLocal() as db:
            db.add(other)
            await db.flush()
            other_conv = Conversation(
                merchant_id=other.id,
                customer_phone="+221770051301",
                status="active",
            )
            db.add(other_conv)
            await db.flush()
            db.add(_add_photo(tmp_path, other, other_conv))
            await db.commit()
            other_conv_id = other_conv.id
        empty = await _call(merchant.id, conversation.id)
        assert empty == {"status": "no_photo"}
        stolen = await _call(merchant.id, other_conv_id)
        assert stolen == {"status": "no_photo"}
    finally:
        await _cleanup(merchant.id, other.id)


@pytest.mark.asyncio
async def test_analyser_picks_latest_ignores_old_and_purged(tmp_path: Path) -> None:
    merchant, product, conversation = await _seed(tmp_path)
    now = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                _add_photo(
                    tmp_path,
                    merchant,
                    conversation,
                    wamid="wamid.old",
                    created_at=now - timedelta(hours=25),
                )
            )
            db.add(
                _add_photo(
                    tmp_path,
                    merchant,
                    conversation,
                    purged=True,
                    wamid="wamid.purged",
                    created_at=now - timedelta(minutes=1),
                )
            )
            latest = _add_photo(
                tmp_path,
                merchant,
                conversation,
                wamid="wamid.latest",
                created_at=now,
            )
            db.add(latest)
            await db.commit()
        vision = AsyncMock(
            return_value=ImageAnalysis(image_kind=IMAGE_KIND_PRODUCT_PHOTO)
        )
        recognise = AsyncMock(return_value=_result(product))
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch("app.proofs.service.classer_image_entrante", vision),
            patch("app.proofs.service.recognise_product_photo", recognise),
        ):
            payload = await _call(merchant.id, conversation.id)
        assert payload["status"] == "analysed"
        assert payload["level"] == MATCH_LEVEL_STRONG
        assert payload["proposal_kind"] == PROPOSAL_KIND_EXACT
        assert payload["candidates"][0]["product_id"] == str(product.id)
        assert payload["candidates"][0]["stock_status"] == "disponible"
        assert "distance" not in json.dumps(payload)
        assert "http" not in json.dumps(payload).lower()
        assert "/static/" not in json.dumps(payload)
        assert "stock_qty" not in json.dumps(payload)
        assert "8" not in payload["candidates"][0]["stock_status"]
        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(InboundImage).where(
                            InboundImage.conversation_id == conversation.id
                        )
                    )
                ).scalars().all()
            )
            assert len(rows) == 3
            stored = next(row for row in rows if row.whatsapp_message_id == "wamid.latest")
            assert stored.classification == CLASSIFICATION_PRODUCT_PHOTO
            assert stored.match_level == MATCH_LEVEL_STRONG
            notes = list(
                (
                    await db.execute(
                        select(Notification).where(
                            Notification.merchant_id == merchant.id
                        )
                    )
                ).scalars().all()
            )
            assert notes == []
            messages = (
                await db.execute(
                    select(func.count()).select_from(Message).where(
                        Message.conversation_id == conversation.id
                    )
                )
            ).scalar_one()
            assert messages == 0
        items = [
            {
                "type": "function_call_output",
                "output": json.dumps(payload),
            }
        ]
        assert extract_product_images(items) == []
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_analyser_idempotent_and_already_analysed_live(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    merchant, product, conversation = await _seed(tmp_path)
    try:
        async with AsyncSessionLocal() as db:
            db.add(_add_photo(tmp_path, merchant, conversation, wamid="wamid.once"))
            await db.commit()
        vision = AsyncMock(
            return_value=ImageAnalysis(image_kind=IMAGE_KIND_PRODUCT_PHOTO)
        )
        recognise = AsyncMock(return_value=_result(product))
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch("app.proofs.service.classer_image_entrante", vision),
            patch("app.proofs.service.recognise_product_photo", recognise),
            caplog.at_level(logging.INFO, logger="app.proofs.service"),
        ):
            first = await _call(merchant.id, conversation.id)
            second = await _call(merchant.id, conversation.id)
        assert first["status"] == "analysed"
        assert second["status"] == "already_analysed"
        assert second["level"] == MATCH_LEVEL_STRONG
        assert vision.await_count == 1
        assert recognise.await_count == 1
        assert "Photo analysis source=analyser_photo_client" in caplog.text
        assert "level=strong" in caplog.text
        assert "classifier_s=" in caplog.text
        assert TINY_PNG.hex() not in caplog.text
        assert "/static/" not in caplog.text
        assert str(tmp_path) not in caplog.text
        assert "iVBORw0" not in caplog.text

        async with AsyncSessionLocal() as db:
            live = _add_photo(
                tmp_path, merchant, conversation, wamid="wamid.live"
            )
            live.classification = CLASSIFICATION_PRODUCT_PHOTO
            persist_recognition(live, _result(product))
            db.add(live)
            await db.commit()
        vision.reset_mock()
        recognise.reset_mock()
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch("app.proofs.service.classer_image_entrante", vision),
            patch("app.proofs.service.recognise_product_photo", recognise),
        ):
            live_payload = await _call(merchant.id, conversation.id)
        assert live_payload["status"] == "already_analysed"
        vision.assert_not_awaited()
        recognise.assert_not_awaited()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_analyser_outcomes_similar_none_error_payment_failure_cap(
    tmp_path: Path,
) -> None:
    merchant, product, conversation = await _seed(tmp_path)
    try:
        async with AsyncSessionLocal() as db:
            db.add(_add_photo(tmp_path, merchant, conversation, wamid="wamid.sim"))
            await db.commit()
        similar = _result(
            product, level=MATCH_LEVEL_POSSIBLE, kind=PROPOSAL_KIND_SIMILAR
        )
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.classer_image_entrante",
                AsyncMock(return_value=ImageAnalysis(image_kind=IMAGE_KIND_PRODUCT_PHOTO)),
            ),
            patch(
                "app.proofs.service.recognise_product_photo",
                AsyncMock(return_value=similar),
            ),
        ):
            payload = await _call(merchant.id, conversation.id)
        assert payload["status"] == "analysed"
        assert payload["level"] == MATCH_LEVEL_POSSIBLE
        assert payload["proposal_kind"] == PROPOSAL_KIND_SIMILAR

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(InboundImage).where(InboundImage.merchant_id == merchant.id)
            )
            db.add(_add_photo(tmp_path, merchant, conversation, wamid="wamid.none"))
            await db.commit()
        none = RecognitionResult(
            level=MATCH_LEVEL_NONE, matches=[], proposed=[]
        )
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.classer_image_entrante",
                AsyncMock(return_value=ImageAnalysis(image_kind=IMAGE_KIND_PRODUCT_PHOTO)),
            ),
            patch(
                "app.proofs.service.recognise_product_photo",
                AsyncMock(return_value=none),
            ),
        ):
            payload = await _call(merchant.id, conversation.id)
        assert payload["status"] == "analysed"
        assert payload["level"] == MATCH_LEVEL_NONE
        assert payload["candidates"] == []

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(InboundImage).where(InboundImage.merchant_id == merchant.id)
            )
            db.add(_add_photo(tmp_path, merchant, conversation, wamid="wamid.err"))
            await db.commit()
        err = RecognitionResult(
            level=MATCH_LEVEL_ERROR, matches=[], proposed=[]
        )
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.classer_image_entrante",
                AsyncMock(return_value=ImageAnalysis(image_kind=IMAGE_KIND_PRODUCT_PHOTO)),
            ),
            patch(
                "app.proofs.service.recognise_product_photo",
                AsyncMock(return_value=err),
            ),
        ):
            payload = await _call(merchant.id, conversation.id)
        assert payload["status"] == "analysed"
        assert payload["level"] == MATCH_LEVEL_ERROR

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(InboundImage).where(InboundImage.merchant_id == merchant.id)
            )
            db.add(_add_photo(tmp_path, merchant, conversation, wamid="wamid.pay"))
            await db.commit()
            before_notes = (
                await db.execute(
                    select(func.count()).select_from(Notification).where(
                        Notification.merchant_id == merchant.id
                    )
                )
            ).scalar_one()
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.classer_image_entrante",
                AsyncMock(
                    return_value=ImageAnalysis(image_kind=IMAGE_KIND_PAYMENT_PROOF)
                ),
            ),
            patch("app.proofs.service.recognise_product_photo", AsyncMock()) as rec,
        ):
            payload = await _call(merchant.id, conversation.id)
        assert payload == {"status": "not_a_product_photo"}
        rec.assert_not_awaited()
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(InboundImage).where(
                        InboundImage.whatsapp_message_id == "wamid.pay"
                    )
                )
            ).scalar_one()
            assert row.classification == CLASSIFICATION_PAYMENT_PROOF
            notes = (
                await db.execute(
                    select(Notification).where(Notification.merchant_id == merchant.id)
                )
            ).scalars().all()
            assert len(notes) == before_notes
            assert all(
                note.type != NotificationType.product_photo_unrecognized.value
                for note in notes
            )
            assert all(
                note.type != NotificationType.payment_proof_received.value
                for note in notes
            )

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(InboundImage).where(InboundImage.merchant_id == merchant.id)
            )
            db.add(_add_photo(tmp_path, merchant, conversation, wamid="wamid.fail"))
            await db.commit()
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.classer_image_entrante",
                AsyncMock(side_effect=ImageAnalysisError("down")),
            ),
        ):
            payload = await _call(merchant.id, conversation.id)
        assert payload == {"status": "unavailable"}
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(InboundImage).where(
                        InboundImage.whatsapp_message_id == "wamid.fail"
                    )
                )
            ).scalar_one()
            assert row.classification == CLASSIFICATION_NOT_ANALYZED

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(InboundImage).where(InboundImage.merchant_id == merchant.id)
            )
            db.add(_add_photo(tmp_path, merchant, conversation, wamid="wamid.cap"))
            await db.commit()
        with patch.object(settings, "max_image_analyses_per_phone_per_day", 0):
            payload = await _call(merchant.id, conversation.id)
        assert payload == {"status": "cap_reached"}
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(InboundImage).where(
                        InboundImage.whatsapp_message_id == "wamid.cap"
                    )
                )
            ).scalar_one()
            assert row.classification == CLASSIFICATION_NOT_ANALYZED
            assert row.match_level is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_analyser_other_kind_is_not_a_product_photo(tmp_path: Path) -> None:
    merchant, _product, conversation = await _seed(tmp_path)
    try:
        async with AsyncSessionLocal() as db:
            db.add(_add_photo(tmp_path, merchant, conversation))
            await db.commit()
        with (
            patch.object(settings, "media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.classer_image_entrante",
                AsyncMock(return_value=ImageAnalysis(image_kind=IMAGE_KIND_OTHER)),
            ),
            patch("app.proofs.service.recognise_product_photo", AsyncMock()) as rec,
        ):
            payload = await _call(merchant.id, conversation.id)
        assert payload == {"status": "not_a_product_photo"}
        rec.assert_not_awaited()
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(InboundImage).where(
                        InboundImage.conversation_id == conversation.id
                    )
                )
            ).scalar_one()
            assert row.classification == CLASSIFICATION_OTHER
    finally:
        await _cleanup(merchant.id)
