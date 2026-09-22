"""Generate placeholder photos for Boutique Awa products that have none.

Uses the existing `enregistrer_photo_produit` write path. Safe to re-run:
products that already have a `product_images` row are skipped.

OpenAI image models (gpt-image-1) are 5 IPM on Tier 1. This script generates
one image at a time and waits 15s between successes so it stays under that.
429s sleep on Retry-After (or 30s) and retry.

    uv run python scripts/backfill_product_photos.py
"""

from __future__ import annotations

import asyncio
import base64
import logging
import httpx
from openai import AsyncOpenAI
from sqlalchemy import select

from app.catalogue.models import Merchant, Product, ProductImage
from app.catalogue.service import enregistrer_photo_produit
from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.merchants.service import DEMO_MERCHANT_NAME

logger = logging.getLogger(__name__)

IMAGE_MODEL = "gpt-image-1"
IMAGE_SIZE = "1024x1024"
# gpt-image-1 Tier 1 = 5 images/minute. 15s => 4 IPM.
PACE_SECONDS = 15.0
MAX_RETRIES = 3


def _prompt(product: Product) -> str:
    category = product.category or "retail"
    description = product.description or product.name
    return (
        "Professional ecommerce product photograph, single item centered, "
        "clean neutral light-gray studio background, soft even lighting, "
        "realistic, no text, no watermark, no logo, no people, no mannequin. "
        f"Product name: {product.name}. Category: {category}. "
        f"Description: {description}."
    )


def _bytes_from_generation(image) -> tuple[bytes, str]:
    b64 = getattr(image, "b64_json", None)
    if b64:
        return base64.b64decode(b64), "image/png"
    url = getattr(image, "url", None)
    if not url:
        raise RuntimeError("Image generation returned neither b64_json nor url")
    response = httpx.get(url, timeout=60.0)
    response.raise_for_status()
    content_type = response.headers.get("content-type", "image/png").split(";")[0]
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        content_type = "image/png"
    return response.content, content_type


async def _generate(client: AsyncOpenAI, product: Product) -> tuple[bytes, str]:
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = await client.images.generate(
                model=IMAGE_MODEL,
                prompt=_prompt(product),
                size=IMAGE_SIZE,
                n=1,
            )
            if not result.data:
                raise RuntimeError("Image generation returned no data")
            return _bytes_from_generation(result.data[0])
        except Exception as exc:
            last_error = exc
            retry_after = 30.0
            response = getattr(exc, "response", None)
            if response is not None:
                header = response.headers.get("retry-after")
                if header:
                    try:
                        retry_after = float(header)
                    except ValueError:
                        pass
                if getattr(response, "status_code", None) != 429:
                    raise
            elif "429" not in str(exc):
                raise
            logger.warning(
                "Rate limited on %s (attempt %s/%s), sleeping %.0fs",
                product.name,
                attempt,
                MAX_RETRIES,
                retry_after,
            )
            await asyncio.sleep(retry_after)
    assert last_error is not None
    raise last_error


async def _products_missing_photo(db) -> tuple[Merchant, list[Product]]:
    result = await db.execute(
        select(Merchant)
        .where(Merchant.name == DEMO_MERCHANT_NAME)
        .order_by(Merchant.created_at)
        .limit(1)
    )
    merchant = result.scalar_one_or_none()
    if merchant is None:
        raise SystemExit(f"Merchant {DEMO_MERCHANT_NAME!r} was not found")
    imaged = select(ProductImage.product_id).where(
        ProductImage.product_id == Product.id
    )
    products = list(
        (
            await db.execute(
                select(Product)
                .where(Product.merchant_id == merchant.id)
                .where(~imaged.exists())
                .order_by(Product.name)
            )
        ).scalars().all()
    )
    return merchant, products


async def _count_photos(db, merchant_id) -> tuple[int, int]:
    total = (
        await db.execute(
            select(Product).where(Product.merchant_id == merchant_id)
        )
    ).scalars().all()
    with_photo = 0
    for product in total:
        has = (
            await db.execute(
                select(ProductImage.id).where(
                    ProductImage.product_id == product.id
                )
            )
        ).scalar_one_or_none()
        if has is not None:
            with_photo += 1
    return len(total), with_photo


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not settings.openai_api_key:
        raise SystemExit("OPENAI_API_KEY is empty. Set it in .env first.")

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    generated = 0
    async with AsyncSessionLocal() as db:
        merchant, pending = await _products_missing_photo(db)
        total, with_photo = await _count_photos(db, merchant.id)
        print(
            f"{DEMO_MERCHANT_NAME}: {total} products, {with_photo} with a photo, "
            f"{len(pending)} to generate",
            flush=True,
        )
        if not pending:
            print("Nothing to do (idempotent).", flush=True)
            return

        for index, product in enumerate(pending, start=1):
            print(
                f"[{index}/{len(pending)}] generating: {product.name} ({product.id})",
                flush=True,
            )
            content, content_type = await _generate(client, product)
            await enregistrer_photo_produit(
                db,
                merchant.id,
                product.id,
                content,
                content_type,
            )
            generated += 1
            print(
                f"  saved via enregistrer_photo_produit ({content_type})",
                flush=True,
            )
            if index < len(pending):
                await asyncio.sleep(PACE_SECONDS)

        total, with_photo = await _count_photos(db, merchant.id)
        print("---", flush=True)
        print(f"generated_this_run={generated}", flush=True)
        print(f"products={total}", flush=True)
        print(f"with_photo={with_photo}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
