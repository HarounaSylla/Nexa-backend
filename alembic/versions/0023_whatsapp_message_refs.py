"""Outbound/inbound WhatsApp wamids for quoted-reply resolution.

Revision ID: 0023_whatsapp_message_refs
Revises: 0022_inbound_image_match
Create Date: 2026-10-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_whatsapp_message_refs"
down_revision: Union[str, Sequence[str], None] = "0022_inbound_image_match"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_message_refs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("wamid", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("reply_to_wamid", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
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
            ["message_id"],
            ["messages.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id",
            "wamid",
            name="uq_whatsapp_message_refs_conversation_wamid",
        ),
    )
    op.create_index(
        "ix_whatsapp_message_refs_wamid",
        "whatsapp_message_refs",
        ["wamid"],
    )
    op.create_index(
        "ix_whatsapp_message_refs_conversation_id",
        "whatsapp_message_refs",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_message_refs_conversation_id",
        table_name="whatsapp_message_refs",
    )
    op.drop_index(
        "ix_whatsapp_message_refs_wamid",
        table_name="whatsapp_message_refs",
    )
    op.drop_table("whatsapp_message_refs")
