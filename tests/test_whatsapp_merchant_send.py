from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.catalogue.models import Merchant
from app.whatsapp.service import WhatsAppSendError, envoyer_message_commercant


def _merchant(*, phone_number_id: str | None) -> Merchant:
    return Merchant(
        name="pytest-wa-send",
        whatsapp_phone_number_id=phone_number_id,
    )


def _status_error(body: str | dict, status_code: int = 400) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://graph.facebook.com/v21.0/x/messages")
    if isinstance(body, dict):
        response = httpx.Response(status_code, request=request, json=body)
    else:
        response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError("error", request=request, response=response)


@pytest.mark.asyncio
async def test_envoyer_message_commercant_not_configured_without_phone_id() -> None:
    with (
        patch("app.whatsapp.service.settings.whatsapp_access_token", "tok"),
        patch(
            "app.whatsapp.service.envoyer_texte_whatsapp", new_callable=AsyncMock
        ) as send,
    ):
        with pytest.raises(WhatsAppSendError) as exc:
            await envoyer_message_commercant(_merchant(phone_number_id=None), "22177", "hi")
        assert exc.value.code == "whatsapp_not_configured"
        send.assert_not_called()


@pytest.mark.asyncio
async def test_envoyer_message_commercant_not_configured_without_token() -> None:
    with (
        patch("app.whatsapp.service.settings.whatsapp_access_token", ""),
        patch(
            "app.whatsapp.service.envoyer_texte_whatsapp", new_callable=AsyncMock
        ) as send,
    ):
        with pytest.raises(WhatsAppSendError) as exc:
            await envoyer_message_commercant(
                _merchant(phone_number_id="pnid"), "22177", "hi"
            )
        assert exc.value.code == "whatsapp_not_configured"
        send.assert_not_called()


@pytest.mark.asyncio
async def test_envoyer_message_commercant_maps_graph_and_network_errors() -> None:
    merchant = _merchant(phone_number_id="pnid-1")
    with patch("app.whatsapp.service.settings.whatsapp_access_token", "tok"):
        with patch(
            "app.whatsapp.service.envoyer_texte_whatsapp",
            new_callable=AsyncMock,
            side_effect=_status_error({"error": {"code": 131047}}),
        ):
            with pytest.raises(WhatsAppSendError) as closed:
                await envoyer_message_commercant(merchant, "22177", "hi")
            assert closed.value.code == "whatsapp_window_closed"

        with patch(
            "app.whatsapp.service.envoyer_texte_whatsapp",
            new_callable=AsyncMock,
            side_effect=_status_error({"error": {"code": 100}}),
        ):
            with pytest.raises(WhatsAppSendError) as other:
                await envoyer_message_commercant(merchant, "22177", "hi")
            assert other.value.code == "whatsapp_send_failed"

        with patch(
            "app.whatsapp.service.envoyer_texte_whatsapp",
            new_callable=AsyncMock,
            side_effect=_status_error("not-json"),
        ):
            with pytest.raises(WhatsAppSendError) as raw:
                await envoyer_message_commercant(merchant, "22177", "hi")
            assert raw.value.code == "whatsapp_send_failed"

        with patch(
            "app.whatsapp.service.envoyer_texte_whatsapp",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("offline"),
        ):
            with pytest.raises(WhatsAppSendError) as network:
                await envoyer_message_commercant(merchant, "22177", "hi")
            assert network.value.code == "whatsapp_send_failed"


@pytest.mark.asyncio
async def test_envoyer_message_commercant_success_forwards_ids() -> None:
    merchant = _merchant(phone_number_id="pnid-success")
    with (
        patch("app.whatsapp.service.settings.whatsapp_access_token", "tok"),
        patch(
            "app.whatsapp.service.envoyer_texte_whatsapp", new_callable=AsyncMock
        ) as send,
    ):
        await envoyer_message_commercant(merchant, "+221770009999", "bonjour")
        send.assert_awaited_once_with("+221770009999", "bonjour", "pnid-success")
