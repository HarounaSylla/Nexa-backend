import hashlib
import hmac
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import delete, select

from app.agent.models import Conversation, Message
from app.catalogue.models import Merchant
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.main import app
from app.whatsapp.service import (
    FALLBACK_REPLY,
    envoyer_image_whatsapp,
    extract_text_messages,
    verify_signature,
)
from app.workers.whatsapp import process_inbound_whatsapp_text_async


PHONE_NUMBER_ID = "test-phone-number-id"


async def _client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_linked_merchant() -> Merchant:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=f"pytest-wa-{uuid.uuid4()}",
            whatsapp_phone_number_id=f"{PHONE_NUMBER_ID}-{uuid.uuid4()}",
        )
        db.add(merchant)
        await db.commit()
        await db.refresh(merchant)
        return merchant


def _text_payload(
    *,
    phone_number_id: str,
    message_id: str = "wamid.test-1",
    customer_phone: str = "221770001300",
    body: str = "vous avez des robes ?",
    message_type: str = "text",
) -> dict:
    message: dict = {
        "from": customer_phone,
        "id": message_id,
        "type": message_type,
    }
    if message_type == "text":
        message["text"] = {"body": body}
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


def _status_payload() -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "statuses": [{"id": "wamid.status", "status": "delivered"}],
                        }
                    }
                ]
            }
        ],
    }


@pytest.mark.asyncio
async def test_webhook_verify_handshake() -> None:
    token = "verify-token-for-test"
    with patch.object(settings, "whatsapp_webhook_verify_token", token):
        async with await _client() as client:
            ok = await client.get(
                "/whatsapp/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": token,
                    "hub.challenge": "challenge-123",
                },
            )
            assert ok.status_code == 200
            assert ok.text == "challenge-123"
            forbidden = await client.get(
                "/whatsapp/webhook",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong",
                    "hub.challenge": "challenge-123",
                },
            )
            assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_webhook_post_enqueues_text_and_ignores_statuses() -> None:
    merchant = await _seed_linked_merchant()
    assert merchant.whatsapp_phone_number_id is not None
    try:
        with patch("app.whatsapp.router.enqueue_inbound_text") as enqueue:
            async with await _client() as client:
                statuses = await client.post(
                    "/whatsapp/webhook", json=_status_payload()
                )
                assert statuses.status_code == 200
                enqueue.assert_not_called()

                unknown = await client.post(
                    "/whatsapp/webhook",
                    json=_text_payload(phone_number_id="unknown-phone"),
                )
                assert unknown.status_code == 200
                enqueue.assert_not_called()

                inbound = await client.post(
                    "/whatsapp/webhook",
                    json=_text_payload(
                        phone_number_id=merchant.whatsapp_phone_number_id,
                        message_id="wamid.robes",
                    ),
                )
                assert inbound.status_code == 200
                enqueue.assert_called_once_with(
                    "wamid.robes",
                    "221770001300",
                    "vous avez des robes ?",
                    merchant.whatsapp_phone_number_id,
                )

                image = await client.post(
                    "/whatsapp/webhook",
                    json=_text_payload(
                        phone_number_id=merchant.whatsapp_phone_number_id,
                        message_id="wamid.img",
                        message_type="image",
                    ),
                )
                assert image.status_code == 200
                assert enqueue.call_count == 1
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature_when_secret_set() -> None:
    secret = "meta-app-secret"
    body = json.dumps(_status_payload()).encode()
    good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    with (
        patch.object(settings, "whatsapp_app_secret", secret),
        patch("app.whatsapp.router.enqueue_inbound_text") as enqueue,
    ):
        async with await _client() as client:
            bad = await client.post(
                "/whatsapp/webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-hub-signature-256": "sha256=deadbeef",
                },
            )
            assert bad.status_code == 403
            enqueue.assert_not_called()

            ok = await client.post(
                "/whatsapp/webhook",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-hub-signature-256": good,
                },
            )
            assert ok.status_code == 200
            enqueue.assert_not_called()


def test_verify_signature_and_extract_text_messages() -> None:
    secret = "abc"
    raw = b'{"ok":true}'
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    with patch.object(settings, "whatsapp_app_secret", secret):
        assert verify_signature(raw, f"sha256={digest}") is True
        assert verify_signature(raw, "sha256=nope") is False
        assert verify_signature(raw, None) is False
    with patch.object(settings, "whatsapp_app_secret", None):
        assert verify_signature(raw, None) is True

    texts = extract_text_messages(_text_payload(phone_number_id="pn"))
    assert texts == [
        {
            "message_id": "wamid.test-1",
            "customer_phone": "221770001300",
            "message_text": "vous avez des robes ?",
            "phone_number_id": "pn",
        }
    ]
    assert extract_text_messages(_status_payload()) == []


@pytest.mark.asyncio
async def test_worker_calls_orchestrator_once_then_sends() -> None:
    merchant = await _seed_linked_merchant()
    try:
        with (
            patch(
                "app.workers.whatsapp.claim_inbound_message", return_value=True
            ),
            patch(
                "app.workers.whatsapp.traiter_message_entrant",
                new_callable=AsyncMock,
                return_value="Oui, nous avons des robes.",
            ) as agent,
            patch(
                "app.workers.whatsapp.envoyer_texte_whatsapp",
                new_callable=AsyncMock,
            ) as send,
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.once",
                "221770001301",
                "vous avez des robes ?",
                merchant.whatsapp_phone_number_id or "",
            )
            agent.assert_awaited_once()
            args = agent.await_args
            assert args is not None
            assert args.args[1] == merchant.id
            assert args.args[2] == "221770001301"
            assert args.args[3] == "vous avez des robes ?"
            send.assert_awaited_once_with(
                "221770001301",
                "Oui, nous avons des robes.",
                merchant.whatsapp_phone_number_id,
            )
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_worker_skips_duplicates_and_unknown_merchant() -> None:
    with (
        patch("app.workers.whatsapp.claim_inbound_message", return_value=False),
        patch(
            "app.workers.whatsapp.traiter_message_entrant",
            new_callable=AsyncMock,
        ) as agent,
        patch(
            "app.workers.whatsapp.envoyer_texte_whatsapp",
            new_callable=AsyncMock,
        ) as send,
    ):
        await process_inbound_whatsapp_text_async(
            "wamid.dup", "221770001302", "hello", PHONE_NUMBER_ID
        )
        agent.assert_not_called()
        send.assert_not_called()

    with (
        patch("app.workers.whatsapp.claim_inbound_message", return_value=True),
        patch(
            "app.workers.whatsapp.traiter_message_entrant",
            new_callable=AsyncMock,
        ) as agent,
        patch(
            "app.workers.whatsapp.envoyer_texte_whatsapp",
            new_callable=AsyncMock,
        ) as send,
    ):
        await process_inbound_whatsapp_text_async(
            "wamid.unknown", "221770001303", "hello", "no-such-phone"
        )
        agent.assert_not_called()
        send.assert_not_called()


@pytest.mark.asyncio
async def test_worker_sends_fallback_when_agent_raises() -> None:
    merchant = await _seed_linked_merchant()
    try:
        with (
            patch(
                "app.workers.whatsapp.claim_inbound_message", return_value=True
            ),
            patch(
                "app.workers.whatsapp.traiter_message_entrant",
                new_callable=AsyncMock,
                side_effect=RuntimeError("openai down"),
            ) as agent,
            patch(
                "app.workers.whatsapp.envoyer_texte_whatsapp",
                new_callable=AsyncMock,
            ) as send,
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.fail",
                "221770001304",
                "vous avez des robes ?",
                merchant.whatsapp_phone_number_id or "",
            )
            agent.assert_awaited_once()
            send.assert_awaited_once_with(
                "221770001304",
                FALLBACK_REPLY,
                merchant.whatsapp_phone_number_id,
            )
    finally:
        await _cleanup(merchant.id)


_MINI_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_envoyer_image_whatsapp_uses_media_id_not_link(tmp_path: Path) -> None:
    product_id = uuid.uuid4()
    photo = tmp_path / f"{product_id}.png"
    photo.write_bytes(_MINI_PNG)
    captured: list[dict] = []

    class _FakeResponse:
        def __init__(self, payload: dict, status_code: int = 200) -> None:
            self._payload = payload
            self.status_code = status_code
            self.text = json.dumps(payload)

        def json(self) -> dict:
            return self._payload

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
            captured.append({"url": url, **kwargs})
            if url.endswith("/media"):
                return _FakeResponse({"id": "media-123"})
            return _FakeResponse({"messages": [{"id": "wamid.out"}]})

    with (
        patch("app.whatsapp.service.chemin_photo_locale", return_value=photo),
        patch.object(settings, "whatsapp_access_token", "token"),
        patch.object(settings, "whatsapp_api_version", "v21.0"),
        patch("app.whatsapp.service.httpx.AsyncClient", _FakeClient),
    ):
        await envoyer_image_whatsapp(
            "221770001305",
            product_id,
            PHONE_NUMBER_ID,
            caption="Robe rouge",
        )

    assert len(captured) == 2
    assert captured[0]["url"].endswith(f"/{PHONE_NUMBER_ID}/media")
    assert captured[0]["data"]["messaging_product"] == "whatsapp"
    assert captured[1]["json"]["type"] == "image"
    assert captured[1]["json"]["image"] == {
        "id": "media-123",
        "caption": "Robe rouge",
    }
    assert "link" not in captured[1]["json"]["image"]


@pytest.mark.asyncio
async def test_envoyer_image_whatsapp_missing_file_does_not_raise() -> None:
    with patch("app.whatsapp.service.chemin_photo_locale", return_value=None):
        await envoyer_image_whatsapp(
            "221770001306", uuid.uuid4(), PHONE_NUMBER_ID
        )


@pytest.mark.asyncio
async def test_worker_sends_text_then_caps_images_at_three() -> None:
    merchant = await _seed_linked_merchant()
    ids = [uuid.uuid4() for _ in range(4)]
    items = [
        {
            "type": "function_call_output",
            "output": json.dumps(
                {
                    "products": [
                        {
                            "id": str(product_id),
                            "name": f"Item {index}",
                            "image_url": f"/static/product_images/{product_id}.png",
                        }
                        for index, product_id in enumerate(ids)
                    ]
                }
            ),
        }
    ]
    try:
        async with AsyncSessionLocal() as db:
            conversation = Conversation(
                merchant_id=merchant.id,
                customer_phone="221770001307",
                status="active",
            )
            db.add(conversation)
            await db.flush()
            db.add(
                Message(
                    conversation_id=conversation.id,
                    turn_role="agent",
                    display_text="Voici quelques articles.",
                    items=items,
                )
            )
            await db.commit()

        with (
            patch(
                "app.workers.whatsapp.claim_inbound_message", return_value=True
            ),
            patch(
                "app.workers.whatsapp.traiter_message_entrant",
                new_callable=AsyncMock,
                return_value="Voici quelques articles.",
            ),
            patch(
                "app.workers.whatsapp.envoyer_texte_whatsapp",
                new_callable=AsyncMock,
            ) as send_text,
            patch(
                "app.workers.whatsapp.envoyer_image_whatsapp",
                new_callable=AsyncMock,
            ) as send_image,
        ):
            await process_inbound_whatsapp_text_async(
                "wamid.images",
                "221770001307",
                "vous avez des robes ?",
                merchant.whatsapp_phone_number_id or "",
            )
            send_text.assert_awaited_once()
            assert send_image.await_count == 3
            sent_ids = [call.args[1] for call in send_image.await_args_list]
            assert sent_ids == ids[:3]
            assert send_image.await_args_list[0].kwargs["caption"] == "Item 0"
    finally:
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
            if conversation_ids:
                await db.execute(
                    delete(Message).where(
                        Message.conversation_id.in_(conversation_ids)
                    )
                )
                await db.execute(
                    delete(Conversation).where(
                        Conversation.id.in_(conversation_ids)
                    )
                )
                await db.commit()
        await _cleanup(merchant.id)
