"""Reusable merchant payment links and order label snapshot.

Revision ID: 0016_merchant_payment_links
Revises: 0015_payment_proofs
Create Date: 2026-10-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_merchant_payment_links"
down_revision: Union[str, Sequence[str], None] = "0015_payment_proofs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merchant_payment_links",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_merchant_payment_links_merchant_id",
        "merchant_payment_links",
        ["merchant_id"],
    )
    op.create_index(
        "uq_merchant_payment_links_merchant_id_lower_label",
        "merchant_payment_links",
        ["merchant_id", sa.text("lower(label)")],
        unique=True,
    )
    op.add_column(
        "orders",
        sa.Column("payment_link_label", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "payment_link_label")
    op.drop_index(
        "uq_merchant_payment_links_merchant_id_lower_label",
        table_name="merchant_payment_links",
    )
    op.drop_index(
        "ix_merchant_payment_links_merchant_id",
        table_name="merchant_payment_links",
    )
    op.drop_table("merchant_payment_links")
