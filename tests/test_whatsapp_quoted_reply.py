import importlib.util
import inspect
import json
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import delete, inspect as sa_inspect, select, text

from app.agent.handover import (
    KIND_CUSTOMER_PHOTO,
    KIND_CUSTOMER_TEXT,
    KIND_SHOP_PHOTO,
    KIND_SHOP_TEXT,
    PHOTO_RECOGNITION_POSSIBLE,
    PHOTO_RECOGNITION_RECOGNIZED,
    PHOTO_RECOGNITION_UNANALYSED,
    PHOTO_RECOGNITION_UNRECOGNIZED,
    QUOTE_MARKER_PREFIX,
    quote_replay_marker,
    trim_quote_excerpt,
)
from app.agent.models import Conversation, Message, WhatsAppMessageRef
from app.agent.orchestrator import prior_items_for_agent
from app.agent.service import (
    STATUS_CLOSED,
    load_quote_replay_markers,
    record_whatsapp_message_ref,
    resolve_quote_marker,
)
from app.catalogue.models import Merchant, Product
from app.proofs.models import (
    CLASSIFICATION_PRODUCT_PHOTO,
    MATCH_LEVEL_NONE,
    MATCH_LEVEL_POSSIBLE,
    MATCH_LEVEL_STRONG,
    InboundImage,
)
from app.core.config import settings
from app.core.db import AsyncSessionLocal, engine
from app.whatsapp.service import (
    envoyer_texte_whatsapp,
    extract_image_messages,
    extract_text_messages,
    truncate_wamid,
    wamid_from_graph_payload,
)
from app.workers.whatsapp import (
    process_inbound_whatsapp_image,
    process_inbound_whatsapp_image_async,
    process_inbound_whatsapp_text,
    process_inbound_whatsapp_text_async,
)


PHONE_NUMBER_ID = "test-quoted-phone-id"


def _payload(*, message: dict, phone_number_id: str = "pn") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "messages": [message],
                        }
                    }
                ]
            }
        ],
    }


def test_extract_text_without_context_omits_reply_to() -> None:
    found = extract_text_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.new",
                "type": "text",
                "text": {"body": "oui"},
            }
        )
    )
    assert found == [
        {
            "message_id": "wamid.new",
            "customer_phone": "+221770001500",
            "message_text": "oui",
            "phone_number_id": "pn",
        }
    ]


def test_extract_text_with_context_id() -> None:
    found = extract_text_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.new",
                "type": "text",
                "text": {"body": "oui"},
                "context": {"from": "221770000000", "id": "wamid.OLD"},
            }
        )
    )
    assert found[0]["reply_to_message_id"] == "wamid.OLD"


def test_extract_image_with_and_without_context() -> None:
    without = extract_image_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.img",
                "type": "image",
                "image": {"id": "media-1", "mime_type": "image/jpeg"},
            }
        )
    )
    assert "reply_to_message_id" not in without[0]
    with_ctx = extract_image_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.img2",
                "type": "image",
                "image": {"id": "media-1", "mime_type": "image/jpeg"},
                "context": {"from": "221770000000", "id": "wamid.quoted"},
            }
        )
    )
    assert with_ctx[0]["reply_to_message_id"] == "wamid.quoted"


def test_extract_ignores_forwarded_and_malformed_context() -> None:
    forwarded = extract_text_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.fwd",
                "type": "text",
                "text": {"body": "oui"},
                "context": {"forwarded": True},
            }
        )
    )
    assert "reply_to_message_id" not in forwarded[0]
    referred = extract_text_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.ref",
                "type": "text",
                "text": {"body": "oui"},
                "context": {"referred_product": {"catalog_id": "1"}},
            }
        )
    )
    assert "reply_to_message_id" not in referred[0]
    malformed = extract_text_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.bad",
                "type": "text",
                "text": {"body": "oui"},
                "context": "not-a-dict",
            }
        )
    )
    assert "reply_to_message_id" not in malformed[0]
    reaction = extract_text_messages(
        _payload(
            message={
                "from": "221770001500",
                "id": "wamid.react",
                "type": "reaction",
                "reaction": {"message_id": "wamid.x", "emoji": "👍"},
                "context": {"id": "wamid.x"},
            }
        )
    )
    assert reaction == []


def test_job_signatures_accept_optional_trailing_reply_to() -> None:
    text_params = inspect.signature(process_inbound_whatsapp_text).parameters
    assert list(text_params)[-1] == "reply_to_message_id"
    assert text_params["reply_to_message_id"].default is None
    image_params = inspect.signature(process_inbound_whatsapp_image).parameters
    assert list(image_params)[-1] == "reply_to_message_id"
    assert image_params["reply_to_message_id"].default is None


def test_wamid_from_graph_payload_missing_id_is_none() -> None:
    assert wamid_from_graph_payload({"messages": [{}]}) is None
    assert wamid_from_graph_payload({}) is None
    assert wamid_from_graph_payload({"messages": [{"id": "wamid.ok"}]}) == "wamid.ok"


def test_truncate_wamid_does_not_log_full_long_id() -> None:
    long_id = "wamid." + ("a" * 80)
    short = truncate_wamid(long_id)
    assert long_id not in short
    assert short.endswith("…")


@pytest.mark.asyncio
async def test_envoyer_texte_graph_without_id_does_not_raise() -> None:
    class _FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict:
            return {"messages": [{}]}

        def raise_for_status(self) -> None:
            return None

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            del url, kwargs
            return _FakeResponse()

    with (
        patch.object(settings, "whatsapp_access_token", "token"),
        patch.object(settings, "whatsapp_api_version", "v21.0"),
        patch("app.whatsapp.service.httpx.AsyncClient", _FakeClient),
    ):
        wamid = await envoyer_texte_whatsapp("221770001501", "bonjour", PHONE_NUMBER_ID)
    assert wamid is None


def test_quote_replay_marker_kinds_and_excerpt_trim() -> None:
    product_id = uuid.uuid4()
    shop = quote_replay_marker(kind=KIND_SHOP_TEXT, excerpt="  Recap du panier.  ")
    assert shop is not None
    assert shop["role"] == "user"
    assert shop["content"].startswith(QUOTE_MARKER_PREFIX)
    assert "Recap du panier." in shop["content"]
    photo = quote_replay_marker(
        kind=KIND_SHOP_PHOTO,
        product_name="Article catalogue",
        product_id=product_id,
    )
    assert photo is not None
    assert "photo du produit" in photo["content"]
    assert str(product_id) in photo["content"]
    own = quote_replay_marker(kind=KIND_CUSTOMER_TEXT, excerpt="ok")
    assert own is not None
    assert "son propre message" in own["content"]
    own_photo = quote_replay_marker(kind=KIND_CUSTOMER_PHOTO)
    assert own_photo is not None
    assert own_photo["content"] == "[Le client répond à sa propre photo]"
    recognised = quote_replay_marker(
        kind=KIND_CUSTOMER_PHOTO,
        photo_recognition=PHOTO_RECOGNITION_RECOGNIZED,
        product_name="Article test quote",
        product_id=product_id,
    )
    assert recognised is not None
    assert "sa propre photo, reconnue comme « Article test quote »" in recognised["content"]
    assert str(product_id) in recognised["content"]
    possible = quote_replay_marker(
        kind=KIND_CUSTOMER_PHOTO,
        photo_recognition=PHOTO_RECOGNITION_POSSIBLE,
        product_name="Article test quote",
    )
    assert possible is not None
    assert "correspondance possible « Article test quote »" in possible["content"]
    unknown = quote_replay_marker(
        kind=KIND_CUSTOMER_PHOTO,
        photo_recognition=PHOTO_RECOGNITION_UNRECOGNIZED,
    )
    assert unknown is not None
    assert "non reconnue" in unknown["content"]
    unanalysed = quote_replay_marker(
        kind=KIND_CUSTOMER_PHOTO,
        photo_recognition=PHOTO_RECOGNITION_UNANALYSED,
    )
    assert unanalysed is not None
    assert "non analysée" in unanalysed["content"]
    earlier = quote_replay_marker(
        kind=KIND_CUSTOMER_PHOTO,
        from_earlier_conversation=True,
        photo_recognition=PHOTO_RECOGNITION_RECOGNIZED,
        product_name="Article test quote",
    )
    assert earlier is not None
    assert "ancienne photo (conversation précédente)" in earlier["content"]
    assert "reconnue comme" in earlier["content"]
    earlier_shop = quote_replay_marker(
        kind=KIND_SHOP_PHOTO,
        product_name="Article catalogue",
        product_id=product_id,
        from_earlier_conversation=True,
    )
    assert earlier_shop is not None
    assert "conversation précédente" in earlier_shop["content"]
    long_text = "a" * 250
    clipped = trim_quote_excerpt(long_text)
    assert len(clipped) == 200
    assert clipped.endswith("…")
    trimmed = quote_replay_marker(kind=KIND_SHOP_TEXT, excerpt=long_text)
    assert trimmed is not None
    assert "a" * 250 not in trimmed["content"]
    assert quote_replay_marker(kind=KIND_SHOP_TEXT, excerpt="   ") is None
    assert quote_replay_marker(kind=KIND_SHOP_PHOTO, product_name="") is None
    assert quote_replay_marker(kind="unknown", excerpt="x") is None


def test_prior_items_inserts_marker_without_mutating_stored_items() -> None:
    conversation_id = uuid.uuid4()
    customer_id = uuid.uuid4()
    stored_item = {"role": "user", "content": "oui"}
    customer = Message(
        id=customer_id,
        conversation_id=conversation_id,
        turn_role="customer",
        display_text="oui",
        items=[stored_item],
    )
    marker = quote_replay_marker(kind=KIND_SHOP_TEXT, excerpt="Proposition")
    assert marker is not None
    replayed = prior_items_for_agent([customer], {customer_id: marker})
    assert replayed[0] == marker
    assert replayed[1] == stored_item
    assert customer.items == [stored_item]
    assert marker not in customer.items
    assert QUOTE_MARKER_PREFIX not in json.dumps(customer.items)


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


@pytest.mark.asyncio
async def test_whatsapp_message_ref_storage_lookup_and_unresolvable() -> None:
    merchant = Merchant(name=f"pytest-quote-{uuid.uuid4()}")
    product = Product(
        merchant_id=uuid.uuid4(),
        name="Article test quote",
        price=Decimal("1000"),
        stock_qty=3,
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(merchant)
            await db.flush()
            product.merchant_id = merchant.id
            db.add(product)
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001510",
                status="active",
            )
            db.add(conversation)
            await db.flush()
            shop_msg = Message(
                conversation_id=conversation.id,
                turn_role="agent",
                display_text="Proposition boutique",
                items=[{"role": "assistant", "content": "Proposition boutique"}],
            )
            customer_msg = Message(
                conversation_id=conversation.id,
                turn_role="customer",
                display_text="oui",
                items=[{"role": "user", "content": "oui"}],
            )
            db.add_all([shop_msg, customer_msg])
            await db.flush()
            await record_whatsapp_message_ref(
                db,
                conversation.id,
                "wamid.shop-text",
                KIND_SHOP_TEXT,
                message_id=shop_msg.id,
                excerpt="Proposition boutique",
                commit=False,
            )
            await record_whatsapp_message_ref(
                db,
                conversation.id,
                "wamid.shop-photo",
                KIND_SHOP_PHOTO,
                product_id=product.id,
                commit=False,
            )
            await record_whatsapp_message_ref(
                db,
                conversation.id,
                "wamid.customer-oui",
                KIND_CUSTOMER_TEXT,
                message_id=customer_msg.id,
                excerpt="oui",
                reply_to_wamid="wamid.shop-text",
                commit=False,
            )
            await db.commit()
            conv_id = conversation.id
            product_id = product.id
            customer_id = customer_msg.id
            original_items = list(customer_msg.items)

        async with AsyncSessionLocal() as db:
            shop_marker = await resolve_quote_marker(
                db, conv_id, "wamid.shop-text"
            )
            assert shop_marker is not None
            assert "Proposition boutique" in shop_marker["content"]
            photo_marker = await resolve_quote_marker(
                db, conv_id, "wamid.shop-photo"
            )
            assert photo_marker is not None
            assert "Article test quote" in photo_marker["content"]
            assert str(product_id) in photo_marker["content"]
            assert await resolve_quote_marker(db, conv_id, "wamid.unknown") is None
            other_conv = uuid.uuid4()
            assert (
                await resolve_quote_marker(db, other_conv, "wamid.shop-text")
                is None
            )
            customer = await db.get(Message, customer_id)
            assert customer is not None
            markers = await load_quote_replay_markers(db, conv_id, [customer])
            assert customer_id in markers
            assert customer.items == original_items
            assert QUOTE_MARKER_PREFIX not in json.dumps(customer.items)
    finally:
        await _cleanup(merchant.id)


def _load_0023():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0023_whatsapp_message_refs.py"
    )
    spec = importlib.util.spec_from_file_location("rev_0023_wamid_refs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_migration_0023_table_and_reversible_ops() -> None:
    module = _load_0023()
    assert module.revision == "0023_whatsapp_message_refs"
    assert module.down_revision == "0022_inbound_image_match"
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert 'op.create_table(\n        "whatsapp_message_refs"' in source
    assert 'op.drop_table("whatsapp_message_refs")' in source

    def _inspect(sync_conn) -> dict:
        inspector = sa_inspect(sync_conn)
        assert inspector.has_table("whatsapp_message_refs")
        columns = {col["name"] for col in inspector.get_columns("whatsapp_message_refs")}
        indexes = {idx["name"] for idx in inspector.get_indexes("whatsapp_message_refs")}
        uniques = {
            uc["name"] for uc in inspector.get_unique_constraints("whatsapp_message_refs")
        }
        return {"columns": columns, "indexes": indexes, "uniques": uniques}

    async with engine.connect() as conn:
        info = await conn.run_sync(_inspect)
    assert {
        "id",
        "conversation_id",
        "wamid",
        "kind",
        "message_id",
        "product_id",
        "excerpt",
        "reply_to_wamid",
        "created_at",
    } <= info["columns"]
    assert "ix_whatsapp_message_refs_wamid" in info["indexes"]
    assert (
        "uq_whatsapp_message_refs_conversation_wamid" in info["uniques"]
        or "uq_whatsapp_message_refs_conversation_wamid" in info["indexes"]
    )


@pytest.mark.asyncio
async def test_worker_text_job_old_signature_omits_reply_to() -> None:
    merchant = Merchant(
        name=f"pytest-quote-job-{uuid.uuid4()}",
        whatsapp_phone_number_id=f"{PHONE_NUMBER_ID}-{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(merchant)
            await db.commit()
            await db.refresh(merchant)
        with (
            patch(
                "app.workers.whatsapp.claim_inbound_message", return_value=True
            ),
            patch(
                "app.workers.whatsapp.traiter_message_entrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as agent,
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.old-sig",
                "221770001520",
                "oui",
                merchant.whatsapp_phone_number_id or "",
            )
            assert agent.await_args is not None
            assert agent.await_args.kwargs["reply_to_message_id"] is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_worker_text_job_passes_reply_to_to_orchestrator() -> None:
    merchant = Merchant(
        name=f"pytest-quote-job2-{uuid.uuid4()}",
        whatsapp_phone_number_id=f"{PHONE_NUMBER_ID}-{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(merchant)
            await db.commit()
            await db.refresh(merchant)
        with (
            patch(
                "app.workers.whatsapp.claim_inbound_message", return_value=True
            ),
            patch(
                "app.workers.whatsapp.traiter_message_entrant",
                new_callable=AsyncMock,
                return_value=None,
            ) as agent,
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.new-sig",
                "221770001521",
                "oui",
                merchant.whatsapp_phone_number_id or "",
                "wamid.quoted",
            )
            assert agent.await_args is not None
            assert agent.await_args.kwargs["reply_to_message_id"] == "wamid.quoted"
            assert (
                agent.await_args.kwargs["inbound_whatsapp_message_id"]
                == "wamid.new-sig"
            )
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_worker_image_job_passes_optional_reply_to() -> None:
    merchant = Merchant(
        name=f"pytest-quote-img-{uuid.uuid4()}",
        whatsapp_phone_number_id=f"{PHONE_NUMBER_ID}-{uuid.uuid4()}",
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(merchant)
            await db.commit()
            await db.refresh(merchant)
        with (
            patch(
                "app.workers.whatsapp.claim_inbound_message", return_value=True
            ),
            patch(
                "app.proofs.service.traiter_image_entrante",
                new_callable=AsyncMock,
                return_value=None,
            ) as images,
        ):
            await process_inbound_whatsapp_image_async(
                "221770001523",
                "wamid.img-new",
                "media-1",
                "image/jpeg",
                "",
                merchant.whatsapp_phone_number_id or "",
                "wamid.quoted-img",
            )
            assert images.await_args is not None
            assert (
                images.await_args.kwargs["reply_to_message_id"] == "wamid.quoted-img"
            )
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_resolve_quote_marker_across_closed_conversation_same_customer() -> None:
    merchant = Merchant(name=f"pytest-quote-closed-{uuid.uuid4()}")
    product = Product(
        merchant_id=uuid.uuid4(),
        name="Article ancien",
        price=Decimal("1000"),
        stock_qty=3,
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(merchant)
            await db.flush()
            product.merchant_id = merchant.id
            db.add(product)
            closed = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001530",
                status=STATUS_CLOSED,
            )
            current = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001530",
                status="active",
            )
            db.add_all([closed, current])
            await db.flush()
            await record_whatsapp_message_ref(
                db,
                closed.id,
                "wamid.old-shop-photo",
                KIND_SHOP_PHOTO,
                product_id=product.id,
                commit=False,
            )
            await record_whatsapp_message_ref(
                db,
                closed.id,
                "wamid.old-customer-photo",
                KIND_CUSTOMER_PHOTO,
                commit=False,
            )
            db.add(
                InboundImage(
                    merchant_id=merchant.id,
                    conversation_id=closed.id,
                    whatsapp_message_id="wamid.old-customer-photo",
                    mime_type="image/png",
                    classification=CLASSIFICATION_PRODUCT_PHOTO,
                    match_level=MATCH_LEVEL_STRONG,
                    matched_product_id=product.id,
                )
            )
            await db.commit()
            current_id = current.id
            product_id = product.id

        async with AsyncSessionLocal() as db:
            shop = await resolve_quote_marker(
                db, current_id, "wamid.old-shop-photo"
            )
            assert shop is not None
            assert "Article ancien" in shop["content"]
            assert "conversation précédente" in shop["content"]
            assert str(product_id) in shop["content"]
            photo = await resolve_quote_marker(
                db, current_id, "wamid.old-customer-photo"
            )
            assert photo is not None
            assert "ancienne photo (conversation précédente)" in photo["content"]
            assert "reconnue comme « Article ancien »" in photo["content"]
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_resolve_quote_marker_rejects_other_customer_and_merchant() -> None:
    merchant = Merchant(name=f"pytest-quote-scope-{uuid.uuid4()}")
    other_merchant = Merchant(name=f"pytest-quote-scope-b-{uuid.uuid4()}")
    try:
        async with AsyncSessionLocal() as db:
            db.add_all([merchant, other_merchant])
            await db.flush()
            own = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001531",
                status="active",
            )
            other_customer = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001532",
                status="active",
            )
            other_shop = Conversation(
                merchant_id=other_merchant.id,
                customer_phone="+221770001531",
                status="active",
            )
            db.add_all([own, other_customer, other_shop])
            await db.flush()
            await record_whatsapp_message_ref(
                db,
                other_customer.id,
                "wamid.other-customer",
                KIND_SHOP_TEXT,
                excerpt="secret other customer",
                commit=False,
            )
            await record_whatsapp_message_ref(
                db,
                other_shop.id,
                "wamid.other-merchant",
                KIND_SHOP_TEXT,
                excerpt="secret other merchant",
                commit=False,
            )
            await db.commit()
            own_id = own.id

        async with AsyncSessionLocal() as db:
            assert (
                await resolve_quote_marker(db, own_id, "wamid.other-customer")
            ) is None
            assert (
                await resolve_quote_marker(db, own_id, "wamid.other-merchant")
            ) is None
    finally:
        await _cleanup(merchant.id, other_merchant.id)


@pytest.mark.asyncio
async def test_resolve_customer_photo_recognition_states() -> None:
    merchant = Merchant(name=f"pytest-quote-recog-{uuid.uuid4()}")
    product = Product(
        merchant_id=uuid.uuid4(),
        name="Article reconnu",
        price=Decimal("1000"),
        stock_qty=3,
    )
    try:
        async with AsyncSessionLocal() as db:
            db.add(merchant)
            await db.flush()
            product.merchant_id = merchant.id
            db.add(product)
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770001533",
                status="active",
            )
            db.add(conversation)
            await db.flush()
            await record_whatsapp_message_ref(
                db, conversation.id, "wamid.photo-strong", KIND_CUSTOMER_PHOTO, commit=False
            )
            await record_whatsapp_message_ref(
                db, conversation.id, "wamid.photo-possible", KIND_CUSTOMER_PHOTO, commit=False
            )
            await record_whatsapp_message_ref(
                db, conversation.id, "wamid.photo-none", KIND_CUSTOMER_PHOTO, commit=False
            )
            await record_whatsapp_message_ref(
                db, conversation.id, "wamid.photo-plain", KIND_CUSTOMER_PHOTO, commit=False
            )
            db.add_all(
                [
                    InboundImage(
                        merchant_id=merchant.id,
                        conversation_id=conversation.id,
                        whatsapp_message_id="wamid.photo-strong",
                        mime_type="image/png",
                        classification=CLASSIFICATION_PRODUCT_PHOTO,
                        match_level=MATCH_LEVEL_STRONG,
                        matched_product_id=product.id,
                    ),
                    InboundImage(
                        merchant_id=merchant.id,
                        conversation_id=conversation.id,
                        whatsapp_message_id="wamid.photo-possible",
                        mime_type="image/png",
                        classification=CLASSIFICATION_PRODUCT_PHOTO,
                        match_level=MATCH_LEVEL_POSSIBLE,
                        matched_product_id=product.id,
                    ),
                    InboundImage(
                        merchant_id=merchant.id,
                        conversation_id=conversation.id,
                        whatsapp_message_id="wamid.photo-none",
                        mime_type="image/png",
                        classification=CLASSIFICATION_PRODUCT_PHOTO,
                        match_level=MATCH_LEVEL_NONE,
                    ),
                ]
            )
            await db.commit()
            conv_id = conversation.id
            stored = (
                await db.execute(
                    select(WhatsAppMessageRef).where(
                        WhatsAppMessageRef.wamid == "wamid.photo-plain"
                    )
                )
            ).scalar_one()
            original_kind = stored.kind

        async with AsyncSessionLocal() as db:
            strong = await resolve_quote_marker(db, conv_id, "wamid.photo-strong")
            assert strong is not None
            assert "reconnue comme « Article reconnu »" in strong["content"]
            assert "conversation précédente" not in strong["content"]
            possible = await resolve_quote_marker(db, conv_id, "wamid.photo-possible")
            assert possible is not None
            assert "correspondance possible « Article reconnu »" in possible["content"]
            none = await resolve_quote_marker(db, conv_id, "wamid.photo-none")
            assert none is not None
            assert "non reconnue" in none["content"]
            plain = await resolve_quote_marker(db, conv_id, "wamid.photo-plain")
            assert plain is not None
            assert plain["content"] == "[Le client répond à sa propre photo]"
            stored = (
                await db.execute(
                    select(WhatsAppMessageRef).where(
                        WhatsAppMessageRef.wamid == "wamid.photo-plain"
                    )
                )
            ).scalar_one()
            assert stored.kind == original_kind
    finally:
        await _cleanup(merchant.id)
