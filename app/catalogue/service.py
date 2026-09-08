import asyncio
import uuid

from sqlalchemy import and_, func, literal, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.embeddings import embed_documents, embed_query, product_index_text
from app.catalogue.models import Product
from app.core.config import settings
from app.orders.models import Order, OrderItem, OrderStatus


class ProductNotFoundError(LookupError):
    def __init__(self, product_id: uuid.UUID) -> None:
        super().__init__(f"Product {product_id} was not found")
        self.product_id = product_id


class ProductNotIndexedError(ValueError):
    """Raised when search is asked to use a product that has no embedding yet."""

    def __init__(self, product_id: uuid.UUID) -> None:
        super().__init__(
            f"Product {product_id} has no embedding yet; "
            "call index_product() before using it in search"
        )
        self.product_id = product_id


async def index_product(db: AsyncSession, product: Product) -> None:
    """Compute and store product.embedding via embed_documents().

    Call this after creating or updating a product's name/description/category.
    """
    embeddings = await asyncio.to_thread(
        embed_documents, [product_index_text(product)]
    )
    product.embedding = embeddings[0]
    await db.flush()


_FRENCH_TS_CONFIG = literal_column("'french'")


def _product_french_tsvector():
    """Same expression as the GIN index on products (migration 0006)."""
    document = (
        func.coalesce(Product.name, "")
        + literal(" ")
        + func.coalesce(Product.description, "")
        + literal(" ")
        + func.coalesce(Product.category, "")
    )
    return func.to_tsvector(_FRENCH_TS_CONFIG, document)


async def _rechercher_produits_lexical(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    requete: str,
    categorie: str | None,
    limit: int,
) -> list[Product]:
    tsvector = _product_french_tsvector()
    tsquery = func.plainto_tsquery(_FRENCH_TS_CONFIG, requete)
    stmt = (
        select(Product)
        .where(
            Product.merchant_id == merchant_id,
            tsvector.op("@@")(tsquery),
        )
        .order_by(func.ts_rank(tsvector, tsquery).desc())
        .limit(limit)
    )
    if categorie is not None:
        stmt = stmt.where(Product.category == categorie)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def rechercher_produits(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    requete: str,
    categorie: str | None = None,
    limit: int = 5,
    max_distance: float | None = None,
) -> list[Product]:
    """Embed `requete` with embed_query(), then order products for this
    merchant by cosine distance. Rows farther than `max_distance`
    (default: settings.rag_max_distance) are dropped, so an out-of-
    catalogue query returns [] rather than weak matches.

    Vector search runs first (meaning and synonyms). If it returns
    nothing under the threshold, fall back to PostgreSQL French
    full-text search — a safety net for short/generic queries whose
    embedding misses the semantic cutoff. Do not apply this fallback
    to trouver_produits_similaires: that starts from a known
    produit_id, not a customer text query.
    """
    cutoff = settings.rag_max_distance if max_distance is None else max_distance
    query_embedding = await asyncio.to_thread(embed_query, requete)
    distance = Product.embedding.cosine_distance(query_embedding)
    stmt = (
        select(Product)
        .where(
            Product.merchant_id == merchant_id,
            Product.embedding.is_not(None),
            distance <= cutoff,
        )
        .order_by(distance)
        .limit(limit)
    )
    if categorie is not None:
        stmt = stmt.where(Product.category == categorie)
    result = await db.execute(stmt)
    products = list(result.scalars().all())
    if products:
        return products
    return await _rechercher_produits_lexical(
        db, merchant_id, requete, categorie, limit
    )


async def trouver_produits_similaires(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    produit_id: uuid.UUID,
    limit: int = 5,
    max_distance: float | None = None,
) -> list[Product]:
    """Look up the source product's embedding, then order other products
    in the same category and merchant by cosine distance to it, excluding
    the source product itself. Rows farther than `max_distance` (default:
    settings.rag_max_distance) are dropped.
    """
    cutoff = settings.rag_max_distance if max_distance is None else max_distance
    source = await db.get(Product, produit_id)
    if source is None or source.merchant_id != merchant_id:
        raise ProductNotFoundError(produit_id)
    if source.embedding is None:
        raise ProductNotIndexedError(produit_id)

    distance = Product.embedding.cosine_distance(source.embedding)
    stmt = (
        select(Product)
        .where(
            Product.merchant_id == merchant_id,
            Product.category == source.category,
            Product.id != produit_id,
            Product.embedding.is_not(None),
            distance <= cutoff,
        )
        .order_by(distance)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def lister_categories(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[dict[str, str | int]]:
    """Return distinct in-stock categories for this merchant.

    Counts only products with stock_qty > 0 — no point advertising a
    category that is entirely out of stock. Plain SQL, no embeddings:

    SELECT category, COUNT(*) FROM products
    WHERE merchant_id = :merchant_id AND stock_qty > 0
    GROUP BY category ORDER BY category.
    """
    result = await db.execute(
        select(Product.category, func.count())
        .where(
            Product.merchant_id == merchant_id,
            Product.stock_qty > 0,
        )
        .group_by(Product.category)
        .order_by(Product.category)
    )
    return [
        {"category": category, "product_count": count}
        for category, count in result.all()
    ]


async def lister_produits_populaires(
    db: AsyncSession, merchant_id: uuid.UUID, limit: int = 10
) -> list[dict]:
    """Return the top `limit` products ranked by distinct non-cancelled orders.

    Tied products (including all-zero on a merchant with no order history)
    are broken by price descending, so a brand-new catalogue naturally
    surfaces its priciest items with no special cold-start path.

    Cancelled orders are excluded from the join (never a real sale).
    stock_qty is returned but not used to filter — historical signal.
    """
    order_count = func.count(func.distinct(Order.id)).label("order_count")
    stmt = (
        select(
            Product.id,
            Product.name,
            Product.price,
            Product.category,
            Product.stock_qty,
            order_count,
        )
        .outerjoin(OrderItem, OrderItem.product_id == Product.id)
        .outerjoin(
            Order,
            and_(
                Order.id == OrderItem.order_id,
                Order.merchant_id == Product.merchant_id,
                Order.status != OrderStatus.cancelled,
            ),
        )
        .where(Product.merchant_id == merchant_id)
        .group_by(
            Product.id,
            Product.name,
            Product.price,
            Product.category,
            Product.stock_qty,
        )
        .order_by(order_count.desc(), Product.price.desc().nulls_last())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [
        {
            "product_id": row.id,
            "name": row.name,
            "price": row.price,
            "category": row.category,
            "stock_qty": row.stock_qty,
            "order_count": int(row.order_count),
        }
        for row in result.all()
    ]
