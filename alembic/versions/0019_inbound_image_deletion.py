"""Make inbound image files deletable; keep the row.

Revision ID: 0019_inbound_image_deletion
Revises: 0018_unique_open_conversation
Create Date: 2026-10-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0019_inbound_image_deletion"
down_revision: Union[str, Sequence[str], None] = "0018_unique_open_conversation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "inbound_images",
        "media_path",
        existing_type=sa.Text(),
        nullable=True,
    )
    op.add_column(
        "inbound_images",
        sa.Column("media_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE inbound_images SET media_path = '' WHERE media_path IS NULL"
        )
    )
    op.drop_column("inbound_images", "media_deleted_at")
    op.alter_column(
        "inbound_images",
        "media_path",
        existing_type=sa.Text(),
        nullable=False,
    )
