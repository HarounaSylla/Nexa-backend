import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.embeddings import embed_documents, embed_query, product_index_text
from app.catalogue.models import Product


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


async def rechercher_produits(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    requete: str,
    categorie: str | None = None,
    limit: int = 5,
) -> list[Product]:
    """Embed `requete` with embed_query(), then order products for this
    merchant by cosine distance to the query embedding (pgvector `<=>`
    operator via the Vector comparator), filtered by category if given,
    limited to `limit`. Only rows with a non-null embedding are eligible.
    """
    query_embedding = await asyncio.to_thread(embed_query, requete)
    stmt = (
        select(Product)
        .where(
            Product.merchant_id == merchant_id,
            Product.embedding.is_not(None),
        )
        .order_by(Product.embedding.cosine_distance(query_embedding))
        .limit(limit)
    )
    if categorie is not None:
        stmt = stmt.where(Product.category == categorie)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def trouver_produits_similaires(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    produit_id: uuid.UUID,
    limit: int = 5,
) -> list[Product]:
    """Look up the source product's embedding, then order other products
    in the same category and merchant by cosine distance to it, excluding
    the source product itself.
    """
    source = await db.get(Product, produit_id)
    if source is None or source.merchant_id != merchant_id:
        raise ProductNotFoundError(produit_id)
    if source.embedding is None:
        raise ProductNotIndexedError(produit_id)

    stmt = (
        select(Product)
        .where(
            Product.merchant_id == merchant_id,
            Product.category == source.category,
            Product.id != produit_id,
            Product.embedding.is_not(None),
        )
        .order_by(Product.embedding.cosine_distance(source.embedding))
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
