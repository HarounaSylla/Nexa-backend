"""Add merchants.whatsapp_phone_number_id for webhook routing.

Revision ID: 0009_merchant_whatsapp_phone
Revises: 0008_notifications
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_merchant_whatsapp_phone"
down_revision: Union[str, Sequence[str], None] = "0008_notifications"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "merchants",
        sa.Column("whatsapp_phone_number_id", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_merchants_whatsapp_phone_number_id",
        "merchants",
        ["whatsapp_phone_number_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_merchants_whatsapp_phone_number_id", table_name="merchants"
    )
    op.drop_column("merchants", "whatsapp_phone_number_id")
