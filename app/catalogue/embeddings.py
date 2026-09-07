import voyageai

from app.catalogue.models import Product
from app.core.config import settings

_client: voyageai.Client | None = None

# Explicit 1024-d output so a Voyage default change cannot silently resize
# our pgvector column. input_type is required for asymmetric search: the
# model encodes queries and documents differently when it is set.
_OUTPUT_DIMENSION = 1024


def _get_client() -> voyageai.Client:
    global _client
    if _client is None:
        _client = voyageai.Client(
            api_key=settings.voyage_api_key or None,
            max_retries=8,
        )
    return _client


def embed_documents(texts: list[str]) -> list[list[float]]:
    """Embed product text for indexing. Uses input_type='document'."""
    result = _get_client().embed(
        texts=texts,
        model=settings.voyage_model,
        input_type="document",
        output_dimension=_OUTPUT_DIMENSION,
    )
    return result.embeddings


def embed_query(text: str) -> list[float]:
    """Embed a customer search phrase. Uses input_type='query'."""
    result = _get_client().embed(
        texts=[text],
        model=settings.voyage_model,
        input_type="query",
        output_dimension=_OUTPUT_DIMENSION,
    )
    return result.embeddings[0]


def product_index_text(product: Product) -> str:
    return f"{product.name}. {product.description}. Category: {product.category}."
