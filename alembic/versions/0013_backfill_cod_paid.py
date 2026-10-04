"""Mark delivered cash-on-delivery orders as paid.

Revision ID: 0013_backfill_cod_paid
Revises: 0012_merchant_prefs
Create Date: 2026-10-04
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0013_backfill_cod_paid"
down_revision: Union[str, Sequence[str], None] = "0012_merchant_prefs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE orders SET payment_status = 'paid'
        WHERE status = 'delivered'
          AND payment_method = 'cash_on_delivery'
          AND payment_status = 'pending'
        """
    )


def downgrade() -> None:
    # Previous pending/paid values cannot be reconstructed: a delivered COD
    # order that was pending before this revision looks the same as one that
    # was already paid.
    pass
