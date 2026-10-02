"""Per-conversation ledger of product photos already sent on WhatsApp.

Revision ID: 0011_sent_product_images
Revises: 0010_order_number
Create Date: 2026-10-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_sent_product_images"
down_revision: Union[str, Sequence[str], None] = "0010_order_number"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sent_product_images",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "product_id",
            name="uq_sent_product_images_conversation_product",
        ),
    )
    op.create_index(
        "ix_sent_product_images_conversation_id",
        "sent_product_images",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sent_product_images_conversation_id",
        table_name="sent_product_images",
    )
    op.drop_table("sent_product_images")
