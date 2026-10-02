"""Add order numbers and optional orders.conversation_id.

Revision ID: 0010_order_number
Revises: 0009_merchant_whatsapp_phone
Create Date: 2026-10-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_order_number"
down_revision: Union[str, Sequence[str], None] = "0009_merchant_whatsapp_phone"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "merchants",
        sa.Column(
            "next_order_number",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.add_column("orders", sa.Column("order_number", sa.Integer(), nullable=True))
    op.add_column(
        "orders",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_orders_conversation_id",
        "orders",
        ["conversation_id"],
    )
    op.create_foreign_key(
        "fk_orders_conversation_id",
        "orders",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        """
        WITH numbered AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY merchant_id
                       ORDER BY created_at ASC, id ASC
                   ) AS n
            FROM orders
        )
        UPDATE orders
        SET order_number = numbered.n
        FROM numbered
        WHERE orders.id = numbered.id
        """
    )
    op.execute(
        """
        UPDATE merchants
        SET next_order_number = COALESCE(
            (
                SELECT MAX(order_number)
                FROM orders
                WHERE orders.merchant_id = merchants.id
            ),
            0
        ) + 1
        """
    )
    op.alter_column("orders", "order_number", existing_type=sa.Integer(), nullable=False)
    op.create_unique_constraint(
        "uq_orders_merchant_order_number",
        "orders",
        ["merchant_id", "order_number"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_orders_merchant_order_number", "orders", type_="unique"
    )
    op.drop_constraint("fk_orders_conversation_id", "orders", type_="foreignkey")
    op.drop_index("ix_orders_conversation_id", table_name="orders")
    op.drop_column("orders", "conversation_id")
    op.drop_column("orders", "order_number")
    op.drop_column("merchants", "next_order_number")
