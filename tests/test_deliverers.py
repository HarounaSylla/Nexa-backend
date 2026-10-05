"""Deliverer create/edit, canonical phone, uniqueness per merchant."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import delete, select, text

from app.catalogue.models import Merchant, Product
from app.core.db import AsyncSessionLocal
from app.main import app
from app.merchants.models import MerchantPaymentLink
from app.notifications.models import Notification
from app.orders.models import (
    Deliverer,
    DeliveryZone,
    Order,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    StockMovement,
)
from app.orders.service import creer_commande, normalize_city

DUPLICATE_DETAIL = "A deliverer with this phone number already exists"


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


def _load_0020():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0020_deliverer_unique_phone.py"
    )
    spec = importlib.util.spec_from_file_location("rev_0020_deliverer_phone", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            delete(Notification).where(Notification.merchant_id.in_(merchant_ids))
        )
        order_ids = list(
            (
                await db.execute(
                    select(Order.id).where(Order.merchant_id.in_(merchant_ids))
                )
            )
            .scalars()
            .all()
        )
        if order_ids:
            await db.execute(
                delete(StockMovement).where(StockMovement.order_id.in_(order_ids))
            )
            await db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            await db.execute(delete(Order).where(Order.id.in_(order_ids)))
        await db.execute(
            delete(Deliverer).where(Deliverer.merchant_id.in_(merchant_ids))
        )
        await db.execute(
            delete(DeliveryZone).where(DeliveryZone.merchant_id.in_(merchant_ids))
        )
        await db.execute(
            delete(MerchantPaymentLink).where(
                MerchantPaymentLink.merchant_id.in_(merchant_ids)
            )
        )
        await db.execute(delete(Product).where(Product.merchant_id.in_(merchant_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_shop(*, name: str, clerk_user_id: str) -> tuple[Merchant, Product]:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name, clerk_user_id=clerk_user_id)
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Article dashboard",
            price=Decimal("1000.00"),
            stock_qty=10,
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
        return merchant, product


@pytest.mark.asyncio
async def test_create_normalises_local_phone_and_trims_name() -> None:
    clerk = f"user_del_norm_{uuid.uuid4()}"
    merchant, _ = await _seed_shop(
        name=f"pytest-del-norm-{uuid.uuid4()}", clerk_user_id=clerk
    )
    try:
        with _auth(clerk):
            async with await _client() as client:
                created = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "  Moussa  ", "phone": "77 123 45 67"},
                )
                assert created.status_code == 200, created.text
                body = created.json()
                assert body["name"] == "Moussa"
                assert body["phone"] == "+221771234567"

                listed = await client.get("/deliverers", headers=_headers())
                assert [row["phone"] for row in listed.json()] == ["+221771234567"]
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_create_rejects_invalid_phone_and_name() -> None:
    clerk = f"user_del_inv_{uuid.uuid4()}"
    merchant, _ = await _seed_shop(
        name=f"pytest-del-inv-{uuid.uuid4()}", clerk_user_id=clerk
    )
    try:
        with _auth(clerk):
            async with await _client() as client:
                invalid = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Moussa", "phone": "not-a-phone"},
                )
                assert invalid.status_code == 422
                assert "Invalid phone number" in invalid.text

                empty = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "   ", "phone": "+221771234567"},
                )
                assert empty.status_code == 422

                oversized = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "x" * 61, "phone": "+221771234567"},
                )
                assert oversized.status_code == 422
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_duplicate_phone_spellings_conflict_other_merchant_allowed() -> None:
    owner_clerk = f"user_del_dup_a_{uuid.uuid4()}"
    other_clerk = f"user_del_dup_b_{uuid.uuid4()}"
    owner, _ = await _seed_shop(
        name=f"pytest-del-dup-a-{uuid.uuid4()}", clerk_user_id=owner_clerk
    )
    other, _ = await _seed_shop(
        name=f"pytest-del-dup-b-{uuid.uuid4()}", clerk_user_id=other_clerk
    )
    try:
        with _auth(owner_clerk):
            async with await _client() as client:
                first = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "A", "phone": "+221771234567"},
                )
                assert first.status_code == 200, first.text
                for spelling in ("221771234567", "771234567", "+221 77 123 45 67"):
                    dup = await client.post(
                        "/deliverers",
                        headers=_headers(),
                        json={"name": "B", "phone": spelling},
                    )
                    assert dup.status_code == 409, spelling
                    assert dup.json()["detail"] == DUPLICATE_DETAIL

        with _auth(other_clerk):
            async with await _client() as client:
                other_ok = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "C", "phone": "771234567"},
                )
                assert other_ok.status_code == 200, other_ok.text
                assert other_ok.json()["phone"] == "+221771234567"
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_put_rename_change_phone_and_conflicts() -> None:
    clerk = f"user_del_put_{uuid.uuid4()}"
    other_clerk = f"user_del_put_o_{uuid.uuid4()}"
    merchant, _ = await _seed_shop(
        name=f"pytest-del-put-{uuid.uuid4()}", clerk_user_id=clerk
    )
    other, _ = await _seed_shop(
        name=f"pytest-del-put-o-{uuid.uuid4()}", clerk_user_id=other_clerk
    )
    try:
        with _auth(clerk):
            async with await _client() as client:
                a = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Alpha", "phone": "+221771111111"},
                )
                b = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Beta", "phone": "+221772222222"},
                )
                assert a.status_code == 200 and b.status_code == 200
                a_id = a.json()["id"]
                b_id = b.json()["id"]

                renamed = await client.put(
                    f"/deliverers/{a_id}",
                    headers=_headers(),
                    json={"name": "Alpha Prime", "phone": "+221771111111"},
                )
                assert renamed.status_code == 200
                assert renamed.json()["name"] == "Alpha Prime"
                assert renamed.json()["phone"] == "+221771111111"

                changed = await client.put(
                    f"/deliverers/{a_id}",
                    headers=_headers(),
                    json={"name": "Alpha Prime", "phone": "773333333"},
                )
                assert changed.status_code == 200
                assert changed.json()["phone"] == "+221773333333"

                clash = await client.put(
                    f"/deliverers/{a_id}",
                    headers=_headers(),
                    json={"name": "Alpha Prime", "phone": "772222222"},
                )
                assert clash.status_code == 409
                assert clash.json()["detail"] == DUPLICATE_DETAIL

                invalid = await client.put(
                    f"/deliverers/{a_id}",
                    headers=_headers(),
                    json={"name": "Alpha Prime", "phone": "xx"},
                )
                assert invalid.status_code == 422
                assert "Invalid phone number" in invalid.text

        with _auth(other_clerk):
            async with await _client() as client:
                hidden = await client.put(
                    f"/deliverers/{a_id}",
                    headers=_headers(),
                    json={"name": "Nope", "phone": "+221774444444"},
                )
                assert hidden.status_code == 404
                assert hidden.json()["detail"] == "Deliverer was not found"
    finally:
        await _cleanup(merchant.id, other.id)


@pytest.mark.asyncio
async def test_order_detail_shows_updated_deliverer() -> None:
    clerk = f"user_del_ord_{uuid.uuid4()}"
    merchant, product = await _seed_shop(
        name=f"pytest-del-ord-{uuid.uuid4()}", clerk_user_id=clerk
    )
    try:
        async with AsyncSessionLocal() as db:
            order = await creer_commande(
                db,
                merchant_id=merchant.id,
                customer_phone="+221770000010",
                items=[(product.id, 1)],
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address="Sacré-Cœur, Dakar",
                ville="Dakar",
            )
        with _auth(clerk):
            async with await _client() as client:
                created = await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Moussa", "phone": "+221775555555"},
                )
                deliverer_id = created.json()["id"]
                assigned = await client.post(
                    f"/orders/{order.id}/assign-deliverer",
                    headers=_headers(),
                    json={"deliverer_id": deliverer_id},
                )
                assert assigned.status_code == 200
                edited = await client.put(
                    f"/deliverers/{deliverer_id}",
                    headers=_headers(),
                    json={"name": "Moussa Diop", "phone": "776666666"},
                )
                assert edited.status_code == 200
                detail = await client.get(f"/orders/{order.id}", headers=_headers())
                assert detail.status_code == 200
                deliverer = detail.json()["deliverer"]
                assert deliverer["name"] == "Moussa Diop"
                assert deliverer["phone"] == "+221776666666"
                assert deliverer["id"] == deliverer_id
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_list_sorted_case_insensitive_then_id() -> None:
    clerk = f"user_del_sort_{uuid.uuid4()}"
    merchant, _ = await _seed_shop(
        name=f"pytest-del-sort-{uuid.uuid4()}", clerk_user_id=clerk
    )
    try:
        with _auth(clerk):
            async with await _client() as client:
                await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "beta", "phone": "+221771010101"},
                )
                await client.post(
                    "/deliverers",
                    headers=_headers(),
                    json={"name": "Alpha", "phone": "+221771010102"},
                )
                listed = await client.get("/deliverers", headers=_headers())
                names = [row["name"] for row in listed.json()]
                assert names == ["Alpha", "beta"]
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_concurrent_create_same_phone_one_row_other_409() -> None:
    clerk = f"user_del_race_{uuid.uuid4()}"
    merchant, _ = await _seed_shop(
        name=f"pytest-del-race-{uuid.uuid4()}", clerk_user_id=clerk
    )
    try:
        async def _post(phone: str) -> httpx.Response:
            with _auth(clerk):
                async with await _client() as client:
                    return await client.post(
                        "/deliverers",
                        headers=_headers(),
                        json={"name": "Racer", "phone": phone},
                    )

        first, second = await asyncio.gather(
            _post("77 123 45 67"),
            _post("+221771234567"),
        )
        statuses = sorted([first.status_code, second.status_code])
        assert statuses == [200, 409]
        loser = first if first.status_code == 409 else second
        assert loser.json()["detail"] == DUPLICATE_DETAIL
        async with AsyncSessionLocal() as db:
            rows = list(
                (
                    await db.execute(
                        select(Deliverer).where(Deliverer.merchant_id == merchant.id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].phone == "+221771234567"
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_migration_0020_normalises_legacy_phone() -> None:
    module = _load_0020()
    clerk = f"user_del_mig_{uuid.uuid4()}"
    merchant, _ = await _seed_shop(
        name=f"pytest-del-mig-{uuid.uuid4()}", clerk_user_id=clerk
    )
    try:
        async with AsyncSessionLocal() as db:
            deliverer = Deliverer(
                merchant_id=merchant.id,
                name="Legacy",
                phone="77 888 99 00",
            )
            db.add(deliverer)
            await db.commit()
            deliverer_id = deliverer.id
            await db.execute(text(module.NORMALIZE_DELIVERER_PHONES_SQL))
            await db.commit()
            db.expire_all()
            reloaded = await db.get(Deliverer, deliverer_id)
            assert reloaded is not None
            assert reloaded.phone == "+221778889900"
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_migration_0020_fails_clearly_on_duplicates() -> None:
    module = _load_0020()
    clerk = f"user_del_migdup_{uuid.uuid4()}"
    merchant, _ = await _seed_shop(
        name=f"pytest-del-migdup-{uuid.uuid4()}", clerk_user_id=clerk
    )
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text(f"DROP INDEX IF EXISTS {module.INDEX_NAME}"))
            await db.commit()
            db.add_all(
                [
                    Deliverer(
                        merchant_id=merchant.id,
                        name="One",
                        phone="771234567",
                    ),
                    Deliverer(
                        merchant_id=merchant.id,
                        name="Two",
                        phone="+221771234567",
                    ),
                ]
            )
            await db.commit()
            await db.execute(text(module.NORMALIZE_DELIVERER_PHONES_SQL))
            await db.commit()
            rows = (await db.execute(text(module.DUPLICATES_SQL))).fetchall()
            with pytest.raises(RuntimeError, match="duplicate"):
                module.raise_if_duplicate_rows(rows)
            assert any("+221771234567" in str(row[1]) for row in rows)
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(Deliverer).where(Deliverer.merchant_id == merchant.id)
            )
            await db.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {module.INDEX_NAME} "
                    "ON deliverers (merchant_id, phone)"
                )
            )
            await db.commit()
        await _cleanup(merchant.id)
