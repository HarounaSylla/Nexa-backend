"""Add orders.payment_link_sent_at.

Revision ID: 0014_payment_link_sent_at
Revises: 0013_backfill_cod_paid
Create Date: 2026-10-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014_payment_link_sent_at"
down_revision: Union[str, Sequence[str], None] = "0013_backfill_cod_paid"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("payment_link_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orders", "payment_link_sent_at")
