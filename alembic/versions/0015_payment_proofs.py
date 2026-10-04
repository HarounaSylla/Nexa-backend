"""Add proof_received payment status and inbound_images.

Revision ID: 0015_payment_proofs
Revises: 0014_payment_link_sent_at
Create Date: 2026-10-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_payment_proofs"
down_revision: Union[str, Sequence[str], None] = "0014_payment_link_sent_at"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            sa.text(
                "ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'proof_received'"
            )
        )
    op.create_table(
        "inbound_images",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("whatsapp_message_id", sa.Text(), nullable=False),
        sa.Column("media_path", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("classification", sa.Text(), nullable=False),
        sa.Column("detected_amount", sa.Numeric(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"]),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"]),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("whatsapp_message_id"),
    )
    op.create_index(
        "ix_inbound_images_merchant_id", "inbound_images", ["merchant_id"]
    )
    op.create_index(
        "ix_inbound_images_conversation_id",
        "inbound_images",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbound_images_conversation_id", table_name="inbound_images")
    op.drop_index("ix_inbound_images_merchant_id", table_name="inbound_images")
    op.drop_table("inbound_images")
    op.execute(
        sa.text(
            "UPDATE orders SET payment_status = 'pending' "
            "WHERE payment_status = 'proof_received'"
        )
    )
    # PostgreSQL cannot DROP a value from a native enum. Recreate the type.
    # The column default must be dropped first: Postgres cannot recast it.
    op.execute(sa.text("ALTER TYPE payment_status RENAME TO payment_status_old"))
    op.execute(sa.text("CREATE TYPE payment_status AS ENUM ('pending', 'paid')"))
    op.execute(sa.text("ALTER TABLE orders ALTER COLUMN payment_status DROP DEFAULT"))
    op.execute(
        sa.text(
            "ALTER TABLE orders ALTER COLUMN payment_status "
            "TYPE payment_status USING payment_status::text::payment_status"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE orders ALTER COLUMN payment_status "
            "SET DEFAULT 'pending'::payment_status"
        )
    )
    op.execute(sa.text("DROP TYPE payment_status_old"))
