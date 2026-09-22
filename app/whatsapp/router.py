"""WhatsApp Cloud API webhook. Fast path only — agent work is in the worker."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.merchants.service import get_merchant_by_whatsapp_phone_number_id
from app.whatsapp.service import (
    enqueue_inbound_text,
    extract_text_messages,
    signature_verification_enabled,
    verify_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    if (
        hub_mode == "subscribe"
        and hub_verify_token
        and hub_verify_token == settings.whatsapp_webhook_verify_token
        and hub_challenge is not None
    ):
        return PlainTextResponse(content=hub_challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    raw = await request.body()
    if signature_verification_enabled():
        header = request.headers.get("x-hub-signature-256")
        if not verify_signature(raw, header):
            raise HTTPException(status_code=403, detail="Invalid signature")

    if not raw:
        return Response(status_code=200)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("WhatsApp webhook body was not valid JSON")
        return Response(status_code=200)

    if not isinstance(payload, dict):
        return Response(status_code=200)

    for item in extract_text_messages(payload):
        merchant = await get_merchant_by_whatsapp_phone_number_id(
            db, item["phone_number_id"]
        )
        if merchant is None:
            logger.warning(
                "Dropping WhatsApp message %s: no merchant for phone_number_id=%s",
                item["message_id"],
                item["phone_number_id"],
            )
            continue
        enqueue_inbound_text(
            item["message_id"],
            item["customer_phone"],
            item["message_text"],
            item["phone_number_id"],
        )
    return Response(status_code=200)
