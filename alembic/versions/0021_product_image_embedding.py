"""Add products.image_embedding (pgvector) and image_embedding_model.

Revision ID: 0021_product_image_embedding
Revises: 0020_deliverer_unique_phone
Create Date: 2026-10-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0021_product_image_embedding"
down_revision: Union[str, Sequence[str], None] = "0020_deliverer_unique_phone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column("image_embedding", Vector(1024), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("image_embedding_model", sa.String(), nullable=True),
    )
    # No ANN index yet. Sequential scan is fine for small catalogues.
    # Backlog: HNSW on products.image_embedding when catalogues grow.


def downgrade() -> None:
    op.drop_column("products", "image_embedding_model")
    op.drop_column("products", "image_embedding")
