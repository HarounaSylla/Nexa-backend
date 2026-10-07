import asyncio
import logging
import unicodedata
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from sqlalchemy import and_, delete, func, literal, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.embeddings import embed_documents, embed_query, product_index_text
from app.catalogue.image_embeddings import (
    ImageEmbeddingError,
    embed_catalogue_image,
    embed_query_image,
)
from app.catalogue.models import Product, ProductImage
from app.core.config import settings
from app.orders.models import Order, OrderItem, OrderStatus

logger = logging.getLogger(__name__)

PRODUCT_IMAGES_DIR = (
    Path(__file__).resolve().parent.parent / "static" / "product_images"
)
MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_PHOTO_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


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


class ProductHasOrderHistoryError(ValueError):
    """Raised when a product cannot be deleted because orders reference it."""

    def __init__(self, product_id: uuid.UUID) -> None:
        super().__init__(
            "This product has existing orders and cannot be deleted. "
            "Set stock to 0 instead of removing it from the catalogue."
        )
        self.product_id = product_id


class CategoryNotFoundError(LookupError):
    def __init__(self, category: str) -> None:
        super().__init__(f"Category {category!r} was not found")
        self.category = category


class InvalidPhotoError(ValueError):
    """Raised when an uploaded product photo fails validation."""


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


def _normalize_category_key(value: str) -> str:
    """Same fold as `normalize_city`: strip, casefold, drop accents.

    Categories stay stored as the merchant typed them. We only fold a
    copy for matching, then filter with exact equality on the stored
    value so `ix_products_category` stays usable.
    """
    collapsed = " ".join(value.split())
    decomposed = unicodedata.normalize("NFKD", collapsed)
    without_accents = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return without_accents.casefold()


def _plural_variants(key: str) -> set[str]:
    variants = {key}
    if len(key) > 1 and key.endswith("s"):
        variants.add(key[:-1])
    elif key:
        variants.add(key + "s")
    return variants


def resolve_stored_category(
    requested: str, stored_categories: list[str]
) -> str | None:
    """Map a guessed category string to one stored `Product.category`.

    Exact normalized match first, then a trivial singular/plural `s`.
    Ambiguous matches return None (caller falls back to an unfiltered search).
    """
    key = _normalize_category_key(requested)
    if not key:
        return None
    by_key: dict[str, str] = {}
    for category in stored_categories:
        if not category:
            continue
        folded = _normalize_category_key(category)
        by_key.setdefault(folded, category)
    if key in by_key:
        return by_key[key]
    matches = {
        by_key[variant] for variant in _plural_variants(key) if variant in by_key
    }
    if len(matches) == 1:
        return next(iter(matches))
    return None


async def _merchant_category_names(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[str]:
    result = await db.execute(
        select(Product.category)
        .where(
            Product.merchant_id == merchant_id,
            Product.category.is_not(None),
        )
        .distinct()
    )
    return [row[0] for row in result.all() if row[0]]


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


async def _search_with_optional_category(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    requete: str,
    categorie: str | None,
    limit: int,
    cutoff: float,
    query_embedding: list[float],
) -> list[Product]:
    """Vector search then lexical fallback, both with the same category filter.

    `categorie` is already a stored value or None — never a guessed spelling.
    """
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

    A guessed `categorie` is resolved against the merchant's stored
    category names (accent/case/trivial plural) and applied as exact
    equality. If that filtered search is empty, the same search runs
    once more without a category.
    """
    cutoff = settings.rag_max_distance if max_distance is None else max_distance
    requested = (categorie or "").strip() or None
    stored: str | None = None
    if requested is not None:
        names = await _merchant_category_names(db, merchant_id)
        stored = resolve_stored_category(requested, names)
    filter_value = stored if stored is not None else requested

    query_embedding = await asyncio.to_thread(embed_query, requete)
    products = await _search_with_optional_category(
        db,
        merchant_id,
        requete,
        filter_value,
        limit,
        cutoff,
        query_embedding,
    )
    if products:
        if (
            requested is not None
            and stored is not None
            and stored != requested
        ):
            logger.info(
                "rechercher_produits category resolved merchant_id=%s "
                "requested=%s resolved=%s",
                merchant_id,
                requested,
                stored,
            )
        return products
    if requested is None:
        return []
    logger.info(
        "rechercher_produits category dropped merchant_id=%s "
        "requested=%s resolved=dropped",
        merchant_id,
        requested,
    )
    return await _search_with_optional_category(
        db,
        merchant_id,
        requete,
        None,
        limit,
        cutoff,
        query_embedding,
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


@dataclass(frozen=True)
class ImageMatch:
    product: Product
    distance: float
    in_stock: bool


async def trouver_produits_par_image(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    content: bytes,
    mime: str,
    limit: int = 3,
) -> list[ImageMatch]:
    """Rank this merchant's catalogue photos by cosine distance to `content`.

    Only rows with `image_embedding` set and `image_embedding_model` equal
    to the current setting are considered. Out-of-stock products are
    included (the caller may say "rupture"). No distance threshold is
    applied here — use `classify_image_match` for that.
    """
    query_embedding = await embed_query_image(content, mime)
    distance = Product.image_embedding.cosine_distance(query_embedding)
    stmt = (
        select(Product, distance.label("image_distance"))
        .where(
            Product.merchant_id == merchant_id,
            Product.image_embedding.is_not(None),
            Product.image_embedding_model == settings.voyage_image_model,
        )
        .order_by(distance)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [
        ImageMatch(
            product=product,
            distance=float(dist),
            in_stock=product.stock_qty > 0,
        )
        for product, dist in result.all()
    ]


def classify_image_match(
    matches: list[ImageMatch],
) -> Literal["strong", "possible", "none"]:
    """Bucket a ranked image search using the three settings placeholders.

    Distances are cosine (pgvector `<=>`; lower is closer). The numeric
    cutoffs are to be tuned on real customer photos.

    - ``strong``: best distance ≤ `image_match_strong_distance` **and**
      (no second candidate, or gap to second ≥ `image_match_min_margin`).
    - ``possible``: not strong, but best distance ≤
      `image_match_possible_distance` (a close but ambiguous pair lands
      here).
    - ``none``: no candidates, or best distance above the possible cutoff.
    """
    if not matches:
        return "none"
    best = matches[0].distance
    if best > settings.image_match_possible_distance:
        return "none"
    second = matches[1].distance if len(matches) > 1 else None
    gap_ok = (
        second is None
        or (second - best) >= settings.image_match_min_margin
    )
    if best <= settings.image_match_strong_distance and gap_ok:
        return "strong"
    return "possible"


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


async def noms_produits(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    product_ids: list[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """Current names for this merchant's products. Unknown or foreign ids omitted."""
    ids = list({product_id for product_id in product_ids if product_id is not None})
    if not ids:
        return {}
    result = await db.execute(
        select(Product.id, Product.name).where(
            Product.merchant_id == merchant_id,
            Product.id.in_(ids),
        )
    )
    return {row.id: row.name for row in result.all()}


async def image_urls_for_products(
    db: AsyncSession, product_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str | None]:
    """Return the current photo URL per product (one image each, latest row)."""
    urls: dict[uuid.UUID, str | None] = {product_id: None for product_id in product_ids}
    if not product_ids:
        return urls
    result = await db.execute(
        select(ProductImage)
        .where(ProductImage.product_id.in_(product_ids))
        .order_by(ProductImage.created_at.desc())
    )
    for image in result.scalars().all():
        if urls.get(image.product_id) is None:
            urls[image.product_id] = image.url
    return urls


async def _owned_product(
    db: AsyncSession, merchant_id: uuid.UUID, product_id: uuid.UUID
) -> Product:
    product = await db.get(Product, product_id)
    if product is None or product.merchant_id != merchant_id:
        raise ProductNotFoundError(product_id)
    return product


async def lister_produits_commercant(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[Product]:
    """List every product for this merchant.

    No pagination — fine for a few hundred items; add a limit/offset
    when catalogues grow past that.
    """
    result = await db.execute(
        select(Product)
        .where(Product.merchant_id == merchant_id)
        .order_by(Product.name, Product.created_at)
    )
    return list(result.scalars().all())


async def creer_produit(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    *,
    name: str,
    description: str | None,
    price: Decimal | None,
    category: str | None,
    stock_qty: int,
) -> Product:
    product = Product(
        merchant_id=merchant_id,
        name=name,
        description=description,
        price=price,
        category=category,
        stock_qty=stock_qty,
    )
    db.add(product)
    await db.flush()
    await index_product(db, product)
    await db.commit()
    await db.refresh(product)
    return product


async def mettre_a_jour_produit(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    name: str | None = None,
    description: str | None = None,
    price: Decimal | None = None,
    category: str | None = None,
    stock_qty: int | None = None,
    fields_set: set[str] | None = None,
) -> Product:
    """Patch the owned product. Re-embed if name/description/category change."""
    product = await _owned_product(db, merchant_id, product_id)
    provided = fields_set if fields_set is not None else {
        key
        for key, value in (
            ("name", name),
            ("description", description),
            ("price", price),
            ("category", category),
            ("stock_qty", stock_qty),
        )
        if value is not None
    }
    if "name" in provided and name is not None:
        product.name = name
    if "description" in provided:
        product.description = description
    if "price" in provided:
        product.price = price
    if "category" in provided:
        product.category = category
    if "stock_qty" in provided and stock_qty is not None:
        product.stock_qty = stock_qty
    if provided & {"name", "description", "category"}:
        await index_product(db, product)
    await db.commit()
    await db.refresh(product)
    return product


def chemin_photo_locale(product_id: uuid.UUID) -> Path | None:
    """On-disk photo written by `enregistrer_photo_produit`, if any."""
    if not PRODUCT_IMAGES_DIR.exists():
        return None
    matches = sorted(PRODUCT_IMAGES_DIR.glob(f"{product_id}.*"))
    return matches[0] if matches else None


async def _delete_product_files(product_id: uuid.UUID) -> None:
    if not PRODUCT_IMAGES_DIR.exists():
        return
    for path in PRODUCT_IMAGES_DIR.glob(f"{product_id}.*"):
        path.unlink(missing_ok=True)


async def _clear_image_embedding(product: Product) -> None:
    product.image_embedding = None
    product.image_embedding_model = None


async def _store_image_embedding(
    db: AsyncSession,
    product: Product,
    content: bytes,
    content_type: str,
) -> None:
    """Compute image_embedding after the photo row is committed.

    Failures are logged (product id only) and leave both columns NULL so
    a stale vector never survives a replaced photo.
    """
    try:
        vector = await embed_catalogue_image(content, content_type)
    except ImageEmbeddingError:
        logger.warning(
            "Image embedding failed for product_id=%s; columns left NULL",
            product.id,
        )
        return
    product.image_embedding = vector
    product.image_embedding_model = settings.voyage_image_model
    await db.commit()


async def supprimer_produit(
    db: AsyncSession, merchant_id: uuid.UUID, product_id: uuid.UUID
) -> None:
    product = await _owned_product(db, merchant_id, product_id)
    result = await db.execute(
        select(func.count())
        .select_from(OrderItem)
        .where(OrderItem.product_id == product.id)
    )
    if int(result.scalar_one()) > 0:
        raise ProductHasOrderHistoryError(product.id)
    await db.execute(
        delete(ProductImage).where(ProductImage.product_id == product.id)
    )
    await _delete_product_files(product.id)
    await db.delete(product)
    await db.commit()


async def lister_categories_dashboard(
    db: AsyncSession, merchant_id: uuid.UUID
) -> list[dict[str, str | int]]:
    """All categories for this merchant, including out-of-stock ones.

    Unlike lister_categories (agent-facing, stock_qty > 0), the dashboard
    must show a category the merchant is still managing even if every
    product in it is at 0.
    """
    result = await db.execute(
        select(Product.category, func.count())
        .where(Product.merchant_id == merchant_id)
        .group_by(Product.category)
        .order_by(Product.category)
    )
    return [
        {"category": category, "product_count": count}
        for category, count in result.all()
    ]


async def renommer_categorie(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    old_name: str,
    new_name: str,
) -> int:
    """Bulk-rename a category string on this merchant's products only."""
    result = await db.execute(
        update(Product)
        .where(
            Product.merchant_id == merchant_id,
            Product.category == old_name,
        )
        .values(category=new_name)
    )
    updated = int(result.rowcount or 0)
    if updated == 0:
        raise CategoryNotFoundError(old_name)
    await db.commit()
    return updated


def _photo_extension(content_type: str) -> str:
    ext = ALLOWED_PHOTO_TYPES.get(content_type)
    if ext is None:
        raise InvalidPhotoError(
            "Photo must be a JPEG, PNG, or WebP image "
            f"(received {content_type or 'unknown type'})"
        )
    return ext


async def enregistrer_photo_produit(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    product_id: uuid.UUID,
    content: bytes,
    content_type: str,
) -> ProductImage:
    """Validate, write `{product_id}.{ext}`, and upsert the product_images row."""
    product = await _owned_product(db, merchant_id, product_id)
    ext = _photo_extension(content_type)
    if len(content) > MAX_PHOTO_BYTES:
        raise InvalidPhotoError("Photo must be 5 MB or smaller")
    if not content:
        raise InvalidPhotoError("Photo file is empty")

    PRODUCT_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    await _delete_product_files(product.id)
    dest = PRODUCT_IMAGES_DIR / f"{product.id}.{ext}"
    dest.write_bytes(content)
    url = f"/static/product_images/{product.id}.{ext}"

    result = await db.execute(
        select(ProductImage)
        .where(ProductImage.product_id == product.id)
        .order_by(ProductImage.created_at.desc())
    )
    images = list(result.scalars().all())
    if images:
        keep = images[0]
        keep.url = url
        for extra in images[1:]:
            await db.delete(extra)
        image = keep
    else:
        image = ProductImage(product_id=product.id, url=url)
        db.add(image)
    await _clear_image_embedding(product)
    await db.commit()
    await db.refresh(image)
    await db.refresh(product)
    await _store_image_embedding(db, product, content, content_type)
    return image
