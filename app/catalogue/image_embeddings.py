"""Catalogue image embeddings via Voyage multimodal (separate from text RAG).

Text search uses `voyage-4-lite` on `Product.embedding`. Image search uses
`voyage-multimodal-3.5` on `Product.image_embedding`. The two spaces must
never be mixed.

Voyage recommends `input_type='document'` for corpus images and
`input_type='query'` for the search image (asymmetric retrieval). See
https://docs.voyageai.com/docs/multimodal-embeddings
"""

from __future__ import annotations

import asyncio
import io

import voyageai
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import settings

IMAGE_EMBEDDING_DIMENSION = 1024
MAX_IMAGE_EDGE_PX = 1024
MAX_INPUT_BYTES = 20 * 1024 * 1024
JPEG_QUALITY = 85
# Voyage bills at $0.60 / billion pixels, with a 50k-pixel floor and a
# 2M-pixel ceiling per image:
# https://docs.voyageai.com/docs/pricing
VOYAGE_IMAGE_USD_PER_BILLION_PIXELS = 0.60
VOYAGE_MIN_BILLABLE_PIXELS = 50_000
VOYAGE_MAX_BILLABLE_PIXELS = 2_000_000

_client: voyageai.Client | None = None


class ImageEmbeddingError(ValueError):
    """Raised when an image cannot be prepared or Voyage fails."""


def _get_client() -> voyageai.Client:
    global _client
    if _client is None:
        _client = voyageai.Client(
            api_key=settings.voyage_api_key or None,
            max_retries=8,
        )
    return _client


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return image.convert("RGB")


def _downscale(image: Image.Image) -> Image.Image:
    width, height = image.size
    longest = max(width, height)
    if longest <= MAX_IMAGE_EDGE_PX:
        return image
    scale = MAX_IMAGE_EDGE_PX / longest
    new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(new_size, Image.Resampling.LANCZOS)


def prepare_image_for_embedding(content: bytes) -> Image.Image:
    """Decode, apply EXIF orientation, flatten to RGB, downscale to ≤1024 px.

    Returns an RGB image. Does not call Voyage.
    """
    if not content:
        raise ImageEmbeddingError("Image is empty")
    if len(content) > MAX_INPUT_BYTES:
        raise ImageEmbeddingError("Image is larger than 20 MB")
    try:
        with Image.open(io.BytesIO(content)) as opened:
            opened.load()
            oriented = ImageOps.exif_transpose(opened)
            rgb = _flatten_to_rgb(oriented if oriented is not None else opened)
            return _downscale(rgb)
    except ImageEmbeddingError:
        raise
    except UnidentifiedImageError as exc:
        raise ImageEmbeddingError("Image could not be decoded") from exc
    except Image.DecompressionBombError as exc:
        raise ImageEmbeddingError("Image is oversized") from exc
    except Exception as exc:
        raise ImageEmbeddingError("Image could not be decoded") from exc


def encode_prepared_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue()


def billable_pixel_count(width: int, height: int) -> int:
    """Pixels Voyage bills for one image after our downscale."""
    return min(
        VOYAGE_MAX_BILLABLE_PIXELS,
        max(VOYAGE_MIN_BILLABLE_PIXELS, width * height),
    )


def estimate_voyage_image_cost_usd(billable_pixels: list[int]) -> float:
    """$0.60 per billion billed pixels (min $0.00003, max $0.0012 / image)."""
    return (
        sum(billable_pixels) / 1_000_000_000 * VOYAGE_IMAGE_USD_PER_BILLION_PIXELS
    )


def _embed_sync(content: bytes, input_type: str) -> list[float]:
    prepared = prepare_image_for_embedding(content)
    jpeg = encode_prepared_jpeg(prepared)
    try:
        with Image.open(io.BytesIO(jpeg)) as image:
            image.load()
            rgb = image.convert("RGB")
            result = _get_client().multimodal_embed(
                inputs=[[rgb]],
                model=settings.voyage_image_model,
                input_type=input_type,
                output_dimension=IMAGE_EMBEDDING_DIMENSION,
            )
    except ImageEmbeddingError:
        raise
    except Exception as exc:
        raise ImageEmbeddingError("Voyage multimodal embed failed") from exc
    embeddings = getattr(result, "embeddings", None)
    if not embeddings or len(embeddings[0]) != IMAGE_EMBEDDING_DIMENSION:
        raise ImageEmbeddingError("Voyage returned an unexpected embedding")
    return embeddings[0]


async def embed_catalogue_image(content: bytes, mime: str) -> list[float]:
    """Embed a merchant catalogue photo (`input_type='document'`)."""
    del mime
    return await asyncio.to_thread(_embed_sync, content, "document")


async def embed_query_image(content: bytes, mime: str) -> list[float]:
    """Embed a search photo (`input_type='query'`)."""
    del mime
    return await asyncio.to_thread(_embed_sync, content, "query")
