"""GIN index for French full-text search on products.

Revision ID: 0006_products_french_fts
Revises: 0005_delivery_zones
Create Date: 2026-09-08
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0006_products_french_fts"
down_revision: Union[str, Sequence[str], None] = "0005_delivery_zones"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_products_french_fts ON products USING GIN (
            to_tsvector(
                'french',
                coalesce(name, '') || ' ' || coalesce(description, '')
                || ' ' || coalesce(category, '')
            )
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_products_french_fts")
