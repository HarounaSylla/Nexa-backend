"""Add delivery_zones and orders.city.

Revision ID: 0005_delivery_zones
Revises: 0004_agent_conversations
Create Date: 2026-09-08
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_delivery_zones"
down_revision: Union[str, Sequence[str], None] = "0004_agent_conversations"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delivery_zones",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("city", sa.String(), nullable=False),
        sa.Column("city_normalized", sa.String(), nullable=False),
        sa.Column(
            "available",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column("min_delivery_hours", sa.Integer(), nullable=False),
        sa.Column("max_delivery_hours", sa.Integer(), nullable=False),
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
            "max_delivery_hours >= min_delivery_hours",
            name="ck_delivery_zones_hours",
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "merchant_id",
            "city_normalized",
            name="uq_delivery_zones_merchant_city",
        ),
    )
    op.create_index(
        "ix_delivery_zones_merchant_id", "delivery_zones", ["merchant_id"]
    )
    op.add_column("orders", sa.Column("city", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "city")
    op.drop_index("ix_delivery_zones_merchant_id", table_name="delivery_zones")
    op.drop_table("delivery_zones")
