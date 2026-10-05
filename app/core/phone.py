"""Canonical customer phone numbers (E.164 with a leading +)."""

from __future__ import annotations

import logging
import re

from app.core.config import settings

logger = logging.getLogger(__name__)

_PUNCTUATION = re.compile(r"[\s.\-()]+")
_NON_DIGIT = re.compile(r"\D")


class InvalidPhoneNumberError(ValueError):
    def __init__(self) -> None:
        super().__init__("Invalid phone number")


def _country_calling_code() -> str:
    return (settings.default_country_calling_code or "221").strip().lstrip("+")


def normalize_phone(raw: str) -> str:
    """Return E.164 with a leading +. Raises InvalidPhoneNumberError if invalid."""
    if raw is None:
        raise InvalidPhoneNumberError()
    cleaned = _PUNCTUATION.sub("", str(raw).strip())
    if not cleaned:
        raise InvalidPhoneNumberError()
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]

    if cleaned.startswith("+"):
        digits = _NON_DIGIT.sub("", cleaned)
    else:
        if _NON_DIGIT.search(cleaned):
            raise InvalidPhoneNumberError()
        digits = cleaned
        cc = _country_calling_code()
        if len(digits) == 9 and digits.startswith("7"):
            digits = f"{cc}{digits}"

    if not digits.isdigit() or len(digits) < 8 or len(digits) > 15:
        raise InvalidPhoneNumberError()
    return f"+{digits}"


def normalize_phone_webhook(raw: str) -> str:
    """Never raise: log and fall back to '+' plus extracted digits."""
    try:
        return normalize_phone(raw)
    except ValueError:
        digits = _NON_DIGIT.sub("", raw or "")
        fallback = f"+{digits}" if digits else "+"
        logger.warning(
            "Could not normalise inbound phone %r; falling back to %s",
            raw,
            fallback,
        )
        return fallback


def try_normalize_phone(raw: str) -> str:
    """Canonical form when possible, otherwise webhook fallback."""
    try:
        return normalize_phone(raw)
    except ValueError:
        return normalize_phone_webhook(raw)
