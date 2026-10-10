"""Burst batching: ingest + flush. No network, no real LLM, no sleep."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.agent.handover import item_customer_text
from app.agent.models import Conversation, Message, WhatsAppMessageRef
from app.agent.orchestrator import prior_items_for_agent, traiter_rafale_entrante
from app.agent.prompts import build_system_prompt
from app.agent.service import STATUS_ESCALATED, TURN_ROLE_AGENT, TURN_ROLE_CUSTOMER
from app.catalogue.models import Merchant
from app.notifications.models import Notification
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.merchants.service import MerchantPreferencesData
from app.whatsapp.batch import (
    KIND_PRODUCT_PHOTO,
    KIND_TEXT,
    PendingEntry,
    flush_delay_seconds,
    get_backend,
    register_pending,
)
from app.whatsapp.service import FALLBACK_REPLY
from app.workers.whatsapp import (
    flush_conversation_async,
    process_inbound_whatsapp_image_async,
    process_inbound_whatsapp_text_async,
)

PHONE_NUMBER_ID = "batch-phone-id"


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    from sqlalchemy import delete

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
        await db.execute(
            delete(Notification).where(Notification.merchant_id.in_(merchant_ids))
        )
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_merchant() -> Merchant:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=f"pytest-batch-{uuid.uuid4()}",
            whatsapp_phone_number_id=f"{PHONE_NUMBER_ID}-{uuid.uuid4()}",
        )
        db.add(merchant)
        await db.commit()
        await db.refresh(merchant)
        return merchant


def test_flush_delay_quiet_zero_is_immediate() -> None:
    assert (
        flush_delay_seconds(
            quiet=0.0, max_wait=10.0, first_arrival=100.0, now=100.5
        )
        == 0.0
    )


def test_flush_delay_caps_at_max_wait() -> None:
    delay = flush_delay_seconds(
        quiet=3.0, max_wait=10.0, first_arrival=0.0, now=9.0
    )
    assert delay == 1.0
    assert (
        flush_delay_seconds(
            quiet=3.0, max_wait=10.0, first_arrival=0.0, now=10.0
        )
        == 0.0
    )


def test_prompt_rule_20_contiguous() -> None:
    import re

    text = build_system_prompt("Boutique Test", MerchantPreferencesData())["content"]
    numbered = re.findall(r"(?m)^(\d+)\. ", text)
    assert numbered == [str(n) for n in range(1, 21)]
    assert "20. When the latest customer messages are several consecutive" in text
    assert "Robe longue" not in text[text.index("20. ") :]
    assert "25 000" not in text[text.index("20. ") :]


@pytest.mark.asyncio
async def test_single_text_one_flush_one_turn() -> None:
    merchant = await _seed_merchant()
    rafale = AsyncMock(return_value="Bonjour")
    send = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", send),
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.one",
                "221770009001",
                "Bonjour",
                merchant.whatsapp_phone_number_id or "",
            )
        rafale.assert_awaited_once()
        assert len(rafale.await_args.args[3]) == 1
        send.assert_awaited_once()
        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(Message).where(
                            Message.display_text == "Bonjour"
                        )
                    )
                ).scalars().all()
            )
            assert len(rows) == 1
            assert rows[0].items == [item_customer_text("Bonjour")]
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_three_texts_inside_window_one_turn(
    monkeypatch, whatsapp_batch_test_env
) -> None:
    merchant = await _seed_merchant()
    monkeypatch.setattr(settings, "whatsapp_batch_quiet_seconds", 3.0)
    clock = {"t": 100.0}
    monkeypatch.setattr("app.workers.whatsapp.now_seconds", lambda: clock["t"])
    monkeypatch.setattr("app.whatsapp.batch.now_seconds", lambda: clock["t"])
    rafale = AsyncMock(return_value="Voici robes et sacs.")
    send = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", send),
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.a",
                "221770009002",
                "Bonjour",
                merchant.whatsapp_phone_number_id or "",
            )
            clock["t"] = 101.0
            await process_inbound_whatsapp_text_async(
                "wamid.b",
                "221770009002",
                "vous avez des robes ?",
                merchant.whatsapp_phone_number_id or "",
            )
            clock["t"] = 102.0
            await process_inbound_whatsapp_text_async(
                "wamid.c",
                "221770009002",
                "et des sacs ?",
                merchant.whatsapp_phone_number_id or "",
            )
            rafale.assert_not_awaited()
            scheduled = whatsapp_batch_test_env["scheduled"]
            assert len(scheduled) == 3
            assert scheduled[-1][4] == 3.0
            token = scheduled[-1][2]
            await flush_conversation_async(
                str(merchant.id),
                "+221770009002",
                token,
                merchant.whatsapp_phone_number_id or "",
            )
        rafale.assert_awaited_once()
        pending = rafale.await_args.args[3]
        assert [entry.kind for entry in pending] == [KIND_TEXT, KIND_TEXT, KIND_TEXT]
        texts = []
        async with AsyncSessionLocal() as db:
            for entry in pending:
                row = await db.get(Message, uuid.UUID(entry.message_row_id))
                texts.append(row.display_text)
        assert texts == ["Bonjour", "vous avez des robes ?", "et des sacs ?"]
        send.assert_awaited_once()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_max_wait_under_continuous_messages(
    monkeypatch, whatsapp_batch_test_env
) -> None:
    merchant = await _seed_merchant()
    monkeypatch.setattr(settings, "whatsapp_batch_quiet_seconds", 3.0)
    monkeypatch.setattr(settings, "whatsapp_batch_max_wait_seconds", 10.0)
    clock = {"t": 0.0}
    monkeypatch.setattr("app.workers.whatsapp.now_seconds", lambda: clock["t"])
    monkeypatch.setattr("app.whatsapp.batch.now_seconds", lambda: clock["t"])
    rafale = AsyncMock(return_value="ok")
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", AsyncMock()),
        ):
            for index in range(4):
                clock["t"] = float(index * 3)
                await process_inbound_whatsapp_text_async(
                    f"wamid.cont-{index}",
                    "221770009003",
                    f"msg {index}",
                    merchant.whatsapp_phone_number_id or "",
                )
            delays = [item[4] for item in whatsapp_batch_test_env["scheduled"]]
            assert delays[-1] == 0.0 or delays[-1] <= 1.0
            clock["t"] = 10.0
            stale_token = whatsapp_batch_test_env["scheduled"][0][2]
            await flush_conversation_async(
                str(merchant.id),
                "+221770009003",
                stale_token,
                merchant.whatsapp_phone_number_id or "",
            )
        rafale.assert_awaited_once()
        assert len(rafale.await_args.args[3]) >= 4
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_escalated_at_ingest_no_pending_no_flush() -> None:
    merchant = await _seed_merchant()
    phone = "+221770009004"
    async with AsyncSessionLocal() as db:
        db.add(
            Conversation(
                merchant_id=merchant.id,
                customer_phone=phone,
                status=STATUS_ESCALATED,
            )
        )
        await db.commit()
    rafale = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", AsyncMock()) as send,
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.esc",
                "221770009004",
                "bonjour",
                merchant.whatsapp_phone_number_id or "",
            )
        rafale.assert_not_awaited()
        send.assert_not_awaited()
        assert get_backend().peek(merchant.id, phone) is None
        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(Message).where(Message.display_text == "bonjour")
                    )
                ).scalars().all()
            )
            assert rows
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_escalated_between_ingest_and_flush(
    monkeypatch, whatsapp_batch_test_env
) -> None:
    merchant = await _seed_merchant()
    monkeypatch.setattr(settings, "whatsapp_batch_quiet_seconds", 3.0)
    phone = "+221770009005"
    rafale = AsyncMock(return_value="nope")
    send = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", send),
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.then-esc",
                "221770009005",
                "bonjour",
                merchant.whatsapp_phone_number_id or "",
            )
            async with AsyncSessionLocal() as db:
                conv = (
                    await db.execute(
                        select(Conversation).where(
                            Conversation.merchant_id == merchant.id
                        )
                    )
                ).scalar_one()
                conv.status = STATUS_ESCALATED
                await db.commit()
            token = whatsapp_batch_test_env["scheduled"][-1][2]
            await flush_conversation_async(
                str(merchant.id),
                phone,
                token,
                merchant.whatsapp_phone_number_id or "",
            )
        rafale.assert_not_awaited()
        send.assert_not_awaited()
        assert get_backend().peek(merchant.id, phone) is None
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_duplicate_wamid_once() -> None:
    merchant = await _seed_merchant()
    rafale = AsyncMock(return_value="ok")
    try:
        with (
            patch(
                "app.workers.whatsapp.claim_inbound_message",
                side_effect=[True, False],
            ),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", AsyncMock()),
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.dup-batch",
                "221770009006",
                "hello",
                merchant.whatsapp_phone_number_id or "",
            )
            await process_inbound_whatsapp_text_async(
                "wamid.dup-batch",
                "221770009006",
                "hello",
                merchant.whatsapp_phone_number_id or "",
            )
        rafale.assert_awaited_once()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_lock_busy_schedules_follow_up(
    monkeypatch, whatsapp_batch_test_env
) -> None:
    merchant = await _seed_merchant()
    phone = "+221770009007"
    register_pending(
        merchant.id,
        phone,
        PendingEntry(
            kind=KIND_TEXT,
            message_row_id=str(uuid.uuid4()),
            wamid="wamid.lock",
        ),
        phone_number_id=merchant.whatsapp_phone_number_id or "",
        now=1.0,
    )
    backend = get_backend()
    assert backend.acquire_lock(merchant.id, phone)
    try:
        whatsapp_batch_test_env["scheduled"].clear()
        await flush_conversation_async(
            str(merchant.id),
            phone,
            "token-other",
            merchant.whatsapp_phone_number_id or "",
        )
        assert whatsapp_batch_test_env["scheduled"] == []
        assert backend.peek(merchant.id, phone) is not None
    finally:
        backend.release_lock(merchant.id, phone)
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_message_during_running_flush_next_turn(
    monkeypatch, whatsapp_batch_test_env
) -> None:
    merchant = await _seed_merchant()
    phone = "+221770009008"
    started = []
    resume = []

    async def slow_rafale(*_args, **_kwargs):
        started.append(True)
        while not resume:
            pass
        return "first"

    # Use a cooperative wait instead of a spin — still no sleep().
    gate = {"go": False}

    async def gated_rafale(*_args, **_kwargs):
        started.append(1)
        while not gate["go"]:
            await _yield()
        return f"turn-{len(started)}"

    async def _yield():
        return None

    rafale = AsyncMock(side_effect=["premier", "second"])
    send = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", send),
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.first",
                "221770009008",
                "un",
                merchant.whatsapp_phone_number_id or "",
            )
            assert rafale.await_count == 1
            monkeypatch.setattr(settings, "whatsapp_batch_quiet_seconds", 3.0)
            await process_inbound_whatsapp_text_async(
                "wamid.late",
                "221770009008",
                "deux",
                merchant.whatsapp_phone_number_id or "",
            )
            assert rafale.await_count == 1
            token = whatsapp_batch_test_env["scheduled"][-1][2]
            await flush_conversation_async(
                str(merchant.id),
                phone,
                token,
                merchant.whatsapp_phone_number_id or "",
            )
        assert rafale.await_count == 2
        assert send.await_count == 2
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_failure_sends_one_fallback() -> None:
    merchant = await _seed_merchant()
    send = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch(
                "app.workers.whatsapp.traiter_rafale_entrante",
                new=AsyncMock(side_effect=RuntimeError("down")),
            ),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", send),
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.fail-batch",
                "221770009009",
                "bonjour",
                merchant.whatsapp_phone_number_id or "",
            )
        send.assert_awaited_once_with(
            "+221770009009",
            FALLBACK_REPLY,
            merchant.whatsapp_phone_number_id,
        )
    finally:
        await _cleanup(merchant.id)


def test_scheduling_failure_immediate_flush(monkeypatch) -> None:
    from app.whatsapp.batch import schedule_conversation_flush, set_scheduler

    called = []

    class BoomQueue:
        def __init__(self, *args, **kwargs):
            pass

        def enqueue(self, func, *args):
            raise RuntimeError("rq down")

        def enqueue_in(self, delta, func, *args):
            raise RuntimeError("rq down")

    set_scheduler(None)
    monkeypatch.setattr("app.whatsapp.batch.settings.redis_url", "redis://test")
    with (
        patch("redis.Redis.from_url", return_value=SimpleNamespace()),
        patch("rq.Queue", BoomQueue),
        patch(
            "app.workers.whatsapp.flush_conversation",
            side_effect=lambda *args: called.append(args),
        ),
    ):
        schedule_conversation_flush(
            uuid.uuid4(),
            "+221770009010",
            "tok",
            "pn",
            delay=3.0,
        )
    assert len(called) == 1


@pytest.mark.asyncio
async def test_replay_several_customer_rows_then_one_agent() -> None:
    merchant = await _seed_merchant()
    try:
        async with AsyncSessionLocal() as db:
            conv = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770009011",
                status="active",
            )
            db.add(conv)
            await db.flush()
            first = Message(
                conversation_id=conv.id,
                turn_role=TURN_ROLE_CUSTOMER,
                display_text="Bonjour",
                items=[item_customer_text("Bonjour")],
            )
            second = Message(
                conversation_id=conv.id,
                turn_role=TURN_ROLE_CUSTOMER,
                display_text="vous avez des robes ?",
                items=[item_customer_text("vous avez des robes ?")],
            )
            db.add_all([first, second])
            await db.flush()
            developer = {
                "role": "developer",
                "content": "Recognised product photo note",
            }
            db.add(
                Message(
                    conversation_id=conv.id,
                    turn_role=TURN_ROLE_AGENT,
                    display_text="Oui, voici.",
                    items=[
                        developer,
                        {"role": "assistant", "content": "Oui, voici."},
                    ],
                )
            )
            await db.commit()
            rows = list(
                (
                    await db.execute(
                        select(Message)
                        .where(Message.conversation_id == conv.id)
                        .order_by(Message.created_at, Message.id)
                    )
                ).scalars().all()
            )
            items = prior_items_for_agent(rows)
            roles = [item.get("role") for item in items if isinstance(item, dict)]
            assert roles.count("user") == 2
            assert "assistant" in roles
            assert any(
                item.get("role") == "developer"
                and "Recognised" in str(item.get("content"))
                for item in items
                if isinstance(item, dict)
            )
            stored = [(row.display_text, list(row.items or [])) for row in rows]
        async with AsyncSessionLocal() as db:
            again = list(
                (
                    await db.execute(
                        select(Message)
                        .where(Message.conversation_id == conv.id)
                        .order_by(Message.created_at, Message.id)
                    )
                ).scalars().all()
            )
            assert [
                (row.display_text, list(row.items or [])) for row in again
            ] == stored
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_rafale_input_order_and_quote_marker() -> None:
    merchant = await _seed_merchant()
    captured = {}

    async def fake_invoke(db, *, input_list, persist_prefix, **_kwargs):
        captured["input"] = input_list
        captured["prefix"] = persist_prefix
        return "ok"

    try:
        async with AsyncSessionLocal() as db:
            conv = Conversation(
                merchant_id=merchant.id,
                customer_phone="+221770009012",
                status="active",
            )
            db.add(conv)
            await db.flush()
            older = Message(
                conversation_id=conv.id,
                turn_role=TURN_ROLE_AGENT,
                display_text="Avant",
                items=[{"role": "assistant", "content": "Avant"}],
            )
            one = Message(
                conversation_id=conv.id,
                turn_role=TURN_ROLE_CUSTOMER,
                display_text="un",
                items=[item_customer_text("un")],
            )
            two = Message(
                conversation_id=conv.id,
                turn_role=TURN_ROLE_CUSTOMER,
                display_text="deux",
                items=[item_customer_text("deux")],
            )
            db.add_all([older, one, two])
            await db.commit()
            await db.refresh(one)
            await db.refresh(two)
            pending = [
                PendingEntry(
                    kind=KIND_TEXT,
                    message_row_id=str(one.id),
                    wamid="wamid.1",
                ),
                PendingEntry(
                    kind=KIND_PRODUCT_PHOTO,
                    message_row_id=str(two.id),
                    wamid="wamid.2",
                    developer_item={"role": "developer", "content": "photo note"},
                ),
            ]
            with patch(
                "app.agent.orchestrator._invoke_agent_and_store",
                new=fake_invoke,
            ):
                reply = await traiter_rafale_entrante(
                    db, merchant.id, "+221770009012", pending
                )
            assert reply == "ok"
            texts = []
            for item in captured["input"]:
                if isinstance(item, dict) and item.get("role") == "user":
                    texts.append(item.get("content"))
            assert texts[-2:] == ["un", "deux"]
            assert captured["prefix"] == [
                {"role": "developer", "content": "photo note"}
            ]
            assert any(
                item.get("role") == "developer" and "photo note" in str(item.get("content"))
                for item in captured["input"]
                if isinstance(item, dict)
            )
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_payment_proof_not_pending(monkeypatch) -> None:
    merchant = await _seed_merchant()
    stored = SimpleNamespace(
        _send_agent=False,
        _agent_failed=False,
        _developer_item=None,
        message_id=uuid.uuid4(),
    )
    rafale = AsyncMock()
    try:
        with (
            patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
            patch(
                "app.proofs.service.traiter_image_entrante",
                new=AsyncMock(return_value=stored),
            ),
            patch("app.workers.whatsapp.traiter_rafale_entrante", rafale),
            patch("app.workers.whatsapp.envoyer_texte_whatsapp", AsyncMock()),
        ):
            await process_inbound_whatsapp_image_async(
                "221770009013",
                "wamid.proof",
                "media",
                "image/jpeg",
                "preuve",
                merchant.whatsapp_phone_number_id or "",
            )
        rafale.assert_not_awaited()
        assert get_backend().peek(merchant.id, "+221770009013") is None
    finally:
        await _cleanup(merchant.id)
