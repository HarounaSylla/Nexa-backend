"""Product-photo vision, recognition, agent turn, and WhatsApp worker.

Vision, Voyage, and the LLM are always stubbed — no network.
"""

from __future__ import annotations

import inspect
import json
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, select

from app.agent.handover import item_customer_photo, _is_native_agent_item
from app.agent.images import extract_product_images
from app.agent.models import Conversation, Message, SentProductImage
from app.agent.orchestrator import (
    _is_replayable_history_item,
    prior_items_for_agent,
    serialize_item,
    traiter_message_entrant,
)
from app.agent.service import (
    STATUS_ESCALATED,
    TURN_ROLE_AGENT,
    obtenir_dernier_message_agent,
)
from app.catalogue.image_embeddings import ImageEmbeddingError
from app.catalogue.models import Merchant, Product
from app.catalogue.service import ImageMatch
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.formatting import format_fcfa
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
    CLASSIFICATION_PRODUCT_PHOTO,
    CLASSIFICATION_UNKNOWN,
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
    build_product_photo_developer_item,
    persist_recognition,
    proposal_set,
    recognise_product_photo,
)
from app.proofs.service import traiter_image_entrante
from app.proofs.vision import (
    IMAGE_KIND_OTHER,
    IMAGE_KIND_PAYMENT_PROOF,
    IMAGE_KIND_PRODUCT_PHOTO,
    VERDICT_NONE,
    VERDICT_SAME,
    VERDICT_SIMILAR,
    ImageAnalysis,
    ImageAnalysisError,
    ImageVerification,
    ImageVerificationError,
    _parse_analysis,
)
from app.whatsapp.service import FALLBACK_REPLY
from app.workers.whatsapp import (
    envoyer_reponse_whatsapp_agent,
    process_inbound_whatsapp_image_async,
    process_inbound_whatsapp_text_async,
)

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)
PHONE = "+221770051200"


class _CapturingGraph:
    def __init__(
        self,
        reply: str = "C'est bien celui-ci ?",
        extra_items: list | None = None,
    ) -> None:
        self.input_lists: list[list] = []
        self.reply = reply
        self.extra_items = extra_items or []

    async def ainvoke(self, initial, config=None):
        self.input_lists.append(list(initial["input_list"]))
        new_items = [
            *self.extra_items,
            {
                "type": "message",
                "role": "assistant",
                "content": self.reply,
            },
        ]
        return {
            **initial,
            "new_items": new_items,
            "output_text": self.reply,
        }


def _product_photo_analysis(description: str | None = "A red evening dress") -> ImageAnalysis:
    return ImageAnalysis(
        image_kind=IMAGE_KIND_PRODUCT_PHOTO,
        product_description=description,
    )


def _match(
    *,
    name: str,
    distance: float,
    price: str = "25000",
    stock_qty: int = 10,
    product_id: uuid.UUID | None = None,
    merchant_id: uuid.UUID | None = None,
) -> ImageMatch:
    product = Product(
        id=product_id or uuid.uuid4(),
        merchant_id=merchant_id or uuid.uuid4(),
        name=name,
        price=Decimal(price),
        stock_qty=stock_qty,
    )
    return ImageMatch(
        product=product,
        distance=distance,
        in_stock=stock_qty > 0,
    )


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
        await db.execute(delete(Product).where(Product.merchant_id.in_(merchant_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_shop(
    tmp_path: Path,
    *,
    second_name: str = "Robe longue noire de soirée",
) -> tuple[Merchant, Product, Product]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=f"pytest-photo-{uuid.uuid4()}",
            whatsapp_phone_number_id=f"pnid-photo-{uuid.uuid4()}",
        )
        db.add(merchant)
        await db.flush()
        first = Product(
            merchant_id=merchant.id,
            name="Robe longue rouge de soirée",
            description="tests photo",
            category="vêtements femme",
            price=Decimal("25000.00"),
            stock_qty=8,
        )
        second = Product(
            merchant_id=merchant.id,
            name=second_name,
            description="tests photo 2",
            category="vêtements femme",
            price=Decimal("25000.00"),
            stock_qty=3,
        )
        db.add_all([first, second])
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
        await db.refresh(first)
        await db.refresh(second)
    return merchant, first, second


def _search_stub(matches: list[ImageMatch]) -> AsyncMock:
    return AsyncMock(return_value=matches)


def _fake_photos(matches: list[ImageMatch]) -> list:
    return [(match, TINY_PNG) for match in matches]


def _verification(
    verdict: str = VERDICT_SAME,
    candidate: int | None = 1,
    model: str = "test-verifier",
) -> AsyncMock:
    return AsyncMock(
        return_value=ImageVerification(
            verdict=verdict,
            candidate=candidate,
            reason="stub",
            model=model,
        )
    )


@contextmanager
def _image_patches(
    tmp_path: Path,
    *,
    vision,
    search=None,
    graph=None,
    agent=None,
    send=None,
    verify=None,
):
    patches = [
        patch("app.core.config.settings.media_dir", str(tmp_path / "media")),
        patch(
            "app.proofs.service.telecharger_media_whatsapp",
            new=AsyncMock(return_value=(TINY_PNG, "image/png")),
        ),
        patch("app.proofs.service.classer_image_entrante", vision),
        patch(
            "app.proofs.service.envoyer_message_commercant",
            send or AsyncMock(),
        ),
    ]
    if search is not None:
        patches.append(
            patch("app.proofs.recognition.trouver_produits_par_image", search)
        )
        patches.append(
            patch("app.proofs.recognition.load_candidate_photos", _fake_photos)
        )
        patches.append(
            patch(
                "app.proofs.recognition.verify_image_against_candidates",
                verify if verify is not None else _verification(),
            )
        )
    if graph is not None:
        patches.append(
            patch("app.agent.orchestrator._build_graph", return_value=graph)
        )
    if agent is not None:
        patches.append(patch("app.proofs.service.traiter_photo_produit", agent))
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        yield


def test_vision_three_kinds_parse_and_is_payment_proof_property() -> None:
    proof = _parse_analysis(
        {
            "image_kind": "payment_proof",
            "detected_amount": 4000,
            "product_description": "should be dropped",
        }
    )
    assert proof.image_kind == IMAGE_KIND_PAYMENT_PROOF
    assert proof.is_payment_proof is True
    assert proof.detected_amount == Decimal("4000")
    assert proof.product_description is None

    product = _parse_analysis(
        {
            "image_kind": "product_photo",
            "detected_amount": 12,
            "product_description": "  A red evening dress with ruffles.  ",
        }
    )
    assert product.image_kind == IMAGE_KIND_PRODUCT_PHOTO
    assert product.is_payment_proof is False
    assert product.detected_amount is None
    assert product.product_description == "A red evening dress with ruffles."

    other = _parse_analysis(
        {
            "image_kind": "other",
            "detected_amount": None,
            "product_description": "a selfie",
        }
    )
    assert other.image_kind == IMAGE_KIND_OTHER
    assert other.is_payment_proof is False
    assert other.detected_amount is None
    assert other.product_description is None

    from_json = _parse_analysis(
        '{"image_kind":"product_photo","detected_amount":null,'
        '"product_description":"Black handbag"}'
    )
    assert from_json.image_kind == IMAGE_KIND_PRODUCT_PHOTO
    assert from_json.product_description == "Black handbag"

    legacy_pos = ImageAnalysis(True, Decimal("4000"))
    assert legacy_pos.is_payment_proof is True
    assert legacy_pos.image_kind == IMAGE_KIND_PAYMENT_PROOF
    assert legacy_pos.detected_amount == Decimal("4000")

    legacy_false = ImageAnalysis(False, None)
    assert legacy_false.is_payment_proof is False
    assert legacy_false.image_kind == IMAGE_KIND_OTHER

    legacy_kw = ImageAnalysis(is_payment_proof=True, detected_amount=Decimal("1"))
    assert legacy_kw.is_payment_proof is True


def test_vision_malformed_output_raises() -> None:
    with pytest.raises(ImageAnalysisError):
        _parse_analysis("not json")
    with pytest.raises(ImageAnalysisError):
        _parse_analysis(["payment_proof"])
    with pytest.raises(ImageAnalysisError):
        _parse_analysis(
            {
                "image_kind": "selfie",
                "detected_amount": None,
                "product_description": None,
            }
        )
    with pytest.raises(ImageAnalysisError):
        _parse_analysis(
            {
                "image_kind": "payment_proof",
                "detected_amount": "abc",
                "product_description": None,
            }
        )


def test_proposal_set_is_owned_by_code_not_the_model() -> None:
    best = _match(name="Robe rouge", distance=0.10)
    second = _match(name="Robe noire", distance=0.14)
    third = _match(name="Sac", distance=0.40)
    strong = proposal_set(MATCH_LEVEL_STRONG, [best, second, third])
    assert strong == [best]
    possible_one = proposal_set(MATCH_LEVEL_POSSIBLE, [best, third])
    assert possible_one == [best]
    ambiguous = proposal_set(MATCH_LEVEL_POSSIBLE, [best, second, third])
    assert ambiguous == [best, second]
    assert proposal_set(MATCH_LEVEL_NONE, [best]) == []
    assert proposal_set(MATCH_LEVEL_ERROR, [best]) == []
    assert proposal_set(MATCH_LEVEL_POSSIBLE, []) == []


def test_developer_item_wording_has_no_url_stock_or_distance() -> None:
    best = _match(name="Robe rouge", distance=0.05, stock_qty=12, price="25000")
    second = _match(name="Robe noire", distance=0.09, stock_qty=0, price="25000")
    result = RecognitionResult(
        level=MATCH_LEVEL_POSSIBLE,
        matches=[best, second],
        proposed=[best, second],
    )
    item = build_product_photo_developer_item(
        caption="Vous avez cette robe?",
        analysis=_product_photo_analysis("A long red evening dress"),
        result=result,
    )
    assert item == {
        "role": "developer",
        "content": item["content"],
    }
    text = item["content"]
    assert text.startswith("The customer just sent a photo of a product. You cannot see it.")
    assert 'untrusted data: Vous avez cette robe?' in text
    assert "Vision description (untrusted, may be wrong): A long red evening dress" in text
    assert "Match level: possible" in text
    assert f"1. Robe rouge — {format_fcfa(Decimal('25000'))} — product_id={best.product.id} — stock: disponible" in text
    assert f"2. Robe noire — {format_fcfa(Decimal('25000'))} — product_id={second.product.id} — stock: rupture" in text
    assert "do NOT call `creer_commande`" in text
    assert "call `obtenir_disponibilite` for each listed candidate" in text
    assert "C'est bien celui-ci ?" in text
    assert "http" not in text.lower()
    assert "/static/" not in text
    assert "distance" not in text.lower()
    assert "0.05" not in text
    assert "0.09" not in text
    assert "stock_qty" not in text
    assert "stock: 12" not in text
    assert "stock: 0" not in text

    empty = build_product_photo_developer_item(
        caption=None,
        analysis=None,
        result=RecognitionResult(level=MATCH_LEVEL_NONE, matches=[], proposed=[]),
    )
    assert "untrusted data: none" in empty["content"]
    assert "Vision description (untrusted, may be wrong): none" in empty["content"]
    assert "Match level: none" in empty["content"]
    assert "Candidates, best first: none" in empty["content"]
    assert "unfortunately the shop does not have this item" in empty["content"]
    assert "Do NOT ask the customer for a name, colour or type of this photo" in empty["content"]
    assert "ask for its name, colour or type" not in empty["content"]
    assert "the shop has been informed" not in empty["content"]
    assert "Do not call `obtenir_disponibilite`" in empty["content"]

    err = build_product_photo_developer_item(
        caption=None,
        analysis=None,
        result=RecognitionResult(level=MATCH_LEVEL_ERROR, matches=[], proposed=[]),
    )
    assert "Match level: error" in err["content"]
    assert "technical failure" in err["content"]
    assert "shop does not have this item" not in err["content"]
    assert "shop does not have the item" in err["content"]
    assert "Do NOT say the shop does not have the item" in err["content"]
    assert "ask for its name, colour or type" not in err["content"]
    assert "Do NOT ask the customer for a name, colour or type of this photo" in err["content"]


def test_replay_helpers_accept_leading_developer_item() -> None:
    developer = {
        "role": "developer",
        "content": "The customer just sent a photo of a product.",
    }
    tool_out = {
        "type": "function_call_output",
        "output": json.dumps(
            {
                "products": [
                    {
                        "id": str(uuid.uuid4()),
                        "image_url": "/static/product_images/x.png",
                    }
                ]
            }
        ),
    }
    assert _is_replayable_history_item(developer) is True
    assert _is_native_agent_item(developer) is False
    sanitized = serialize_item(developer)
    assert sanitized == developer
    images = extract_product_images([developer, tool_out])
    assert len(images) == 1
    assert str(images[0].image_url).endswith(".png")


def test_shared_send_helper_is_used_by_text_and_image_jobs() -> None:
    from app.workers.whatsapp import flush_conversation_async

    flush_src = inspect.getsource(flush_conversation_async)
    assert "envoyer_reponse_whatsapp_agent" in flush_src
    text_src = inspect.getsource(process_inbound_whatsapp_text_async)
    image_src = inspect.getsource(process_inbound_whatsapp_image_async)
    assert "register_pending" in text_src
    assert "register_pending" in image_src
    helper_src = inspect.getsource(envoyer_reponse_whatsapp_agent)
    assert "photo_delivery_plan" in helper_src
    assert "MAX_WHATSAPP_IMAGES" in helper_src
    assert "PHOTO_STATUS_ALREADY_SENT" in helper_src
    assert "PHOTO_STATUS_WILL_BE_SENT" in helper_src


@pytest.mark.asyncio
async def test_escalated_without_awaiting_order_skips_vision_and_voyage(
    tmp_path: Path,
) -> None:
    merchant, _first, _second = await _seed_shop(tmp_path)
    vision = AsyncMock(return_value=_product_photo_analysis())
    search = _search_stub([])
    try:
        async with AsyncSessionLocal() as db:
            db.add(
                Conversation(
                    merchant_id=merchant.id,
                    customer_phone=PHONE,
                    status=STATUS_ESCALATED,
                )
            )
            await db.commit()
        with _image_patches(tmp_path, vision=vision, search=search, agent=AsyncMock()):
            async with AsyncSessionLocal() as db:
                stored = await traiter_image_entrante(
                    db, merchant, PHONE, "wamid.esc-skip", "m", "image/png", None
                )
        assert stored is not None
        assert stored.classification == CLASSIFICATION_NOT_ANALYZED
        assert stored.match_level is None
        assert stored.matched_product_id is None
        vision.assert_not_awaited()
        search.assert_not_awaited()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_escalated_awaiting_proof_product_photo_skips_recognition(
    tmp_path: Path,
) -> None:
    merchant, first, _second = await _seed_shop(tmp_path)
    vision = AsyncMock(return_value=_product_photo_analysis())
    search = _search_stub([])
    agent = AsyncMock()
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone=PHONE,
                status=STATUS_ESCALATED,
            )
            db.add(conversation)
            await db.commit()
            await db.refresh(conversation)
        async with AsyncSessionLocal() as db:
            order = await creer_commande(
                db,
                merchant_id=merchant.id,
                customer_phone=PHONE,
                items=[(first.id, 1)],
                payment_method=PaymentMethod.online,
                delivery_address="Sacré-Cœur, Dakar",
                ville="Dakar",
            )
        async with AsyncSessionLocal() as db:
            row = await db.get(Order, order.id)
            assert row is not None
            row.payment_link = "https://pay.example.com/x"
            row.payment_link_sent_at = datetime.now(timezone.utc)
            row.conversation_id = conversation.id
            await db.commit()
        with _image_patches(tmp_path, vision=vision, search=search, agent=agent):
            async with AsyncSessionLocal() as db:
                stored = await traiter_image_entrante(
                    db, merchant, PHONE, "wamid.esc-prod", "m", "image/png", None
                )
        assert stored is not None
        assert stored.classification == CLASSIFICATION_PRODUCT_PHOTO
        assert stored.match_level is None
        assert stored.matched_product_id is None
        vision.assert_awaited()
        search.assert_not_awaited()
        agent.assert_not_awaited()
        async with AsyncSessionLocal() as db:
            notes = list(
                (
                    await db.execute(
                        select(Notification).where(
                            Notification.merchant_id == merchant.id,
                            Notification.type == "product_photo_unrecognized",
                        )
                    )
                ).scalars().all()
            )
            assert notes == []
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_daily_cap_uses_setting_and_skips_vision(tmp_path: Path) -> None:
    merchant, _first, _second = await _seed_shop(tmp_path)
    vision = AsyncMock(return_value=_product_photo_analysis())
    search = _search_stub([])
    try:
        with patch.object(settings, "max_image_analyses_per_phone_per_day", 0):
            with _image_patches(tmp_path, vision=vision, search=search, agent=AsyncMock()):
                async with AsyncSessionLocal() as db:
                    stored = await traiter_image_entrante(
                        db, merchant, PHONE, "wamid.cap-setting", "m", "image/png", None
                    )
        assert stored is not None
        assert stored.classification == CLASSIFICATION_NOT_ANALYZED
        vision.assert_not_awaited()
        search.assert_not_awaited()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_other_stores_no_reply_no_notification(tmp_path: Path) -> None:
    merchant, _first, _second = await _seed_shop(tmp_path)
    vision = AsyncMock(return_value=ImageAnalysis(False, None))
    send = AsyncMock()
    search = _search_stub([])
    agent = AsyncMock()
    try:
        with _image_patches(tmp_path, vision=vision, search=search, agent=agent, send=send):
            async with AsyncSessionLocal() as db:
                stored = await traiter_image_entrante(
                    db, merchant, PHONE, "wamid.other-photo", "m", "image/png", None
                )
        assert stored is not None
        assert stored.classification == CLASSIFICATION_OTHER
        send.assert_not_awaited()
        search.assert_not_awaited()
        agent.assert_not_awaited()
        assert getattr(stored, "_send_agent", False) is False
        async with AsyncSessionLocal() as db:
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
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_vision_error_is_unknown_without_recognition(tmp_path: Path) -> None:
    merchant, _first, _second = await _seed_shop(tmp_path)
    vision = AsyncMock(side_effect=ImageAnalysisError("timeout"))
    search = _search_stub([])
    agent = AsyncMock()
    try:
        with _image_patches(tmp_path, vision=vision, search=search, agent=agent):
            async with AsyncSessionLocal() as db:
                stored = await traiter_image_entrante(
                    db, merchant, PHONE, "wamid.vision-err", "m", "image/png", None
                )
        assert stored is not None
        assert stored.classification == CLASSIFICATION_UNKNOWN
        assert stored.match_level is None
        search.assert_not_awaited()
        agent.assert_not_awaited()
    finally:
        await _cleanup(merchant.id)


async def _run_recognised(
    tmp_path: Path,
    merchant: Merchant,
    matches: list[ImageMatch] | Exception,
    *,
    wamid: str,
    caption: str | None = None,
    graph: _CapturingGraph | None = None,
    verify=None,
) -> tuple[InboundImage, _CapturingGraph]:
    vision = AsyncMock(return_value=_product_photo_analysis("A long red dress"))
    if isinstance(matches, Exception):
        search = AsyncMock(side_effect=matches)
    else:
        search = _search_stub(matches)
    capturing = graph or _CapturingGraph()
    with _image_patches(
        tmp_path,
        vision=vision,
        search=search,
        graph=capturing,
        verify=verify,
    ):
        async with AsyncSessionLocal() as db:
            stored = await traiter_image_entrante(
                db, merchant, PHONE, wamid, "m", "image/png", caption
            )
    assert stored is not None
    return stored, capturing


@pytest.mark.asyncio
async def test_live_photo_analysis_emits_timing_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    merchant, first, _second = await _seed_shop(tmp_path)
    try:
        matches = [ImageMatch(product=first, distance=0.10, in_stock=True)]
        with caplog.at_level("INFO", logger="app.proofs.service"):
            stored, _ = await _run_recognised(
                tmp_path, merchant, matches, wamid="wamid.timing"
            )
        assert stored.match_level == MATCH_LEVEL_STRONG
        assert "Photo analysis source=live" in caplog.text
        assert "classifier_s=" in caplog.text
        assert "voyage_s=" in caplog.text
        assert "total_s=" in caplog.text
        assert "level=strong" in caplog.text
        assert TINY_PNG.hex() not in caplog.text
        assert "/static/" not in caplog.text
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_recognition_persistence_strong_possible_none_error(
    tmp_path: Path,
) -> None:
    merchant, first, second = await _seed_shop(tmp_path)
    try:
        strong_matches = [
            ImageMatch(product=first, distance=0.10, in_stock=True),
            ImageMatch(product=second, distance=0.40, in_stock=True),
        ]
        stored, _graph = await _run_recognised(
            tmp_path, merchant, strong_matches, wamid="wamid.strong"
        )
        assert stored.classification == CLASSIFICATION_PRODUCT_PHOTO
        assert stored.match_level == MATCH_LEVEL_STRONG
        assert stored.matched_product_id == first.id
        assert stored.match_candidates == [
            {
                "product_id": str(first.id),
                "distance": 0.10,
                "verdict": "same",
                "verifier": "test-verifier",
            },
            {"product_id": str(second.id), "distance": 0.40, "verdict": "none"},
        ]

        possible_matches = [
            ImageMatch(product=first, distance=0.25, in_stock=True),
            ImageMatch(product=second, distance=0.40, in_stock=True),
        ]
        possible, _ = await _run_recognised(
            tmp_path,
            merchant,
            possible_matches,
            wamid="wamid.possible",
            verify=_verification(VERDICT_SIMILAR, 1),
        )
        assert possible.match_level == MATCH_LEVEL_POSSIBLE
        assert possible.matched_product_id == first.id
        assert possible.match_candidates[0]["verdict"] == "similar"

        none_matches = [
            ImageMatch(product=first, distance=0.70, in_stock=True),
        ]
        none_row, none_graph = await _run_recognised(
            tmp_path, merchant, none_matches, wamid="wamid.none"
        )
        assert none_row.match_level == MATCH_LEVEL_NONE
        assert none_row.matched_product_id is None
        assert "Candidates, best first: none" in none_graph.input_lists[0][-1]["content"]

        error_row, error_graph = await _run_recognised(
            tmp_path,
            merchant,
            ImageEmbeddingError("voyage down"),
            wamid="wamid.voyage-err",
        )
        assert error_row.match_level == MATCH_LEVEL_ERROR
        assert error_row.matched_product_id is None
        assert error_row.match_candidates == []
        assert error_graph.input_lists[0][-1]["content"].startswith(
            "The customer just sent a photo of a product."
        )
        assert error_row.classification == CLASSIFICATION_PRODUCT_PHOTO
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_unrecognized_notification_only_for_none_and_error(
    tmp_path: Path,
) -> None:
    merchant, first, second = await _seed_shop(tmp_path)
    try:
        strong_matches = [
            ImageMatch(product=first, distance=0.10, in_stock=True),
            ImageMatch(product=second, distance=0.40, in_stock=True),
        ]
        await _run_recognised(tmp_path, merchant, strong_matches, wamid="wamid.n-strong")
        possible_matches = [
            ImageMatch(product=first, distance=0.25, in_stock=True),
            ImageMatch(product=second, distance=0.40, in_stock=True),
        ]
        await _run_recognised(
            tmp_path,
            merchant,
            possible_matches,
            wamid="wamid.n-possible",
            verify=_verification(VERDICT_SIMILAR, 1),
        )
        none_matches = [ImageMatch(product=first, distance=0.80, in_stock=True)]
        await _run_recognised(tmp_path, merchant, none_matches, wamid="wamid.n-none")
        await _run_recognised(
            tmp_path,
            merchant,
            ImageEmbeddingError("boom"),
            wamid="wamid.n-error",
        )
        async with AsyncSessionLocal() as db:
            notes = list(
                (
                    await db.execute(
                        select(Notification).where(
                            Notification.merchant_id == merchant.id,
                            Notification.type == "product_photo_unrecognized",
                        )
                    )
                ).scalars().all()
            )
        assert len(notes) == 2
        for note in notes:
            assert note.data["title"] == "Photo de produit non reconnue"
            assert PHONE in note.data["body"]
            assert "vente" in note.data["body"]
            assert note.related_type == "conversation"
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_developer_item_stored_first_and_replayed_on_oui(
    tmp_path: Path,
) -> None:
    merchant, first, second = await _seed_shop(tmp_path)
    try:
        matches = [
            ImageMatch(product=first, distance=0.10, in_stock=True),
            ImageMatch(product=second, distance=0.40, in_stock=True),
        ]
        graph = _CapturingGraph(reply="Je pense que c'est la robe rouge. C'est bien celui-ci ?")
        stored, _ = await _run_recognised(
            tmp_path,
            merchant,
            matches,
            wamid="wamid.replay",
            caption="Vous avez cette robe?",
            graph=graph,
        )
        photo_marker = item_customer_photo(
            classification="product_photo",
            caption="Vous avez cette robe?",
        )
        assert stored.caption == "Vous avez cette robe?"
        async with AsyncSessionLocal() as db:
            last = await obtenir_dernier_message_agent(db, merchant.id, PHONE)
            assert last is not None
            assert last.turn_role == TURN_ROLE_AGENT
            assert last.items[0]["role"] == "developer"
            developer = last.items[0]
            assert developer["content"].startswith(
                "The customer just sent a photo of a product."
            )
            assert "untrusted data: Vous avez cette robe?" in developer["content"]
            assert str(first.id) in developer["content"]
            assert str(second.id) not in developer["content"]
            assert last.items[-1]["role"] == "assistant"
            messages = list(
                (
                    await db.execute(
                        select(Message)
                        .where(Message.conversation_id == last.conversation_id)
                        .order_by(Message.created_at, Message.id)
                    )
                ).scalars().all()
            )
            replayed = prior_items_for_agent(messages)
            assert photo_marker in replayed
            assert developer in replayed
            assert replayed.index(photo_marker) < replayed.index(developer)

        follow = _CapturingGraph(reply="Quelle quantité souhaitez-vous ?")
        with patch("app.agent.orchestrator._build_graph", return_value=follow):
            async with AsyncSessionLocal() as db:
                await traiter_message_entrant(db, merchant.id, PHONE, "oui")
        input_list = follow.input_lists[0]
        assert input_list[0]["role"] == "developer"
        assert "You are the WhatsApp sales assistant" in input_list[0]["content"]
        assert input_list[-1] == {"role": "user", "content": "oui"}
        photo_index = input_list.index(photo_marker)
        dev_index = input_list.index(developer)
        assistant_index = next(
            index
            for index, item in enumerate(input_list)
            if item.get("role") == "assistant"
            and "C'est bien celui-ci" in str(item.get("content"))
        )
        oui_index = len(input_list) - 1
        assert photo_index < dev_index < assistant_index < oui_index
    finally:
        await _cleanup(merchant.id)


def test_persist_recognition_matched_product_id_rules() -> None:
    image = InboundImage(
        merchant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        whatsapp_message_id="x",
        mime_type="image/png",
        classification=CLASSIFICATION_PRODUCT_PHOTO,
        media_path="x.png",
    )
    first = _match(name="A", distance=0.1)
    persist_recognition(
        image,
        RecognitionResult(
            level=MATCH_LEVEL_STRONG, matches=[first], proposed=[first]
        ),
    )
    assert image.matched_product_id == first.product.id
    persist_recognition(
        image,
        RecognitionResult(level=MATCH_LEVEL_NONE, matches=[first], proposed=[]),
    )
    assert image.matched_product_id is None
    persist_recognition(
        image,
        RecognitionResult(level=MATCH_LEVEL_ERROR, matches=[], proposed=[]),
    )
    assert image.matched_product_id is None
    assert image.match_candidates == []


@pytest.mark.asyncio
async def test_worker_sends_photos_via_shared_helper_and_dedupes(
    tmp_path: Path,
) -> None:
    merchant, first, _second = await _seed_shop(tmp_path)
    product_id = first.id
    extra = [
        {
            "type": "function_call_output",
            "output": json.dumps(
                {
                    "products": [
                        {
                            "id": str(product_id),
                            "name": first.name,
                            "price": "25000",
                            "image_url": f"/static/product_images/{product_id}.png",
                        }
                    ]
                }
            ),
        }
    ]
    graph = _CapturingGraph(reply="Je pense que c'est cette robe. C'est bien celui-ci ?", extra_items=extra)
    vision = AsyncMock(return_value=_product_photo_analysis())
    search = _search_stub(
        [ImageMatch(product=first, distance=0.10, in_stock=True)]
    )
    send_text = AsyncMock()
    send_image = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.core.config.settings.media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.telecharger_media_whatsapp",
                new=AsyncMock(return_value=(TINY_PNG, "image/png")),
            ),
            patch("app.proofs.service.classer_image_entrante", vision),
            patch("app.proofs.recognition.trouver_produits_par_image", search),
            patch("app.proofs.recognition.load_candidate_photos", _fake_photos),
            patch(
                "app.proofs.recognition.verify_image_against_candidates",
                _verification(),
            ),
            patch("app.agent.orchestrator._build_graph", return_value=graph),
            patch("app.proofs.service.envoyer_message_commercant", AsyncMock()),
            patch(
                "app.workers.whatsapp.envoyer_texte_whatsapp", send_text
            ),
            patch(
                "app.workers.whatsapp.envoyer_image_whatsapp", send_image
            ),
        ):
            await process_inbound_whatsapp_image_async(
                PHONE,
                "wamid.worker-photo",
                "media",
                "image/png",
                None,
                merchant.whatsapp_phone_number_id or "",
            )
            await process_inbound_whatsapp_image_async(
                PHONE,
                "wamid.worker-photo",
                "media",
                "image/png",
                None,
                merchant.whatsapp_phone_number_id or "",
            )
        assert send_text.await_count == 1
        assert send_text.await_args.args[1] == graph.reply
        assert send_image.await_count == 1
        assert send_image.await_args.args[1] == product_id
        async with AsyncSessionLocal() as db:
            images = list(
                (
                    await db.execute(
                        select(InboundImage).where(
                            InboundImage.whatsapp_message_id == "wamid.worker-photo"
                        )
                    )
                ).scalars().all()
            )
            assert len(images) == 1
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_worker_skips_already_sent_product_photo(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    merchant, first, _second = await _seed_shop(tmp_path)
    extra = [
        {
            "type": "function_call_output",
            "output": json.dumps(
                {
                    "products": [
                        {
                            "id": str(first.id),
                            "name": first.name,
                            "price": "25000",
                            "image_url": f"/static/product_images/{first.id}.png",
                        }
                    ]
                }
            ),
        }
    ]
    graph = _CapturingGraph(reply="C'est bien celui-ci ?", extra_items=extra)
    vision = AsyncMock(return_value=_product_photo_analysis())
    search = _search_stub(
        [ImageMatch(product=first, distance=0.10, in_stock=True)]
    )
    send_text = AsyncMock()
    send_image = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.core.config.settings.media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.telecharger_media_whatsapp",
                new=AsyncMock(return_value=(TINY_PNG, "image/png")),
            ),
            patch("app.proofs.service.classer_image_entrante", vision),
            patch("app.proofs.recognition.trouver_produits_par_image", search),
            patch("app.proofs.recognition.load_candidate_photos", _fake_photos),
            patch(
                "app.proofs.recognition.verify_image_against_candidates",
                _verification(),
            ),
            patch("app.agent.orchestrator._build_graph", return_value=graph),
            patch("app.proofs.service.envoyer_message_commercant", AsyncMock()),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", send_text),
            patch("app.workers.whatsapp.envoyer_image_whatsapp", send_image),
        ):
            await process_inbound_whatsapp_image_async(
                PHONE,
                "wamid.photo-1",
                "media",
                "image/png",
                None,
                merchant.whatsapp_phone_number_id or "",
            )
            with caplog.at_level("INFO", logger="app.workers.whatsapp"):
                await process_inbound_whatsapp_image_async(
                    PHONE,
                    "wamid.photo-2",
                    "media",
                    "image/png",
                    None,
                    merchant.whatsapp_phone_number_id or "",
                )
        assert send_image.await_count == 1
        assert "Skipping already-sent product photo" in caplog.text
        assert send_text.await_count == 2
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_worker_text_path_still_uses_shared_helper(tmp_path: Path) -> None:
    merchant, _first, _second = await _seed_shop(tmp_path)
    helper = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch(
                "app.workers.whatsapp.traiter_rafale_entrante",
                new=AsyncMock(return_value="Bonjour"),
            ),
            patch("app.workers.whatsapp.envoyer_reponse_whatsapp_agent", helper),
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.text-shared",
                PHONE,
                "bonjour",
                merchant.whatsapp_phone_number_id or "",
            )
        helper.assert_awaited_once()
        assert helper.await_args.args[1] == "Bonjour"
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_worker_sends_fallback_once_when_agent_turn_fails(
    tmp_path: Path,
) -> None:
    merchant, first, _second = await _seed_shop(tmp_path)
    vision = AsyncMock(return_value=_product_photo_analysis())
    search = _search_stub(
        [ImageMatch(product=first, distance=0.10, in_stock=True)]
    )
    send_text = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.core.config.settings.media_dir", str(tmp_path / "media")),
            patch(
                "app.proofs.service.telecharger_media_whatsapp",
                new=AsyncMock(return_value=(TINY_PNG, "image/png")),
            ),
            patch("app.proofs.service.classer_image_entrante", vision),
            patch("app.proofs.recognition.trouver_produits_par_image", search),
            patch("app.proofs.recognition.load_candidate_photos", _fake_photos),
            patch(
                "app.proofs.recognition.verify_image_against_candidates",
                _verification(),
            ),
            patch(
                "app.workers.whatsapp.traiter_rafale_entrante",
                new=AsyncMock(side_effect=RuntimeError("llm down")),
            ),
            patch("app.proofs.service.envoyer_message_commercant", AsyncMock()),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", send_text),
            patch("app.workers.whatsapp.envoyer_image_whatsapp", AsyncMock()),
        ):
            await process_inbound_whatsapp_image_async(
                PHONE,
                "wamid.agent-fail",
                "media",
                "image/png",
                None,
                merchant.whatsapp_phone_number_id or "",
            )
            await process_inbound_whatsapp_image_async(
                PHONE,
                "wamid.agent-fail",
                "media",
                "image/png",
                None,
                merchant.whatsapp_phone_number_id or "",
            )
        assert send_text.await_count == 1
        assert send_text.await_args.args[1] == FALLBACK_REPLY
    finally:
        await _cleanup(merchant.id)


def test_developer_item_similar_and_out_of_stock_same() -> None:
    similar = _match(name="Robe rouge", distance=0.20, stock_qty=8, price="25000")
    item = build_product_photo_developer_item(
        caption=None,
        analysis=None,
        result=RecognitionResult(
            level=MATCH_LEVEL_POSSIBLE,
            matches=[similar],
            proposed=[similar],
            proposal_kind=PROPOSAL_KIND_SIMILAR,
        ),
    )
    text = item["content"]
    assert "proposal_kind: similar" in text
    assert "does NOT have exactly this item" in text
    assert "colour or variant differs" in text
    assert "would interest the customer" in text
    assert "Ce n'est pas exactement ce modèle" in text
    assert "<exact name from the candidate list>" in text
    assert "use only the name and price listed in Candidates" in text
    assert "Always put that candidate's exact name" in text
    assert "robe longue noire de soirée" not in text
    assert "do NOT call `creer_commande`" in text
    assert "call `obtenir_disponibilite` for each listed candidate" in text
    assert "C'est bien celui-ci ?" in text

    oos = _match(name="Robe rouge", distance=0.08, stock_qty=0, price="25000")
    exact = build_product_photo_developer_item(
        caption=None,
        analysis=None,
        result=RecognitionResult(
            level=MATCH_LEVEL_STRONG,
            matches=[oos],
            proposed=[oos],
            proposal_kind=PROPOSAL_KIND_EXACT,
        ),
    )
    assert "stock: rupture" in exact["content"]
    assert "mention rupture honestly" in exact["content"]
    assert "stock: 0" not in exact["content"]
    assert "call `obtenir_disponibilite` for each listed candidate" in exact["content"]
    assert "C'est bien celui-ci ?" in exact["content"]


def test_persist_recognition_writes_verdict_and_verifier() -> None:
    image = InboundImage(
        merchant_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        whatsapp_message_id="x",
        mime_type="image/png",
        classification=CLASSIFICATION_PRODUCT_PHOTO,
        media_path="x.png",
    )
    first = _match(name="A", distance=0.22)
    second = _match(name="B", distance=0.31)
    persist_recognition(
        image,
        RecognitionResult(
            level=MATCH_LEVEL_POSSIBLE,
            matches=[first, second],
            proposed=[second],
            proposal_kind=PROPOSAL_KIND_SIMILAR,
            verifier_model="gpt-test",
            candidate_verdicts={1: VERDICT_NONE, 2: VERDICT_SIMILAR},
        ),
    )
    assert image.match_level == MATCH_LEVEL_POSSIBLE
    assert image.matched_product_id == second.product.id
    assert image.match_candidates[0]["verdict"] == "none"
    assert image.match_candidates[1]["verdict"] == "similar"
    assert image.match_candidates[1]["verifier"] == "gpt-test"
    assert "verifier" not in image.match_candidates[0]


async def _recognise_with(
    matches: list[ImageMatch] | Exception,
    *,
    verify=None,
    verify_enabled: bool = True,
) -> tuple:
    search = (
        AsyncMock(side_effect=matches)
        if isinstance(matches, Exception)
        else _search_stub(matches)
    )
    verify_fn = verify if verify is not None else _verification()
    with (
        patch("app.proofs.recognition.trouver_produits_par_image", search),
        patch("app.proofs.recognition.load_candidate_photos", _fake_photos),
        patch("app.proofs.recognition.verify_image_against_candidates", verify_fn),
        patch.object(settings, "image_match_verify_with_vision", verify_enabled),
    ):
        async with AsyncSessionLocal() as db:
            result = await recognise_product_photo(
                db, uuid.uuid4(), TINY_PNG, "image/png", caption="hi"
            )
    return result, search, verify_fn


@pytest.mark.asyncio
async def test_empty_shortlist_is_none_without_verification() -> None:
    far = _match(name="Far", distance=0.80)
    result, _search, verify = await _recognise_with([far])
    assert result.level == MATCH_LEVEL_NONE
    assert result.proposed == []
    verify.assert_not_awaited()


@pytest.mark.asyncio
async def test_shortlist_drops_candidates_above_retrieval_distance() -> None:
    keep = _match(name="Keep", distance=0.64)
    drop = _match(name="Drop", distance=0.66)
    result, _search, verify = await _recognise_with(
        [keep, drop], verify=_verification(VERDICT_SAME, 1)
    )
    assert result.level == MATCH_LEVEL_STRONG
    assert result.proposed[0].product.id == keep.product.id
    assert len(verify.await_args.args[2]) == 1
    assert result.matches[0].product.id == keep.product.id
    assert all(m.distance <= settings.image_match_retrieval_distance for m in result.matches)


@pytest.mark.asyncio
async def test_verification_same_similar_none_and_bad_index() -> None:
    first = _match(name="A", distance=0.30)
    second = _match(name="B", distance=0.40)
    matches = [first, second]

    same, _, verify_same = await _recognise_with(
        matches, verify=_verification(VERDICT_SAME, 1)
    )
    assert same.level == MATCH_LEVEL_STRONG
    assert same.proposal_kind == PROPOSAL_KIND_EXACT
    assert same.proposed == [first]
    verify_same.assert_awaited()

    similar, _, _ = await _recognise_with(
        matches, verify=_verification(VERDICT_SIMILAR, 2)
    )
    assert similar.level == MATCH_LEVEL_POSSIBLE
    assert similar.proposal_kind == PROPOSAL_KIND_SIMILAR
    assert similar.proposed == [second]

    none, _, _ = await _recognise_with(
        matches, verify=_verification(VERDICT_NONE, None)
    )
    assert none.level == MATCH_LEVEL_NONE
    assert none.proposed == []

    bad, _, _ = await _recognise_with(
        matches, verify=_verification(VERDICT_SAME, 9)
    )
    assert bad.level == MATCH_LEVEL_NONE
    assert bad.proposed == []


@pytest.mark.asyncio
async def test_verification_failure_disabled_never_strong() -> None:
    close = _match(name="Close", distance=0.10)
    failed, _, verify = await _recognise_with(
        [close],
        verify=AsyncMock(side_effect=ImageVerificationError("timeout")),
    )
    assert failed.level == MATCH_LEVEL_POSSIBLE
    assert failed.proposed == [close]
    verify.assert_awaited()

    disabled, _, verify_off = await _recognise_with(
        [close], verify_enabled=False
    )
    assert disabled.level == MATCH_LEVEL_POSSIBLE
    assert disabled.proposed == [close]
    verify_off.assert_not_awaited()

    crashed, _, _ = await _recognise_with(
        [close],
        verify=AsyncMock(side_effect=RuntimeError("openai down")),
    )
    assert crashed.level == MATCH_LEVEL_POSSIBLE
    assert crashed.proposed == [close]


@pytest.mark.asyncio
async def test_disabled_verification_can_propose_two_close_candidates() -> None:
    first = _match(name="A", distance=0.25)
    second = _match(name="B", distance=0.28)
    result, _, verify = await _recognise_with(
        [first, second], verify_enabled=False
    )
    assert result.level == MATCH_LEVEL_POSSIBLE
    assert result.proposed == [first, second]
    verify.assert_not_awaited()

