from decimal import Decimal
from unittest.mock import patch

import pytest

from app.catalogue.embeddings import product_index_text
from app.catalogue.models import Merchant, Product
from app.catalogue.service import (
    ProductNotIndexedError,
    index_product,
    rechercher_produits,
    trouver_produits_similaires,
)
from app.core.db import AsyncSessionLocal


def _vector(index: int) -> list[float]:
    values = [0.0] * 1024
    values[index] = 1.0
    return values


def test_product_index_text() -> None:
    product = Product(
        name="Robe longue rouge",
        description="Pour une soirée",
        category="vêtements femme",
    )
    assert (
        product_index_text(product)
        == "Robe longue rouge. Pour une soirée. Category: vêtements femme."
    )


@pytest.mark.asyncio
async def test_rechercher_produits_orders_by_cosine_distance() -> None:
    query = _vector(0)
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name="pytest-search")
        db.add(merchant)
        await db.flush()
        match = Product(
            merchant_id=merchant.id,
            name="match",
            description="close to query",
            category="chaussures",
            price=Decimal("1.00"),
            embedding=query,
        )
        other = Product(
            merchant_id=merchant.id,
            name="other",
            description="far from query",
            category="chaussures",
            price=Decimal("2.00"),
            embedding=_vector(1),
        )
        db.add_all([match, other])
        await db.flush()
        try:
            with patch(
                "app.catalogue.service.embed_query",
                return_value=query,
            ):
                results = await rechercher_produits(
                    db, merchant.id, "baskets homme"
                )
            assert [product.name for product in results] == ["match", "other"]
        finally:
            await db.delete(other)
            await db.delete(match)
            await db.delete(merchant)
            await db.commit()


@pytest.mark.asyncio
async def test_trouver_produits_similaires_ranks_near_duplicate() -> None:
    source_vec = _vector(0)
    near_vec = [0.99] + [0.0] * 1023
    far_vec = _vector(5)
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name="pytest-similar")
        db.add(merchant)
        await db.flush()
        source = Product(
            merchant_id=merchant.id,
            name="Robe rouge",
            description="soirée",
            category="vêtements femme",
            embedding=source_vec,
        )
        near = Product(
            merchant_id=merchant.id,
            name="Robe noire",
            description="soirée",
            category="vêtements femme",
            embedding=near_vec,
        )
        far = Product(
            merchant_id=merchant.id,
            name="Jean slim",
            description="quotidien",
            category="vêtements femme",
            embedding=far_vec,
        )
        other_category = Product(
            merchant_id=merchant.id,
            name="Coque iPhone",
            description="électronique",
            category="électronique",
            embedding=source_vec,
        )
        db.add_all([source, near, far, other_category])
        await db.flush()
        try:
            results = await trouver_produits_similaires(
                db, merchant.id, source.id, limit=5
            )
            assert [product.name for product in results] == ["Robe noire", "Jean slim"]
        finally:
            await db.delete(other_category)
            await db.delete(far)
            await db.delete(near)
            await db.delete(source)
            await db.delete(merchant)
            await db.commit()


@pytest.mark.asyncio
async def test_trouver_produits_similaires_raises_if_not_indexed() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name="pytest-unindexed")
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Pas encore indexé",
            description="sans vecteur",
            category="accessoires",
            embedding=None,
        )
        db.add(product)
        await db.flush()
        try:
            with pytest.raises(ProductNotIndexedError):
                await trouver_produits_similaires(db, merchant.id, product.id)
        finally:
            await db.delete(product)
            await db.delete(merchant)
            await db.commit()


@pytest.mark.asyncio
async def test_index_product_stores_embedding() -> None:
    fake = _vector(3)
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name="pytest-index")
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Beurre de karité",
            description="hydratation",
            category="cosmétiques",
        )
        db.add(product)
        await db.flush()
        try:
            with patch(
                "app.catalogue.service.embed_documents",
                return_value=[fake],
            ):
                await index_product(db, product)
            assert product.embedding is not None
            assert len(product.embedding) == 1024
            assert product.embedding[3] == pytest.approx(1.0)
        finally:
            await db.delete(product)
            await db.delete(merchant)
            await db.commit()
