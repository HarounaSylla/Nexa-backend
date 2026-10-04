"""Per-merchant shop preferences for the WhatsApp agent.

Revision ID: 0012_merchant_prefs
Revises: 0011_sent_product_images
Create Date: 2026-10-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_merchant_prefs"
down_revision: Union[str, Sequence[str], None] = "0011_sent_product_images"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "merchant_preferences",
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "accepts_cash_on_delivery",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "accepts_online_payment",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("shop_address", sa.Text(), nullable=True),
        sa.Column("opening_hours", sa.Text(), nullable=True),
        sa.Column("return_policy", sa.Text(), nullable=True),
        sa.Column(
            "timezone",
            sa.String(),
            server_default=sa.text("'Africa/Dakar'"),
            nullable=False,
        ),
        sa.Column("delivery_fee_note", sa.Text(), nullable=True),
        sa.Column("extra_info", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "accepts_cash_on_delivery OR accepts_online_payment",
            name="ck_merchant_preferences_one_payment_method",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("merchant_id"),
    )


def downgrade() -> None:
    op.drop_table("merchant_preferences")
