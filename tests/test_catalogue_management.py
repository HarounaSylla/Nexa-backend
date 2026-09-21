import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.catalogue.models import Merchant, Product, ProductImage
from app.catalogue.service import MAX_PHOTO_BYTES
from app.core.db import AsyncSessionLocal
from app.main import app
from app.orders.models import Order, OrderItem, OrderStatus, PaymentMethod

_MINI_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)
_FAKE_EMBEDDING = [0.01] * 1024


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
        product_ids = list(
            (
                await db.execute(
                    select(Product.id).where(Product.merchant_id.in_(merchant_ids))
                )
            ).scalars().all()
        )
        order_ids = list(
            (
                await db.execute(
                    select(Order.id).where(Order.merchant_id.in_(merchant_ids))
                )
            ).scalars().all()
        )
        if order_ids:
            await db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            await db.execute(delete(Order).where(Order.id.in_(order_ids)))
        if product_ids:
            await db.execute(
                delete(ProductImage).where(ProductImage.product_id.in_(product_ids))
            )
            await db.execute(delete(Product).where(Product.id.in_(product_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


async def _seed_merchant(name: str, clerk_user_id: str) -> Merchant:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=name, clerk_user_id=clerk_user_id)
        db.add(merchant)
        await db.commit()
        await db.refresh(merchant)
        return merchant


@pytest.mark.asyncio
async def test_product_crud_round_trip_is_merchant_scoped() -> None:
    owner_clerk = f"user_cat_owner_{uuid.uuid4()}"
    other_clerk = f"user_cat_other_{uuid.uuid4()}"
    owner = await _seed_merchant(f"pytest-cat-owner-{uuid.uuid4()}", owner_clerk)
    other = await _seed_merchant(f"pytest-cat-other-{uuid.uuid4()}", other_clerk)
    created_id = None
    other_product_id = None
    try:
        async with AsyncSessionLocal() as db:
            other_product = Product(
                merchant_id=other.id,
                name="Secret other shop item",
                description="should stay invisible",
                category="hidden",
                price=Decimal("999.00"),
                stock_qty=4,
            )
            db.add(other_product)
            await db.commit()
            other_product_id = other_product.id

        with _auth(owner_clerk), patch(
            "app.catalogue.service.embed_documents",
            return_value=[_FAKE_EMBEDDING],
        ) as mocked_embed:
            async with await _client() as client:
                created = await client.post(
                    "/catalogue/products",
                    headers=_headers(),
                    json={
                        "name": "Foulard test",
                        "description": "Soie indigo",
                        "price": "4500.00",
                        "category": "accessoires",
                        "stock_qty": 7,
                    },
                )
                assert created.status_code == 200, created.text
                body = created.json()
                created_id = uuid.UUID(body["id"])
                assert body["name"] == "Foulard test"
                assert body["stock_qty"] == 7
                assert body["image_url"] is None
                mocked_embed.assert_called_once()

                listed = await client.get("/catalogue/products", headers=_headers())
                assert listed.status_code == 200
                names = [item["name"] for item in listed.json()]
                assert names == ["Foulard test"]
                assert all(item["id"] != str(other_product_id) for item in listed.json())

                patched = await client.patch(
                    f"/catalogue/products/{created_id}",
                    headers=_headers(),
                    json={"name": "Foulard test soie", "stock_qty": 3},
                )
                assert patched.status_code == 200, patched.text
                assert patched.json()["name"] == "Foulard test soie"
                assert patched.json()["stock_qty"] == 3
                assert mocked_embed.call_count == 2

                missing = await client.patch(
                    f"/catalogue/products/{other_product_id}",
                    headers=_headers(),
                    json={"name": "hack"},
                )
                assert missing.status_code == 404
                assert "not found" in missing.json()["detail"].lower()

                deleted_other = await client.delete(
                    f"/catalogue/products/{other_product_id}",
                    headers=_headers(),
                )
                assert deleted_other.status_code == 404

                deleted = await client.delete(
                    f"/catalogue/products/{created_id}",
                    headers=_headers(),
                )
                assert deleted.status_code == 204
                after = await client.get("/catalogue/products", headers=_headers())
                assert after.json() == []

        async with AsyncSessionLocal() as db:
            leftover = await db.get(Product, other_product_id)
            assert leftover is not None
            assert leftover.name == "Secret other shop item"
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_delete_blocked_when_order_items_exist() -> None:
    clerk_user_id = f"user_cat_del_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-cat-del-{uuid.uuid4()}", clerk_user_id)
    try:
        async with AsyncSessionLocal() as db:
            with_history = Product(
                merchant_id=merchant.id,
                name="Sold once",
                category="tests",
                price=Decimal("1000.00"),
                stock_qty=2,
            )
            unused = Product(
                merchant_id=merchant.id,
                name="Never sold",
                category="tests",
                price=Decimal("2000.00"),
                stock_qty=5,
            )
            db.add_all([with_history, unused])
            await db.flush()
            order = Order(
                merchant_id=merchant.id,
                customer_phone="+221770000001",
                status=OrderStatus.created,
                payment_method=PaymentMethod.cash_on_delivery,
                delivery_address="Dakar",
            )
            db.add(order)
            await db.flush()
            db.add(
                OrderItem(
                    order_id=order.id,
                    product_id=with_history.id,
                    quantity=1,
                    unit_price=Decimal("1000.00"),
                )
            )
            await db.commit()
            blocked_id = with_history.id
            allowed_id = unused.id

        with _auth(clerk_user_id):
            async with await _client() as client:
                blocked = await client.delete(
                    f"/catalogue/products/{blocked_id}",
                    headers=_headers(),
                )
                assert blocked.status_code == 409, blocked.text
                assert "stock to 0" in blocked.json()["detail"]

                allowed = await client.delete(
                    f"/catalogue/products/{allowed_id}",
                    headers=_headers(),
                )
                assert allowed.status_code == 204
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_photo_upload_validates_and_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.catalogue.service.PRODUCT_IMAGES_DIR", tmp_path
    )
    clerk_user_id = f"user_cat_photo_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-cat-photo-{uuid.uuid4()}", clerk_user_id)
    try:
        async with AsyncSessionLocal() as db:
            product = Product(
                merchant_id=merchant.id,
                name="Photo item",
                category="tests",
                price=Decimal("1000.00"),
                stock_qty=1,
            )
            db.add(product)
            await db.commit()
            product_id = product.id

        with _auth(clerk_user_id):
            async with await _client() as client:
                rejected_type = await client.post(
                    f"/catalogue/products/{product_id}/photo",
                    headers=_headers(),
                    files={"file": ("note.txt", b"hello", "text/plain")},
                )
                assert rejected_type.status_code == 400
                assert "JPEG, PNG, or WebP" in rejected_type.json()["detail"]

                rejected_size = await client.post(
                    f"/catalogue/products/{product_id}/photo",
                    headers=_headers(),
                    files={
                        "file": (
                            "huge.jpg",
                            b"x" * (MAX_PHOTO_BYTES + 1),
                            "image/jpeg",
                        )
                    },
                )
                assert rejected_size.status_code == 400
                assert "5 MB" in rejected_size.json()["detail"]

                first = await client.post(
                    f"/catalogue/products/{product_id}/photo",
                    headers=_headers(),
                    files={"file": ("shot.png", _MINI_PNG, "image/png")},
                )
                assert first.status_code == 200, first.text
                first_url = first.json()["image_url"]
                assert first_url == f"/static/product_images/{product_id}.png"
                assert (tmp_path / f"{product_id}.png").is_file()

                second = await client.post(
                    f"/catalogue/products/{product_id}/photo",
                    headers=_headers(),
                    files={"file": ("shot.jpg", _MINI_PNG, "image/jpeg")},
                )
                assert second.status_code == 200, second.text
                assert second.json()["image_url"] == (
                    f"/static/product_images/{product_id}.jpg"
                )
                assert (tmp_path / f"{product_id}.jpg").is_file()
                assert not (tmp_path / f"{product_id}.png").exists()

        async with AsyncSessionLocal() as db:
            count = await db.scalar(
                select(func.count()).select_from(ProductImage).where(
                    ProductImage.product_id == product_id
                )
            )
            assert count == 1
    finally:
        await _cleanup(merchant.id)


@pytest.mark.asyncio
async def test_category_rename_is_merchant_scoped() -> None:
    owner_clerk = f"user_cat_ren_{uuid.uuid4()}"
    other_clerk = f"user_cat_ren_o_{uuid.uuid4()}"
    owner = await _seed_merchant(f"pytest-cat-ren-{uuid.uuid4()}", owner_clerk)
    other = await _seed_merchant(f"pytest-cat-ren-o-{uuid.uuid4()}", other_clerk)
    try:
        async with AsyncSessionLocal() as db:
            first = Product(
                merchant_id=owner.id,
                name="A",
                category="vêtements femme",
                price=Decimal("1000.00"),
                stock_qty=1,
            )
            second = Product(
                merchant_id=owner.id,
                name="B",
                category="vêtements femme",
                price=Decimal("2000.00"),
                stock_qty=0,
            )
            untouched = Product(
                merchant_id=owner.id,
                name="C",
                category="cosmétiques",
                price=Decimal("3000.00"),
                stock_qty=4,
            )
            leak = Product(
                merchant_id=other.id,
                name="D",
                category="vêtements femme",
                price=Decimal("4000.00"),
                stock_qty=8,
            )
            db.add_all([first, second, untouched, leak])
            await db.commit()

        with _auth(owner_clerk):
            async with await _client() as client:
                missing = await client.patch(
                    f"/catalogue/categories/{quote('does-not-exist', safe='')}",
                    headers=_headers(),
                    json={"new_name": "nope"},
                )
                assert missing.status_code == 404

                renamed = await client.patch(
                    f"/catalogue/categories/{quote('vêtements femme', safe='')}",
                    headers=_headers(),
                    json={"new_name": "mode femme"},
                )
                assert renamed.status_code == 200, renamed.text
                assert renamed.json() == {
                    "category": "mode femme",
                    "product_count": 2,
                }

                listed = await client.get("/catalogue/products", headers=_headers())
                by_name = {item["name"]: item["category"] for item in listed.json()}
                assert by_name["A"] == "mode femme"
                assert by_name["B"] == "mode femme"
                assert by_name["C"] == "cosmétiques"

                categories = await client.get(
                    "/catalogue/categories", headers=_headers()
                )
                assert categories.status_code == 200
                assert {row["category"] for row in categories.json()} == {
                    "mode femme",
                    "cosmétiques",
                }

        async with AsyncSessionLocal() as db:
            other_row = (
                await db.execute(
                    select(Product.category).where(Product.merchant_id == other.id)
                )
            ).scalar_one()
            assert other_row == "vêtements femme"
    finally:
        await _cleanup(owner.id, other.id)


@pytest.mark.asyncio
async def test_create_and_edit_trigger_reembedding() -> None:
    clerk_user_id = f"user_cat_embed_{uuid.uuid4()}"
    merchant = await _seed_merchant(f"pytest-cat-embed-{uuid.uuid4()}", clerk_user_id)
    try:
        with _auth(clerk_user_id), patch(
            "app.catalogue.service.embed_documents",
            return_value=[_FAKE_EMBEDDING],
        ) as mocked:
            async with await _client() as client:
                created = await client.post(
                    "/catalogue/products",
                    headers=_headers(),
                    json={
                        "name": "Embed me",
                        "description": "first",
                        "price": "1000.00",
                        "category": "tests",
                        "stock_qty": 1,
                    },
                )
                assert created.status_code == 200, created.text
                product_id = created.json()["id"]
                assert mocked.call_count == 1

                stock_only = await client.patch(
                    f"/catalogue/products/{product_id}",
                    headers=_headers(),
                    json={"stock_qty": 9},
                )
                assert stock_only.status_code == 200
                assert mocked.call_count == 1

                renamed = await client.patch(
                    f"/catalogue/products/{product_id}",
                    headers=_headers(),
                    json={"description": "second"},
                )
                assert renamed.status_code == 200
                assert mocked.call_count == 2

        async with AsyncSessionLocal() as db:
            stored = await db.get(Product, uuid.UUID(product_id))
            assert stored is not None
            assert stored.embedding is not None
            assert stored.stock_qty == 9
            assert stored.description == "second"
    finally:
        await _cleanup(merchant.id)
