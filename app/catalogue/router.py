import uuid
from decimal import Decimal
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_merchant
from app.catalogue.models import Merchant, Product
from app.catalogue.service import (
    ALLOWED_PHOTO_TYPES,
    MAX_PHOTO_BYTES,
    CategoryNotFoundError,
    InvalidPhotoError,
    ProductHasOrderHistoryError,
    ProductNotFoundError,
    ProductNotIndexedError,
    creer_produit,
    enregistrer_photo_produit,
    image_urls_for_products,
    lister_categories_dashboard,
    lister_produits_commercant,
    lister_produits_populaires,
    mettre_a_jour_produit,
    rechercher_produits,
    renommer_categorie,
    supprimer_produit,
    trouver_produits_similaires,
)
from app.core.db import get_db

# Temporary scaffolding to validate RAG relevance by hand before the
# WhatsApp agent exists (Jalon 3). Not the final API surface; no auth.
# Authenticated merchant routes below are the real dashboard API.
router = APIRouter(prefix="/catalogue", tags=["catalogue"])


class ProductSearchItem(BaseModel):
    id: uuid.UUID
    name: str
    category: str | None
    price: Decimal | None


class PopularProductOut(BaseModel):
    product_id: uuid.UUID
    name: str
    price: Decimal | None
    category: str | None
    stock_qty: int
    order_count: int


class MerchantProductOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    category: str | None
    price: Decimal | None
    stock_qty: int
    image_url: str | None


class ProductCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    price: Decimal
    category: str = Field(min_length=1)
    stock_qty: int = Field(ge=0)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    description: str | None = None
    price: Decimal | None = None
    category: str | None = Field(default=None, min_length=1)
    stock_qty: int | None = Field(default=None, ge=0)


class CategoryOut(BaseModel):
    category: str | None
    product_count: int


class CategoryRename(BaseModel):
    new_name: str = Field(min_length=1)


class CategoryRenameOut(BaseModel):
    category: str
    product_count: int


def _to_item(product: Product) -> ProductSearchItem:
    return ProductSearchItem(
        id=product.id,
        name=product.name,
        category=product.category,
        price=product.price,
    )


async def _to_merchant_item(
    db: AsyncSession, product: Product
) -> MerchantProductOut:
    urls = await image_urls_for_products(db, [product.id])
    return MerchantProductOut(
        id=product.id,
        name=product.name,
        description=product.description,
        category=product.category,
        price=product.price,
        stock_qty=product.stock_qty,
        image_url=urls.get(product.id),
    )


def _not_found_product() -> HTTPException:
    return HTTPException(status_code=404, detail="Product was not found")


@router.get("/search", response_model=list[ProductSearchItem])
async def search_products(
    merchant_id: uuid.UUID,
    q: str,
    category: str | None = None,
    limit: int = Query(default=5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[ProductSearchItem]:
    products = await rechercher_produits(
        db,
        merchant_id=merchant_id,
        requete=q,
        categorie=category,
        limit=limit,
    )
    return [_to_item(product) for product in products]


@router.get("/products", response_model=list[MerchantProductOut])
async def list_merchant_products(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> list[MerchantProductOut]:
    """List the calling merchant's catalogue (exact stock, no pagination)."""
    products = await lister_produits_commercant(db, merchant.id)
    urls = await image_urls_for_products(db, [product.id for product in products])
    return [
        MerchantProductOut(
            id=product.id,
            name=product.name,
            description=product.description,
            category=product.category,
            price=product.price,
            stock_qty=product.stock_qty,
            image_url=urls.get(product.id),
        )
        for product in products
    ]


@router.post("/products", response_model=MerchantProductOut)
async def create_merchant_product(
    body: ProductCreate,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> MerchantProductOut:
    product = await creer_produit(
        db,
        merchant.id,
        name=body.name,
        description=body.description,
        price=body.price,
        category=body.category,
        stock_qty=body.stock_qty,
    )
    return await _to_merchant_item(db, product)


@router.patch("/products/{product_id}", response_model=MerchantProductOut)
async def update_merchant_product(
    product_id: uuid.UUID,
    body: ProductUpdate,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> MerchantProductOut:
    try:
        product = await mettre_a_jour_produit(
            db,
            merchant.id,
            product_id,
            name=body.name,
            description=body.description,
            price=body.price,
            category=body.category,
            stock_qty=body.stock_qty,
            fields_set=set(body.model_fields_set),
        )
    except ProductNotFoundError as exc:
        raise _not_found_product() from exc
    return await _to_merchant_item(db, product)


@router.delete("/products/{product_id}", status_code=204)
async def delete_merchant_product(
    product_id: uuid.UUID,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await supprimer_produit(db, merchant.id, product_id)
    except ProductNotFoundError as exc:
        raise _not_found_product() from exc
    except ProductHasOrderHistoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/products/{product_id}/photo", response_model=MerchantProductOut)
async def upload_merchant_product_photo(
    product_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> MerchantProductOut:
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_PHOTO_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "Photo must be a JPEG, PNG, or WebP image "
                f"(received {file.content_type or 'unknown type'})"
            ),
        )
    content = await file.read(MAX_PHOTO_BYTES + 1)
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=400,
            detail="Photo must be 5 MB or smaller",
        )
    try:
        await enregistrer_photo_produit(
            db,
            merchant.id,
            product_id,
            content=content,
            content_type=content_type,
        )
        product = await db.get(Product, product_id)
        if product is None:
            raise ProductNotFoundError(product_id)
        return await _to_merchant_item(db, product)
    except ProductNotFoundError as exc:
        raise _not_found_product() from exc
    except InvalidPhotoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/products/{product_id}/similar",
    response_model=list[ProductSearchItem],
)
async def similar_products(
    product_id: uuid.UUID,
    limit: int = Query(default=5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[ProductSearchItem]:
    product = await db.get(Product, product_id)
    if product is None:
        raise HTTPException(status_code=404, detail=f"Product {product_id} was not found")
    try:
        products = await trouver_produits_similaires(
            db,
            merchant_id=product.merchant_id,
            produit_id=product_id,
            limit=limit,
        )
    except ProductNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ProductNotIndexedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return [_to_item(item) for item in products]


@router.get("/categories", response_model=list[CategoryOut])
async def list_merchant_categories(
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> list[CategoryOut]:
    rows = await lister_categories_dashboard(db, merchant.id)
    return [CategoryOut.model_validate(row) for row in rows]


@router.patch("/categories/{old_name}", response_model=CategoryRenameOut)
async def rename_merchant_category(
    old_name: str,
    body: CategoryRename,
    merchant: Merchant = Depends(get_current_merchant),
    db: AsyncSession = Depends(get_db),
) -> CategoryRenameOut:
    decoded = unquote(old_name)
    try:
        updated = await renommer_categorie(
            db, merchant.id, decoded, body.new_name
        )
    except CategoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CategoryRenameOut(category=body.new_name, product_count=updated)


@router.get("/popular-products", response_model=list[PopularProductOut])
async def popular_products(
    merchant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> list[PopularProductOut]:
    rows = await lister_produits_populaires(db, merchant_id, limit=10)
    return [PopularProductOut.model_validate(row) for row in rows]
