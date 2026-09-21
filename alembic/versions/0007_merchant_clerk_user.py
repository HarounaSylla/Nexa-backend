"""Add merchants.clerk_user_id for Clerk auth.

Revision ID: 0007_merchant_clerk_user
Revises: 0006_products_french_fts
Create Date: 2026-09-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_merchant_clerk_user"
down_revision: Union[str, Sequence[str], None] = "0006_products_french_fts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "merchants",
        sa.Column("clerk_user_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_merchants_clerk_user_id",
        "merchants",
        ["clerk_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_merchants_clerk_user_id", table_name="merchants")
    op.drop_column("merchants", "clerk_user_id")
