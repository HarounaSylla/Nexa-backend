"""Add inbound_images visual-search columns.

Revision ID: 0022_inbound_image_match
Revises: 0021_product_image_embedding
Create Date: 2026-10-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_inbound_image_match"
down_revision: Union[str, Sequence[str], None] = "0021_product_image_embedding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inbound_images",
        sa.Column("matched_product_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "inbound_images",
        sa.Column("match_level", sa.Text(), nullable=True),
    )
    op.add_column(
        "inbound_images",
        sa.Column("match_candidates", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        "fk_inbound_images_matched_product_id",
        "inbound_images",
        "products",
        ["matched_product_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_inbound_images_matched_product_id",
        "inbound_images",
        type_="foreignkey",
    )
    op.drop_column("inbound_images", "match_candidates")
    op.drop_column("inbound_images", "match_level")
    op.drop_column("inbound_images", "matched_product_id")
