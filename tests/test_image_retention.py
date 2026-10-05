"""Inbound image file retention (90 days after reception)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, select

from app.agent.models import Conversation, Message
from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.main import app
from app.notifications.models import Notification
from app.orders.models import (
    DeliveryZone,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    StockMovement,
)
from app.orders.service import creer_commande, normalize_city
from app.proofs.models import CLASSIFICATION_NOT_ANALYZED, InboundImage
from app.proofs.service import purger_images_expirees
from app.proofs.storage import media_root

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
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


async def _seed_shop(tmp_path: Path) -> tuple[Merchant, Product, str]:
    clerk = f"user_retention_{uuid.uuid4()}"
    async with AsyncSessionLocal() as db:
        merchant = Merchant(
            name=f"pytest-retention-{uuid.uuid4()}",
            clerk_user_id=clerk,
        )
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Article retention",
            description="tests retention",
            category="tests",
            price=Decimal("4000.00"),
            stock_qty=20,
        )
        db.add(product)
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
        await db.refresh(product)
    return merchant, product, clerk


async def _conversation(merchant_id: uuid.UUID, phone: str) -> Conversation:
    async with AsyncSessionLocal() as db:
        conversation = Conversation(
            merchant_id=merchant_id,
            customer_phone=phone,
            status="active",
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        return conversation


def _write_file(relative: str) -> Path:
    path = media_root() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(TINY_PNG)
    return path


async def _add_image(
    *,
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    relative: str,
    created_at: datetime,
    order_id: uuid.UUID | None = None,
) -> InboundImage:
    async with AsyncSessionLocal() as db:
        image = InboundImage(
            merchant_id=merchant_id,
            conversation_id=conversation_id,
            order_id=order_id,
            whatsapp_message_id=f"wamid.ret-{uuid.uuid4()}",
            media_path=relative,
            mime_type="image/png",
            classification=CLASSIFICATION_NOT_ANALYZED,
            created_at=created_at,
        )
        db.add(image)
        await db.commit()
        await db.refresh(image)
        return image


@pytest.mark.asyncio
async def test_purger_images_expirees_eligibility_and_idempotence(tmp_path: Path) -> None:
    merchant, product, clerk = await _seed_shop(tmp_path)
    now = datetime.now(timezone.utc)
    media = str(tmp_path / "media")
    try:
        with patch("app.core.config.settings.media_dir", media), patch(
            "app.proofs.storage.settings.media_dir", media
        ), patch(
            "app.core.config.settings.payment_proof_retention_days", 90
        ):
            conversation = await _conversation(merchant.id, "+221770080001")
            paid = await _place(merchant.id, product.id, "+221770080002")
            cancelled = await _place(merchant.id, product.id, "+221770080003")
            pending = await _place(merchant.id, product.id, "+221770080004")
            proof = await _place(merchant.id, product.id, "+221770080005")
            async with AsyncSessionLocal() as db:
                paid_row = await db.get(Order, paid.id)
                cancelled_row = await db.get(Order, cancelled.id)
                proof_row = await db.get(Order, proof.id)
                assert paid_row is not None and cancelled_row is not None
                assert proof_row is not None
                paid_row.payment_status = PaymentStatus.paid
                cancelled_row.status = OrderStatus.cancelled
                proof_row.payment_status = PaymentStatus.proof_received
                await db.commit()

            old = now - timedelta(days=91)
            recent = now - timedelta(days=89)
            cases = {
                "no_order_old": await _file_image(
                    merchant.id, conversation.id, old, None
                ),
                "no_order_recent": await _file_image(
                    merchant.id, conversation.id, recent, None
                ),
                "paid_old": await _file_image(
                    merchant.id, conversation.id, old, paid.id
                ),
                "cancelled_old": await _file_image(
                    merchant.id, conversation.id, old, cancelled.id
                ),
                "pending_old": await _file_image(
                    merchant.id, conversation.id, old, pending.id
                ),
                "proof_old": await _file_image(
                    merchant.id, conversation.id, old, proof.id
                ),
            }
            outside = tmp_path / "secret.bin"
            outside.write_bytes(b"secret")
            bad_relative = "../secret.bin"
            bad = await _add_image(
                merchant_id=merchant.id,
                conversation_id=conversation.id,
                relative=bad_relative,
                created_at=old,
            )
            missing_relative = f"{merchant.id}/{uuid.uuid4()}.png"
            missing = await _add_image(
                merchant_id=merchant.id,
                conversation_id=conversation.id,
                relative=missing_relative,
                created_at=old,
            )

            async with AsyncSessionLocal() as db:
                first = await purger_images_expirees(db, now=now)
            assert first == 4
            assert outside.is_file()

            async with AsyncSessionLocal() as db:
                db.expire_all()
                kept_pending = await db.get(InboundImage, cases["pending_old"].id)
                kept_proof = await db.get(InboundImage, cases["proof_old"].id)
                kept_recent = await db.get(InboundImage, cases["no_order_recent"].id)
                purged_none = await db.get(InboundImage, cases["no_order_old"].id)
                purged_paid = await db.get(InboundImage, cases["paid_old"].id)
                purged_cancelled = await db.get(InboundImage, cases["cancelled_old"].id)
                still_bad = await db.get(InboundImage, bad.id)
                still_missing = await db.get(InboundImage, missing.id)
            assert kept_pending is not None and kept_pending.media_deleted_at is None
            assert kept_proof is not None and kept_proof.media_deleted_at is None
            assert kept_recent is not None and kept_recent.media_deleted_at is None
            for row, original in (
                (purged_none, cases["no_order_old"]),
                (purged_paid, cases["paid_old"]),
                (purged_cancelled, cases["cancelled_old"]),
                (still_missing, missing),
            ):
                assert row is not None
                assert row.media_deleted_at is not None
                assert row.media_path is None
                assert not (media_root() / original.media_path).is_file()
            assert still_bad is not None
            assert still_bad.media_deleted_at is None
            assert still_bad.media_path == bad_relative

            async with AsyncSessionLocal() as db:
                second = await purger_images_expirees(db, now=now)
            assert second == 0

            with _auth(clerk):
                async with await _client() as client:
                    gone = await client.get(
                        f"/images/{cases['no_order_old'].id}", headers=_headers()
                    )
                    assert gone.status_code == 410
                    assert gone.json()["detail"] == "Image has been deleted"
                    alive = await client.get(
                        f"/images/{cases['pending_old'].id}", headers=_headers()
                    )
                    assert alive.status_code == 200
                    thread = await client.get(
                        f"/conversations/{conversation.id}/messages",
                        headers=_headers(),
                    )
                    assert thread.status_code == 200
                    order_detail = await client.get(
                        f"/orders/{pending.id}", headers=_headers()
                    )
                    assert order_detail.status_code == 200
            # Messages have no InboundImage.message_id here, so conversation
            # payload has no image objects. Attach one purged row to a message
            # for the deleted flag.
            async with AsyncSessionLocal() as db:
                message = Message(
                    conversation_id=conversation.id,
                    turn_role="customer",
                    display_text="Photo",
                    items=[],
                )
                db.add(message)
                await db.flush()
                purged = await db.get(InboundImage, cases["paid_old"].id)
                assert purged is not None
                purged.message_id = message.id
                pending_image = await db.get(InboundImage, cases["pending_old"].id)
                assert pending_image is not None
                pending_msg = Message(
                    conversation_id=conversation.id,
                    turn_role="customer",
                    display_text="Photo 2",
                    items=[],
                )
                db.add(pending_msg)
                await db.flush()
                pending_image.message_id = pending_msg.id
                await db.commit()
            with _auth(clerk):
                async with await _client() as client:
                    thread = await client.get(
                        f"/conversations/{conversation.id}/messages",
                        headers=_headers(),
                    )
                    images = [
                        row["image"]
                        for row in thread.json()
                        if row["image"] is not None
                    ]
                    by_id = {row["id"]: row["deleted"] for row in images}
                    assert by_id[str(cases["paid_old"].id)] is True
                    assert by_id[str(cases["pending_old"].id)] is False
                    pending_detail = await client.get(
                        f"/orders/{pending.id}", headers=_headers()
                    )
                    proofs = pending_detail.json()["proofs"]
                    assert proofs
                    assert proofs[0]["deleted"] is False
                    paid_detail = await client.get(
                        f"/orders/{paid.id}", headers=_headers()
                    )
                    paid_proofs = paid_detail.json()["proofs"]
                    assert paid_proofs
                    assert paid_proofs[0]["deleted"] is True
    finally:
        await _cleanup(merchant.id)


async def _place(
    merchant_id: uuid.UUID, product_id: uuid.UUID, phone: str
) -> Order:
    async with AsyncSessionLocal() as db:
        return await creer_commande(
            db,
            merchant_id=merchant_id,
            customer_phone=phone,
            items=[(product_id, 1)],
            payment_method=PaymentMethod.online,
            delivery_address="Sacré-Cœur, Dakar",
            ville="Dakar",
        )


async def _file_image(
    merchant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    created_at: datetime,
    order_id: uuid.UUID | None,
) -> InboundImage:
    relative = f"{merchant_id}/{uuid.uuid4()}.png"
    _write_file(relative)
    return await _add_image(
        merchant_id=merchant_id,
        conversation_id=conversation_id,
        relative=relative,
        created_at=created_at,
        order_id=order_id,
    )


@pytest.mark.asyncio
async def test_one_failing_purge_does_not_block_others(tmp_path: Path) -> None:
    merchant, _product, _clerk = await _seed_shop(tmp_path)
    now = datetime.now(timezone.utc)
    media = str(tmp_path / "media")
    try:
        with patch("app.core.config.settings.media_dir", media), patch(
            "app.proofs.storage.settings.media_dir", media
        ):
            conversation = await _conversation(merchant.id, "+221770080010")
            old = now - timedelta(days=91)
            good = await _file_image(merchant.id, conversation.id, old, None)
            await _add_image(
                merchant_id=merchant.id,
                conversation_id=conversation.id,
                relative="../secret.bin",
                created_at=old,
            )
            async with AsyncSessionLocal() as db:
                purged = await purger_images_expirees(db, now=now)
            assert purged == 1
            async with AsyncSessionLocal() as db:
                row = await db.get(InboundImage, good.id)
            assert row is not None
            assert row.media_deleted_at is not None
    finally:
        await _cleanup(merchant.id)
