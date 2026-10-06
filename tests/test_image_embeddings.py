"""Image embeddings, similarity search, classify helper, backfill.

Voyage is always stubbed — no network in these tests.
"""

from __future__ import annotations

import importlib.util
import io
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from PIL import Image
from sqlalchemy import delete, select

from app.catalogue.image_embeddings import (
    IMAGE_EMBEDDING_DIMENSION,
    ImageEmbeddingError,
    billable_pixel_count,
    embed_catalogue_image,
    embed_query_image,
    estimate_voyage_image_cost_usd,
    prepare_image_for_embedding,
)
from app.catalogue.models import Merchant, Product, ProductImage
from app.catalogue.service import (
    ImageMatch,
    classify_image_match,
    enregistrer_photo_produit,
    trouver_produits_par_image,
)
from app.core.config import settings
from app.core.db import AsyncSessionLocal

_FAKE_IMAGE_EMBEDDING = [0.02] * IMAGE_EMBEDDING_DIMENSION


def _vector(index: int) -> list[float]:
    values = [0.0] * IMAGE_EMBEDDING_DIMENSION
    values[index] = 1.0
    return values


def _png_bytes(size: tuple[int, int], color: tuple[int, ...] = (255, 0, 0, 255), *, mode: str = "RGB") -> bytes:
    image = Image.new(mode, size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_exif_orientation(size: tuple[int, int], orientation: int) -> bytes:
    image = Image.new("RGB", size, (200, 30, 30))
    exif = image.getexif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif)
    return buffer.getvalue()


def _load_backfill_module():
    path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "backfill_product_image_embeddings.py"
    )
    spec = importlib.util.spec_from_file_location(
        "backfill_product_image_embeddings", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _cleanup(*merchant_ids: uuid.UUID) -> None:
    async with AsyncSessionLocal() as db:
        product_ids = list(
            (
                await db.execute(
                    select(Product.id).where(Product.merchant_id.in_(merchant_ids))
                )
            ).scalars().all()
        )
        if product_ids:
            await db.execute(
                delete(ProductImage).where(ProductImage.product_id.in_(product_ids))
            )
            await db.execute(delete(Product).where(Product.id.in_(product_ids)))
        await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
        await db.commit()


def test_prepare_applies_exif_rotation() -> None:
    content = _jpeg_with_exif_orientation((40, 20), orientation=6)
    prepared = prepare_image_for_embedding(content)
    assert prepared.size == (20, 40)
    assert prepared.mode == "RGB"


def test_prepare_flattens_rgba_on_white() -> None:
    content = _png_bytes((8, 8), (0, 0, 255, 0), mode="RGBA")
    prepared = prepare_image_for_embedding(content)
    assert prepared.mode == "RGB"
    assert prepared.size == (8, 8)
    assert prepared.getpixel((0, 0)) == (255, 255, 255)


def test_prepare_downscales_longest_side() -> None:
    content = _png_bytes((2048, 1024), (10, 20, 30), mode="RGB")
    prepared = prepare_image_for_embedding(content)
    assert prepared.size == (1024, 512)
    assert max(prepared.size) == 1024


def test_prepare_rejects_garbage_and_empty() -> None:
    with pytest.raises(ImageEmbeddingError, match="could not be decoded"):
        prepare_image_for_embedding(b"not-an-image")
    with pytest.raises(ImageEmbeddingError, match="empty"):
        prepare_image_for_embedding(b"")


@pytest.mark.asyncio
async def test_embed_uses_document_vs_query_input_type() -> None:
    content = _png_bytes((16, 16), (1, 2, 3), mode="RGB")
    fake = type("Result", (), {"embeddings": [_FAKE_IMAGE_EMBEDDING]})()
    with patch(
        "app.catalogue.image_embeddings._get_client"
    ) as get_client:
        client = get_client.return_value
        client.multimodal_embed.return_value = fake
        await embed_catalogue_image(content, "image/png")
        assert client.multimodal_embed.call_args.kwargs["input_type"] == "document"
        await embed_query_image(content, "image/png")
        assert client.multimodal_embed.call_args.kwargs["input_type"] == "query"
        assert client.multimodal_embed.call_args.kwargs["output_dimension"] == 1024


@pytest.mark.asyncio
async def test_photo_upload_sets_image_embedding_or_leaves_null_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.catalogue.service.PRODUCT_IMAGES_DIR", tmp_path)
    merchant_id = None
    try:
        async with AsyncSessionLocal() as db:
            merchant = Merchant(name=f"pytest-img-up-{uuid.uuid4()}")
            db.add(merchant)
            await db.flush()
            product = Product(
                merchant_id=merchant.id,
                name="Photo embed",
                category="tests",
                price=Decimal("1000.00"),
                stock_qty=2,
            )
            db.add(product)
            await db.commit()
            merchant_id = merchant.id
            product_id = product.id

        async with AsyncSessionLocal() as db:
            with patch(
                "app.catalogue.service.embed_catalogue_image",
                new_callable=AsyncMock,
                return_value=_FAKE_IMAGE_EMBEDDING,
            ):
                image = await enregistrer_photo_produit(
                    db, merchant_id, product_id, _png_bytes((12, 12)), "image/png"
                )
            assert image.url.endswith(".png")
            stored = await db.get(Product, product_id)
            assert stored is not None
            assert list(stored.image_embedding) == pytest.approx(_FAKE_IMAGE_EMBEDDING)
            assert stored.image_embedding_model == settings.voyage_image_model

        async with AsyncSessionLocal() as db:
            with patch(
                "app.catalogue.service.embed_catalogue_image",
                new_callable=AsyncMock,
                side_effect=ImageEmbeddingError("stub failure"),
            ):
                await enregistrer_photo_produit(
                    db,
                    merchant_id,
                    product_id,
                    _png_bytes((14, 14), (0, 128, 0)),
                    "image/jpeg",
                )
            after_fail = await db.get(Product, product_id)
            assert after_fail is not None
            assert after_fail.image_embedding is None
            assert after_fail.image_embedding_model is None
            row = (
                await db.execute(
                    select(ProductImage).where(ProductImage.product_id == product_id)
                )
            ).scalar_one()
            assert row.url.endswith(".jpg")

        refill = [0.03] * IMAGE_EMBEDDING_DIMENSION
        async with AsyncSessionLocal() as db:
            with patch(
                "app.catalogue.service.embed_catalogue_image",
                new_callable=AsyncMock,
                return_value=refill,
            ):
                await enregistrer_photo_produit(
                    db,
                    merchant_id,
                    product_id,
                    _png_bytes((10, 10), (9, 9, 9)),
                    "image/png",
                )
            refilled = await db.get(Product, product_id)
            assert refilled is not None
            assert list(refilled.image_embedding) == pytest.approx(refill)
            assert refilled.image_embedding_model == settings.voyage_image_model
    finally:
        if merchant_id is not None:
            await _cleanup(merchant_id)


@pytest.mark.asyncio
async def test_trouver_produits_par_image_orders_isolates_and_includes_oos() -> None:
    query = _vector(0)
    near = [0.99] + [0.0] * (IMAGE_EMBEDDING_DIMENSION - 1)
    far = _vector(5)
    merchant_ids: list[uuid.UUID] = []
    try:
        async with AsyncSessionLocal() as db:
            owner = Merchant(name=f"pytest-img-s-{uuid.uuid4()}")
            other = Merchant(name=f"pytest-img-o-{uuid.uuid4()}")
            db.add_all([owner, other])
            await db.flush()
            merchant_ids = [owner.id, other.id]
            close = Product(
                merchant_id=owner.id,
                name="close in stock",
                category="tests",
                price=Decimal("1.00"),
                stock_qty=4,
                image_embedding=query,
                image_embedding_model=settings.voyage_image_model,
            )
            oos = Product(
                merchant_id=owner.id,
                name="close out of stock",
                category="tests",
                price=Decimal("2.00"),
                stock_qty=0,
                image_embedding=near,
                image_embedding_model=settings.voyage_image_model,
            )
            distant = Product(
                merchant_id=owner.id,
                name="far",
                category="tests",
                price=Decimal("3.00"),
                stock_qty=1,
                image_embedding=far,
                image_embedding_model=settings.voyage_image_model,
            )
            missing = Product(
                merchant_id=owner.id,
                name="no vector",
                category="tests",
                price=Decimal("4.00"),
                stock_qty=1,
            )
            other_model = Product(
                merchant_id=owner.id,
                name="other model",
                category="tests",
                price=Decimal("5.00"),
                stock_qty=1,
                image_embedding=query,
                image_embedding_model="voyage-multimodal-3",
            )
            leak = Product(
                merchant_id=other.id,
                name="other merchant",
                category="tests",
                price=Decimal("6.00"),
                stock_qty=9,
                image_embedding=query,
                image_embedding_model=settings.voyage_image_model,
            )
            db.add_all([close, oos, distant, missing, other_model, leak])
            await db.commit()
            owner_id = owner.id

        async with AsyncSessionLocal() as db:
            with patch(
                "app.catalogue.service.embed_query_image",
                new_callable=AsyncMock,
                return_value=query,
            ):
                matches = await trouver_produits_par_image(
                    db, owner_id, b"unused", "image/jpeg", limit=5
                )
        names = [item.product.name for item in matches]
        assert "other merchant" not in names
        assert "no vector" not in names
        assert "other model" not in names
        assert names[0] == "close in stock"
        assert names[1] == "close out of stock"
        assert matches[0].distance == pytest.approx(0.0, abs=1e-6)
        assert matches[0].in_stock is True
        assert matches[1].in_stock is False
        assert matches[1].distance > matches[0].distance
    finally:
        if merchant_ids:
            await _cleanup(*merchant_ids)


def test_classify_image_match_thresholds() -> None:
    def _m(*distances: float) -> list[ImageMatch]:
        dummy = Product(name="x")
        return [
            ImageMatch(product=dummy, distance=value, in_stock=True)
            for value in distances
        ]

    assert classify_image_match([]) == "none"
    assert classify_image_match(_m(0.10)) == "strong"
    assert classify_image_match(_m(0.10, 0.30)) == "strong"
    assert classify_image_match(_m(0.10, 0.12)) == "possible"
    assert classify_image_match(_m(0.30)) == "possible"
    assert classify_image_match(_m(0.60)) == "none"


def test_voyage_cost_estimate_uses_floor_and_ceiling() -> None:
    tiny = billable_pixel_count(10, 10)
    huge = billable_pixel_count(2000, 2000)
    assert tiny == 50_000
    assert huge == 2_000_000
    cost = estimate_voyage_image_cost_usd([tiny, huge])
    assert cost == pytest.approx(0.00003 + 0.0012)


@pytest.mark.asyncio
async def test_backfill_dry_run_noop_and_continues_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.catalogue.service.PRODUCT_IMAGES_DIR", tmp_path)
    backfill = _load_backfill_module()
    merchant_id = None
    try:
        async with AsyncSessionLocal() as db:
            merchant = Merchant(name=f"pytest-img-bf-{uuid.uuid4()}")
            db.add(merchant)
            await db.flush()
            merchant_id = merchant.id
            ok_product = Product(
                merchant_id=merchant.id,
                name="ok photo",
                category="tests",
                price=Decimal("1.00"),
                stock_qty=1,
            )
            fail_product = Product(
                merchant_id=merchant.id,
                name="fail photo",
                category="tests",
                price=Decimal("2.00"),
                stock_qty=1,
            )
            skip_product = Product(
                merchant_id=merchant.id,
                name="already embedded",
                category="tests",
                price=Decimal("3.00"),
                stock_qty=1,
                image_embedding=_FAKE_IMAGE_EMBEDDING,
                image_embedding_model=settings.voyage_image_model,
            )
            db.add_all([ok_product, fail_product, skip_product])
            await db.flush()
            for product in (ok_product, fail_product, skip_product):
                path = tmp_path / f"{product.id}.png"
                path.write_bytes(_png_bytes((32, 16), (40, 50, 60)))
                db.add(
                    ProductImage(
                        product_id=product.id,
                        url=f"/static/product_images/{product.id}.png",
                    )
                )
            await db.commit()
            ok_id = ok_product.id
            fail_id = fail_product.id
            skip_id = skip_product.id

        async with AsyncSessionLocal() as db:
            with patch.object(
                backfill, "embed_catalogue_image", new_callable=AsyncMock
            ) as embed:
                summary = await backfill.backfill_product_image_embeddings(
                    db, merchant_id, dry_run=True, force=False
                )
                embed.assert_not_called()
            skip_row = await db.get(Product, skip_id)
            ok_row = await db.get(Product, ok_id)
            assert skip_row is not None
            assert ok_row is not None
            assert list(skip_row.image_embedding) == pytest.approx(
                _FAKE_IMAGE_EMBEDDING
            )
            assert ok_row.image_embedding is None
            assert fail_id not in summary["ok"]

        async def ordered_embed(content: bytes, mime: str) -> list[float]:
            del content, mime
            ordered_embed.n += 1  # type: ignore[attr-defined]
            # Targets ordered by Product.name: "fail photo" then "ok photo".
            if ordered_embed.n == 1:  # type: ignore[attr-defined]
                raise ImageEmbeddingError("stub")
            return _vector(3)

        ordered_embed.n = 0  # type: ignore[attr-defined]
        async with AsyncSessionLocal() as db:
            with patch.object(
                backfill, "embed_catalogue_image", side_effect=ordered_embed
            ):
                first = await backfill.backfill_product_image_embeddings(
                    db, merchant_id, dry_run=False, force=False
                )
            assert fail_id in first["failed"]
            assert ok_id in first["ok"]
            assert skip_id in first["skipped"]
            stored_ok = await db.get(Product, ok_id)
            stored_fail = await db.get(Product, fail_id)
            stored_skip = await db.get(Product, skip_id)
            assert stored_ok is not None
            assert stored_fail is not None
            assert stored_skip is not None
            assert list(stored_ok.image_embedding) == pytest.approx(_vector(3))
            assert stored_fail.image_embedding is None
            assert list(stored_skip.image_embedding) == pytest.approx(
                _FAKE_IMAGE_EMBEDDING
            )

        async with AsyncSessionLocal() as db:
            with patch.object(
                backfill,
                "embed_catalogue_image",
                new_callable=AsyncMock,
                return_value=_vector(7),
            ):
                repaired = await backfill.backfill_product_image_embeddings(
                    db, merchant_id, dry_run=False, force=False
                )
            assert fail_id in repaired["ok"]
            assert ok_id in repaired["skipped"]
            stored_fail = await db.get(Product, fail_id)
            assert stored_fail is not None
            assert list(stored_fail.image_embedding) == pytest.approx(_vector(7))

        async with AsyncSessionLocal() as db:
            with patch.object(
                backfill,
                "embed_catalogue_image",
                new_callable=AsyncMock,
            ) as embed:
                second = await backfill.backfill_product_image_embeddings(
                    db, merchant_id, dry_run=False, force=False
                )
            embed.assert_not_called()
            assert set(second["skipped"]) == {ok_id, fail_id, skip_id}
            assert second["ok"] == []
            assert second["failed"] == []
    finally:
        if merchant_id is not None:
            await _cleanup(merchant_id)
