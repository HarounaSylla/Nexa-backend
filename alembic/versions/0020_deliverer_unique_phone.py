"""Normalise deliverer phones and unique (merchant_id, phone).

Revision ID: 0020_deliverer_unique_phone
Revises: 0019_inbound_image_deletion
Create Date: 2026-10-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0020_deliverer_unique_phone"
down_revision: Union[str, Sequence[str], None] = "0019_inbound_image_deletion"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_deliverers_merchant_phone"
_CANONICAL = r"^\+[0-9]{8,15}$"

# SQL equivalent of app.core.phone.normalize_phone (same rule as 0017).
# Values that cannot be normalised are left untouched.
NORMALIZE_DELIVERER_PHONES_SQL = """
UPDATE deliverers AS t
SET phone = n.canonical
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
                    regexp_replace(phone, '[\\s.\\-()]+', '', 'g') AS stripped
                FROM deliverers
            ) AS stripped
        ) AS cleaned
    ) AS src
    WHERE src.digit_chars ~ '^[0-9]{8,15}$'
) AS n
WHERE t.id = n.id
  AND t.phone IS DISTINCT FROM n.canonical
"""

DUPLICATES_SQL = """
SELECT
    merchant_id::text,
    phone,
    string_agg(id::text, ', ' ORDER BY id) AS ids,
    COUNT(*) AS n
FROM deliverers
GROUP BY merchant_id, phone
HAVING COUNT(*) > 1
ORDER BY merchant_id, phone
"""


def noncanonical_count(connection) -> int:
    return int(
        connection.execute(
            sa.text("SELECT COUNT(*) FROM deliverers WHERE phone !~ :pat"),
            {"pat": _CANONICAL},
        ).scalar()
        or 0
    )


def list_duplicate_phones(connection) -> list[tuple[str, str, str, int]]:
    return list(connection.execute(sa.text(DUPLICATES_SQL)).fetchall())


def format_duplicate_error(rows: Sequence) -> str:
    listing = "; ".join(
        f"merchant={row[0]} phone={row[1]} ids=[{row[2]}] n={row[3]}"
        for row in rows
    )
    return (
        "0020_deliverer_unique_phone: duplicate (merchant_id, phone) rows "
        "exist; refusing to create the unique index (deliverers are "
        f"referenced by orders). {listing}"
    )


def raise_if_duplicate_rows(rows: Sequence) -> None:
    if rows:
        raise RuntimeError(format_duplicate_error(rows))


def raise_if_duplicates(connection) -> None:
    raise_if_duplicate_rows(list_duplicate_phones(connection))


def upgrade() -> None:
    bind = op.get_bind()
    before = noncanonical_count(bind)
    bind.execute(sa.text(NORMALIZE_DELIVERER_PHONES_SQL))
    after = noncanonical_count(bind)
    print(
        f"0020 deliverers: non-canonical before={before} after={after} "
        f"(left untouched={after})"
    )
    raise_if_duplicates(bind)
    op.create_index(
        INDEX_NAME,
        "deliverers",
        ["merchant_id", "phone"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="deliverers")
