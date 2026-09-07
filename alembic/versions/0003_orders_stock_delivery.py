"""Create order/stock/delivery enums and tables.

Revision ID: 0003_orders_stock_delivery
Revises: 0002_catalogue_tables
Create Date: 2026-09-07
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_orders_stock_delivery"
down_revision: Union[str, Sequence[str], None] = "0002_catalogue_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

order_status = postgresql.ENUM(
    "created",
    "deliverer_assigned",
    "delivered",
    "cancelled",
    name="order_status",
)
payment_method = postgresql.ENUM(
    "cash_on_delivery",
    "online",
    name="payment_method",
)
payment_status = postgresql.ENUM(
    "pending",
    "paid",
    name="payment_status",
)


def upgrade() -> None:
    op.create_table(
        "deliverers",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deliverers_merchant_id", "deliverers", ["merchant_id"])

    op.create_table(
        "orders",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_phone", sa.String(), nullable=False),
        sa.Column(
            "status",
            order_status,
            server_default=sa.text("'created'"),
            nullable=False,
        ),
        sa.Column("payment_method", payment_method, nullable=False),
        sa.Column(
            "payment_status",
            payment_status,
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column("payment_link", sa.String(), nullable=True),
        sa.Column("delivery_address", sa.Text(), nullable=False),
        sa.Column("deliverer_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.ForeignKeyConstraint(["deliverer_id"], ["deliverers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_orders_merchant_id", "orders", ["merchant_id"])
    op.create_index("ix_orders_customer_phone", "orders", ["customer_phone"])
    op.create_index("ix_orders_status", "orders", ["status"])

    op.create_table(
        "order_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])

    op.create_table(
        "stock_movements",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("movement_type", sa.String(), nullable=False),
        sa.Column("quantity_delta", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_stock_movements_product_id", "stock_movements", ["product_id"])
    op.create_index("ix_stock_movements_order_id", "stock_movements", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_stock_movements_order_id", table_name="stock_movements")
    op.drop_index("ix_stock_movements_product_id", table_name="stock_movements")
    op.drop_table("stock_movements")
    op.drop_index("ix_order_items_order_id", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_index("ix_orders_customer_phone", table_name="orders")
    op.drop_index("ix_orders_merchant_id", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_deliverers_merchant_id", table_name="deliverers")
    op.drop_table("deliverers")
    payment_status.drop(op.get_bind(), checkfirst=True)
    payment_method.drop(op.get_bind(), checkfirst=True)
    order_status.drop(op.get_bind(), checkfirst=True)
