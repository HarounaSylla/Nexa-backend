import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.models import Product
from app.catalogue.service import (
    ProductNotFoundError,
    ProductNotIndexedError,
    rechercher_produits,
    trouver_produits_similaires,
)
from app.core.db import get_db

# Temporary scaffolding to validate RAG relevance by hand before the
# WhatsApp agent exists (Jalon 3). Not the final API surface; no auth.
router = APIRouter(prefix="/catalogue", tags=["catalogue"])


class ProductSearchItem(BaseModel):
    id: uuid.UUID
    name: str
    category: str | None
    price: Decimal | None


def _to_item(product: Product) -> ProductSearchItem:
    return ProductSearchItem(
        id=product.id,
        name=product.name,
        category=product.category,
        price=product.price,
    )


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
