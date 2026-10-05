"""Normalise conversation and order phones to E.164 with a leading +.

Revision ID: 0017_normalize_phones
Revises: 0016_merchant_payment_links
Create Date: 2026-10-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017_normalize_phones"
down_revision: Union[str, Sequence[str], None] = "0016_merchant_payment_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# SQL equivalent of app.core.phone.normalize_phone for 221… / 00… / spaced
# forms. Values that cannot be normalised (letters, too short/long) are left
# untouched. Duplicate rows are not merged (part 2).
_CANONICAL = r"^\+[0-9]{8,15}$"


def normalize_phones_sql(table: str) -> str:
    if table not in {"conversations", "orders"}:
        raise ValueError(f"Unsupported table {table}")
    return f"""
UPDATE {table} AS t
SET customer_phone = n.canonical
FROM (
    SELECT
        src.id,
        '+' || src.digit_chars AS canonical
    FROM (
        SELECT
            cleaned.id,
            CASE
                WHEN cleaned.cleaned LIKE '+%' THEN regexp_replace(cleaned.cleaned, '\\D', '', 'g')
                WHEN cleaned.cleaned ~ '^[0-9]+$' THEN
                    CASE
                        WHEN length(cleaned.cleaned) = 9 AND cleaned.cleaned LIKE '7%'
                            THEN '221' || cleaned.cleaned
                        ELSE cleaned.cleaned
                    END
                ELSE NULL
            END AS digit_chars
        FROM (
            SELECT
                stripped.id,
                CASE
                    WHEN stripped.stripped LIKE '00%' THEN '+' || substr(stripped.stripped, 3)
                    ELSE stripped.stripped
                END AS cleaned
            FROM (
                SELECT
                    id,
                    regexp_replace(customer_phone, '[\\s.\\-()]+', '', 'g') AS stripped
                FROM {table}
            ) AS stripped
        ) AS cleaned
    ) AS src
    WHERE src.digit_chars ~ '^[0-9]{{8,15}}$'
) AS n
WHERE t.id = n.id
  AND t.customer_phone IS DISTINCT FROM n.canonical
"""


def _noncanonical_count(connection, table: str) -> int:
    return int(
        connection.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} WHERE customer_phone !~ :pat"
            ),
            {"pat": _CANONICAL},
        ).scalar()
        or 0
    )


def upgrade() -> None:
    bind = op.get_bind()
    for table in ("conversations", "orders"):
        before = _noncanonical_count(bind, table)
        bind.execute(sa.text(normalize_phones_sql(table)))
        after = _noncanonical_count(bind, table)
        print(f"0017 {table}: non-canonical before={before} after={after}")


def downgrade() -> None:
    # Original spellings are discarded; the backfill cannot be reversed.
    pass
