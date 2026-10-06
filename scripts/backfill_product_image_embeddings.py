"""Backfill `products.image_embedding` from on-disk catalogue photos.

Idempotent: skips a product when `image_embedding` is already set for the
current `voyage_image_model`, unless `--force`. Sequential; one failure
does not stop the rest.

    uv run python scripts/backfill_product_image_embeddings.py --dry-run
    uv run python scripts/backfill_product_image_embeddings.py --merchant-id <uuid>
    uv run python scripts/backfill_product_image_embeddings.py --force

Pricing used in the estimate (Voyage multimodal, billed per pixel):
https://docs.voyageai.com/docs/pricing — $0.60 per billion pixels, with a
50,000-pixel floor and a 2,000,000-pixel ceiling per image (min $0.00003,
max $0.0012).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalogue.image_embeddings import (
    ImageEmbeddingError,
    billable_pixel_count,
    embed_catalogue_image,
    estimate_voyage_image_cost_usd,
    prepare_image_for_embedding,
)
from app.catalogue.models import Product, ProductImage
from app.catalogue.service import chemin_photo_locale
from app.core.config import settings
from app.core.db import AsyncSessionLocal

logger = logging.getLogger(__name__)

_MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def _mime_for_path(path: Path) -> str:
    return _MIME_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")


def _needs_backfill(product: Product, force: bool) -> bool:
    if force:
        return True
    if product.image_embedding is None:
        return True
    if product.image_embedding_model != settings.voyage_image_model:
        return True
    return False


async def list_backfill_targets(
    db: AsyncSession,
    merchant_id: uuid.UUID | None,
    force: bool,
) -> tuple[list[tuple[Product, Path]], list[uuid.UUID]]:
    """Return (to_embed, skipped_already_current) for products with a local photo."""
    imaged = select(ProductImage.product_id)
    stmt = select(Product).where(Product.id.in_(imaged)).order_by(Product.name)
    if merchant_id is not None:
        stmt = stmt.where(Product.merchant_id == merchant_id)
    products = list((await db.execute(stmt)).scalars().all())
    targets: list[tuple[Product, Path]] = []
    skipped: list[uuid.UUID] = []
    for product in products:
        path = chemin_photo_locale(product.id)
        if path is None or not path.is_file():
            continue
        if _needs_backfill(product, force):
            targets.append((product, path))
        else:
            skipped.append(product.id)
    return targets, skipped


def estimate_targets_cost(
    targets: list[tuple[Product, Path]],
) -> tuple[int, list[int], float, list[uuid.UUID]]:
    """Pixels after downscale, billed-pixel list, USD estimate, unreadable ids."""
    total_pixels = 0
    billed: list[int] = []
    unreadable: list[uuid.UUID] = []
    for product, path in targets:
        try:
            prepared = prepare_image_for_embedding(path.read_bytes())
        except ImageEmbeddingError:
            unreadable.append(product.id)
            continue
        width, height = prepared.size
        total_pixels += width * height
        billed.append(billable_pixel_count(width, height))
    return total_pixels, billed, estimate_voyage_image_cost_usd(billed), unreadable


def _print_estimate(
    targets: list[tuple[Product, Path]],
    skipped: list[uuid.UUID],
    total_pixels: int,
    cost_usd: float,
    unreadable: list[uuid.UUID],
) -> None:
    print(f"products_to_embed={len(targets)}", flush=True)
    print(f"skipped_already_current={len(skipped)}", flush=True)
    print(f"unreadable={len(unreadable)}", flush=True)
    if unreadable:
        print(
            "unreadable_product_ids="
            + ",".join(str(item) for item in unreadable),
            flush=True,
        )
    print(f"pixels_after_downscale={total_pixels}", flush=True)
    print(
        f"estimated_cost_usd={cost_usd:.8f} "
        f"(Voyage multimodal $0.60/billion pixels; "
        f"min $0.00003 max $0.0012 per image; "
        f"https://docs.voyageai.com/docs/pricing)",
        flush=True,
    )


async def backfill_product_image_embeddings(
    db: AsyncSession,
    merchant_id: uuid.UUID | None = None,
    *,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, list[uuid.UUID]]:
    targets, skipped = await list_backfill_targets(db, merchant_id, force)
    total_pixels, _billed, cost_usd, unreadable = estimate_targets_cost(targets)
    _print_estimate(targets, skipped, total_pixels, cost_usd, unreadable)
    summary: dict[str, list[uuid.UUID]] = {
        "ok": [],
        "skipped": list(skipped),
        "failed": list(unreadable),
    }
    if dry_run:
        print("dry_run=1 (no writes)", flush=True)
        return summary

    for product, path in targets:
        if product.id in unreadable:
            continue
        content = path.read_bytes()
        mime = _mime_for_path(path)
        try:
            vector = await embed_catalogue_image(content, mime)
        except ImageEmbeddingError:
            logger.warning(
                "Image embedding failed for product_id=%s; continuing",
                product.id,
            )
            summary["failed"].append(product.id)
            continue
        product.image_embedding = vector
        product.image_embedding_model = settings.voyage_image_model
        await db.commit()
        summary["ok"].append(product.id)
        print(f"ok product_id={product.id}", flush=True)

    print("---", flush=True)
    print(f"ok={len(summary['ok'])}", flush=True)
    print(f"skipped={len(summary['skipped'])}", flush=True)
    print(f"failed={len(summary['failed'])}", flush=True)
    if summary["failed"]:
        print(
            "failed_product_ids="
            + ",".join(str(item) for item in summary["failed"]),
            flush=True,
        )
    return summary


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill product image embeddings from local catalogue photos."
    )
    parser.add_argument(
        "--merchant-id",
        type=uuid.UUID,
        default=None,
        help="Limit to one merchant. Default: every merchant.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the count, pixels, and cost estimate, then exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute even when the stored model matches the setting.",
    )
    return parser.parse_args()


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _parse_args()
    async with AsyncSessionLocal() as db:
        await backfill_product_image_embeddings(
            db,
            args.merchant_id,
            dry_run=args.dry_run,
            force=args.force,
        )


if __name__ == "__main__":
    asyncio.run(_main())
