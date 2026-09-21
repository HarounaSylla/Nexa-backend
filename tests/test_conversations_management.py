import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, select

from app.agent.models import Conversation, Message
from app.agent.service import STATUS_ACTIVE, STATUS_ESCALATED, TURN_ROLE_MERCHANT
from app.catalogue.models import Merchant
from app.core.db import AsyncSessionLocal
from app.main import app


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _auth(clerk_user_id: str):
    return patch(
        "app.auth.deps.verify_clerk_session_token",
        return_value=clerk_user_id,
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer valid.token"}


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
                delete(Message).where(Message.conversation_id.in_(conversation_ids))
            )
            await db.execute(
                delete(Conversation).where(Conversation.id.in_(conversation_ids))
            )
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_merchant(name: str, clerk_user_id: str) -> Merchant:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name, clerk_user_id=clerk_user_id)
        db.add(merchant)
        await db.commit()
        await db.refresh(merchant)
        return merchant


async def _add_conversation(
    merchant_id: uuid.UUID,
    phone: str,
    *,
    status: str = STATUS_ACTIVE,
    updated_at: datetime | None = None,
    messages: list[tuple[str, str]] | None = None,
) -> Conversation:
    async with AsyncSessionLocal() as db:
        conversation = Conversation(
            merchant_id=merchant_id,
            customer_phone=phone,
            status=status,
        )
        if updated_at is not None:
            conversation.updated_at = updated_at
        db.add(conversation)
        await db.flush()
        stamp = updated_at or datetime.now(timezone.utc)
        for index, (turn_role, text) in enumerate(messages or []):
            db.add(
                Message(
                    conversation_id=conversation.id,
                    turn_role=turn_role,
                    display_text=text,
                    items=[],
                    created_at=stamp + timedelta(seconds=index),
                )
            )
        await db.commit()
        await db.refresh(conversation)
        return conversation


@pytest.mark.asyncio
async def test_conversation_list_and_thread_are_merchant_scoped() -> None:
    owner_clerk = f"user_conv_owner_{uuid.uuid4()}"
    other_clerk = f"user_conv_other_{uuid.uuid4()}"
    owner = await _seed_merchant(f"pytest-conv-owner-{uuid.uuid4()}", owner_clerk)
    other = await _seed_merchant(f"pytest-conv-other-{uuid.uuid4()}", other_clerk)
    now = datetime.now(timezone.utc)
    try:
        escalated = await _add_conversation(
            owner.id,
            "+221770001001",
            status=STATUS_ESCALATED,
            updated_at=now - timedelta(hours=2),
            messages=[
                ("customer", "je veux un humain"),
                ("agent", "[escalade] réclamation"),
            ],
        )
        active = await _add_conversation(
            owner.id,
            "+221770001002",
            status=STATUS_ACTIVE,
            updated_at=now,
            messages=[("customer", "bonjour")],
        )
        theirs = await _add_conversation(
            other.id,
            "+221770001099",
            status=STATUS_ESCALATED,
            updated_at=now,
            messages=[("customer", "secret other shop")],
        )

        with _auth(owner_clerk):
            async with await _client() as client:
                listed = await client.get("/conversations", headers=_headers())
                assert listed.status_code == 200, listed.text
                rows = listed.json()
                ids = [row["id"] for row in rows]
                assert ids[0] == str(escalated.id)
                assert str(active.id) in ids
                assert str(theirs.id) not in ids
                first = rows[0]
                assert first["status"] == STATUS_ESCALATED
                assert first["customer_phone"] == "+221770001001"
                assert first["message_count"] == 2
                assert "escalade" in first["last_message_preview"]

                thread = await client.get(
                    f"/conversations/{escalated.id}/messages",
                    headers=_headers(),
                )
                assert thread.status_code == 200, thread.text
                body = thread.json()
                assert [item["turn_role"] for item in body] == ["customer", "agent"]
                assert body[0]["display_text"] == "je veux un humain"

                hidden = await client.get(
                    f"/conversations/{theirs.id}/messages",
                    headers=_headers(),
                )
                assert hidden.status_code == 404
                assert hidden.json()["detail"] == "Conversation was not found"
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_human_reply_is_merchant_role_and_does_not_call_agent() -> None:
    clerk_user_id = f"user_conv_reply_{uuid.uuid4()}"
    merchant = await _seed_merchant(
        f"pytest-conv-reply-{uuid.uuid4()}", clerk_user_id
    )
    try:
        conversation = await _add_conversation(
            merchant.id,
            "+221770001011",
            status=STATUS_ESCALATED,
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            messages=[
                ("customer", "réclamation"),
                ("agent", "[escalade] client mécontent"),
            ],
        )
        with (
            _auth(clerk_user_id),
            patch("app.agent.orchestrator.traiter_message_entrant") as agent_loop,
            patch("app.agent.tools.execute_tool") as tools,
        ):
            async with await _client() as client:
                replied = await client.post(
                    f"/conversations/{conversation.id}/reply",
                    headers=_headers(),
                    json={"message": "Bonjour, je m'en occupe."},
                )
                assert replied.status_code == 200, replied.text
                payload = replied.json()
                assert payload["turn_role"] == TURN_ROLE_MERCHANT
                assert payload["display_text"] == "Bonjour, je m'en occupe."

                thread = await client.get(
                    f"/conversations/{conversation.id}/messages",
                    headers=_headers(),
                )
                roles = [item["turn_role"] for item in thread.json()]
                texts = [item["display_text"] for item in thread.json()]
                assert roles == ["customer", "agent", TURN_ROLE_MERCHANT]
                assert texts[-1] == "Bonjour, je m'en occupe."

        agent_loop.assert_not_called()
        tools.assert_not_called()
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_return_to_agent_clears_escalated_and_409_otherwise() -> None:
    clerk_user_id = f"user_conv_return_{uuid.uuid4()}"
    merchant = await _seed_merchant(
        f"pytest-conv-return-{uuid.uuid4()}", clerk_user_id
    )
    try:
        escalated = await _add_conversation(
            merchant.id,
            "+221770001021",
            status=STATUS_ESCALATED,
            messages=[("customer", "humain svp")],
        )
        active = await _add_conversation(
            merchant.id,
            "+221770001022",
            status=STATUS_ACTIVE,
            messages=[("customer", "ok")],
        )
        with _auth(clerk_user_id):
            async with await _client() as client:
                returned = await client.post(
                    f"/conversations/{escalated.id}/return-to-agent",
                    headers=_headers(),
                )
                assert returned.status_code == 200, returned.text
                assert returned.json()["status"] == STATUS_ACTIVE

                listed = await client.get("/conversations", headers=_headers())
                by_id = {row["id"]: row["status"] for row in listed.json()}
                assert by_id[str(escalated.id)] == STATUS_ACTIVE

                rejected = await client.post(
                    f"/conversations/{active.id}/return-to-agent",
                    headers=_headers(),
                )
                assert rejected.status_code == 409
                assert "not escalated" in rejected.json()["detail"]
    finally:
        await _cleanup(merchant.id)
