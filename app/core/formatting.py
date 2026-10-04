"""Shared display formatting. No locale/currency switch yet."""

from decimal import Decimal
from typing import Any


def format_fcfa(price: Any) -> str:
    value = int(Decimal(str(price)))
    return f"{value:,}".replace(",", " ") + " FCFA"
