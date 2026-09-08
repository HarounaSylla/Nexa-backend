import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.catalogue.embeddings import product_index_text
from app.catalogue.models import Merchant, Product
from app.catalogue.service import (
    ProductNotIndexedError,
    index_product,
    lister_categories,
    lister_produits_populaires,
    rechercher_produits,
    trouver_produits_similaires,
)
from app.core.db import AsyncSessionLocal
from app.orders.models import Order, OrderItem, OrderStatus, PaymentMethod
from sqlalchemy import delete, select


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
                unfiltered = await rechercher_produits(
                    db, merchant.id, "baskets homme", max_distance=2.0
                )
            assert [product.name for product in results] == ["match"]
            assert [product.name for product in unfiltered] == ["match", "other"]
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
            unfiltered = await trouver_produits_similaires(
                db, merchant.id, source.id, limit=5, max_distance=2.0
            )
            assert [product.name for product in results] == ["Robe noire"]
            assert [product.name for product in unfiltered] == ["Robe noire", "Jean slim"]
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


@pytest.mark.asyncio
async def test_rechercher_produits_lexical_fallback_when_vector_misses() -> None:
    query_vec = _vector(0)
    far_vec = _vector(20)
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-fts-hit-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        lexical_hit = Product(
            merchant_id=merchant.id,
            name="Robe longue rouge de soirée",
            description="pour un mariage",
            category="vêtements femme",
            price=Decimal("25000.00"),
            embedding=far_vec,
        )
        unrelated = Product(
            merchant_id=merchant.id,
            name="Powerbank 20000 mAh",
            description="charge rapide",
            category="électronique",
            price=Decimal("13000.00"),
            embedding=far_vec,
        )
        db.add_all([lexical_hit, unrelated])
        await db.flush()
        try:
            with patch(
                "app.catalogue.service.embed_query",
                return_value=query_vec,
            ):
                results = await rechercher_produits(
                    db, merchant.id, "vous avez des robes?"
                )
            names = [product.name for product in results]
            assert names == ["Robe longue rouge de soirée"]
        finally:
            await db.delete(unrelated)
            await db.delete(lexical_hit)
            await db.delete(merchant)
            await db.commit()


@pytest.mark.asyncio
async def test_rechercher_produits_lexical_fallback_still_rejects_ood() -> None:
    query_vec = _vector(0)
    far_vec = _vector(20)
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-fts-ood-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        product = Product(
            merchant_id=merchant.id,
            name="Robe longue rouge de soirée",
            description="pour un mariage",
            category="vêtements femme",
            price=Decimal("25000.00"),
            embedding=far_vec,
        )
        db.add(product)
        await db.flush()
        try:
            with patch(
                "app.catalogue.service.embed_query",
                return_value=query_vec,
            ):
                results = await rechercher_produits(
                    db, merchant.id, "ciment 50kg pour chantier"
                )
            assert results == []
        finally:
            await db.delete(product)
            await db.delete(merchant)
            await db.commit()


@pytest.mark.asyncio
async def test_lister_categories_counts_in_stock_and_stays_on_merchant() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-cats-{uuid.uuid4()}")
        other = Merchant(name=f"pytest-cats-other-{uuid.uuid4()}")
        db.add_all([merchant, other])
        await db.flush()
        in_stock_a = Product(
            merchant_id=merchant.id,
            name="Robe A",
            category="vêtements femme",
            price=Decimal("1000.00"),
            stock_qty=3,
        )
        in_stock_b = Product(
            merchant_id=merchant.id,
            name="Robe B",
            category="vêtements femme",
            price=Decimal("2000.00"),
            stock_qty=1,
        )
        cosmetics = Product(
            merchant_id=merchant.id,
            name="Karité",
            category="cosmétiques",
            price=Decimal("3000.00"),
            stock_qty=5,
        )
        out_of_stock_only = Product(
            merchant_id=merchant.id,
            name="Coque",
            category="électronique",
            price=Decimal("4000.00"),
            stock_qty=0,
        )
        leak = Product(
            merchant_id=other.id,
            name="Autre boutique",
            category="chaussures",
            price=Decimal("5000.00"),
            stock_qty=9,
        )
        db.add_all([in_stock_a, in_stock_b, cosmetics, out_of_stock_only, leak])
        await db.flush()
        try:
            rows = await lister_categories(db, merchant.id)
            assert rows == [
                {"category": "cosmétiques", "product_count": 1},
                {"category": "vêtements femme", "product_count": 2},
            ]
        finally:
            await db.delete(leak)
            await db.delete(out_of_stock_only)
            await db.delete(cosmetics)
            await db.delete(in_stock_b)
            await db.delete(in_stock_a)
            await db.delete(other)
            await db.delete(merchant)
            await db.commit()


def _make_product(
    merchant_id: uuid.UUID, name: str, price: str, stock_qty: int = 5
) -> Product:
    return Product(
        merchant_id=merchant_id,
        name=name,
        category="tests",
        price=Decimal(price),
        stock_qty=stock_qty,
    )


async def _add_order(
    db,
    merchant_id: uuid.UUID,
    items: list[tuple[uuid.UUID, int]],
    *,
    status: OrderStatus = OrderStatus.created,
) -> Order:
    order = Order(
        merchant_id=merchant_id,
        customer_phone="+221770000000",
        status=status,
        payment_method=PaymentMethod.cash_on_delivery,
        delivery_address="Dakar",
    )
    db.add(order)
    await db.flush()
    for product_id, quantity in items:
        db.add(
            OrderItem(
                order_id=order.id,
                product_id=product_id,
                quantity=quantity,
                unit_price=Decimal("1.00"),
            )
        )
    return order


async def _cleanup_popular_fixture(
    db, merchant_ids: list[uuid.UUID], products: list[Product]
) -> None:
    product_ids = [product.id for product in products]
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
        await db.execute(delete(Product).where(Product.id.in_(product_ids)))
    await db.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))
    await db.commit()


@pytest.mark.asyncio
async def test_lister_produits_populaires_ranks_orders_then_price() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-pop-{uuid.uuid4()}")
        other = Merchant(name=f"pytest-pop-other-{uuid.uuid4()}")
        db.add_all([merchant, other])
        await db.flush()
        cheap = _make_product(merchant.id, "Cheap seller", "1000.00")
        pricey = _make_product(merchant.id, "Pricey untouched", "9000.00")
        mid = _make_product(merchant.id, "Mid one sale", "5000.00")
        cancelled_only = _make_product(merchant.id, "Cancelled only", "8000.00")
        leak = _make_product(other.id, "Other merchant hit", "99000.00")
        db.add_all([cheap, pricey, mid, cancelled_only, leak])
        await db.flush()
        try:
            await _add_order(db, merchant.id, [(cheap.id, 1)])
            await _add_order(db, merchant.id, [(cheap.id, 3)])
            await _add_order(db, merchant.id, [(mid.id, 1)])
            await _add_order(
                db, merchant.id, [(mid.id, 1)], status=OrderStatus.cancelled
            )
            await _add_order(
                db,
                merchant.id,
                [(cancelled_only.id, 1)],
                status=OrderStatus.cancelled,
            )
            await _add_order(db, other.id, [(leak.id, 1)])
            await _add_order(db, other.id, [(leak.id, 1)])
            await db.flush()

            rows = await lister_produits_populaires(db, merchant.id, limit=10)
            names = [row["name"] for row in rows]
            counts = {row["name"]: row["order_count"] for row in rows}
            assert names == [
                "Cheap seller",
                "Mid one sale",
                "Pricey untouched",
                "Cancelled only",
            ]
            assert counts["Cheap seller"] == 2
            assert counts["Mid one sale"] == 1
            assert counts["Pricey untouched"] == 0
            assert counts["Cancelled only"] == 0
            assert "Other merchant hit" not in names
        finally:
            await _cleanup_popular_fixture(
                db, [merchant.id, other.id], [cheap, pricey, mid, cancelled_only, leak]
            )


@pytest.mark.asyncio
async def test_lister_produits_populaires_cold_start_is_priciest() -> None:
    async with AsyncSessionLocal() as db:
        merchant = Merchant(name=f"pytest-pop-cold-{uuid.uuid4()}")
        db.add(merchant)
        await db.flush()
        products = [
            _make_product(merchant.id, f"P{i}", f"{(i + 1) * 1000}.00")
            for i in range(12)
        ]
        db.add_all(products)
        await db.flush()
        try:
            rows = await lister_produits_populaires(db, merchant.id, limit=10)
            assert [row["name"] for row in rows] == [
                "P11", "P10", "P9", "P8", "P7", "P6", "P5", "P4", "P3", "P2"
            ]
            assert all(row["order_count"] == 0 for row in rows)
            assert len(rows) == 10
        finally:
            await _cleanup_popular_fixture(db, [merchant.id], products)
