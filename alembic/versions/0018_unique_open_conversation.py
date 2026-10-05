"""Keep at most one open conversation per merchant + phone.

Revision ID: 0018_unique_open_conversation
Revises: 0017_normalize_phones
Create Date: 2026-10-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018_unique_open_conversation"
down_revision: Union[str, Sequence[str], None] = "0017_normalize_phones"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_conversations_one_open_per_phone"

CLOSE_DUPLICATE_OPENS_SQL = """
WITH ranked AS (
    SELECT
        id,
        row_number() OVER (
            PARTITION BY merchant_id, customer_phone
            ORDER BY
                CASE WHEN status = 'escalated' THEN 0 ELSE 1 END,
                updated_at DESC,
                id DESC
        ) AS rn
    FROM conversations
    WHERE status IN ('active', 'escalated')
)
UPDATE conversations
SET status = 'closed'
WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
"""


def upgrade() -> None:
    bind = op.get_bind()
    result = bind.execute(sa.text(CLOSE_DUPLICATE_OPENS_SQL + " RETURNING id"))
    closed = len(result.fetchall())
    print(f"0018 closed {closed} duplicate open conversation(s)")
    op.create_index(
        INDEX_NAME,
        "conversations",
        ["merchant_id", "customer_phone"],
        unique=True,
        postgresql_where=sa.text("status IN ('active', 'escalated')"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="conversations")
