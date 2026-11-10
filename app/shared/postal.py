"""Postal / ZIP code checks for client addresses.

PayFlow started Canada-only, so every postal code had to look like ``A1A 1A1``
and a client anywhere else could not be saved. The check now follows the
client's country; with no country on file Canada stays the default.
"""

from __future__ import annotations

import re

_CANADA = re.compile(r"^[A-Z]\d[A-Z] \d[A-Z]\d$")
_UNITED_STATES = re.compile(r"^\d{5}(-\d{4})?$")
# Anywhere else: letters, digits, spaces and hyphens, a sensible length.
_GENERIC = re.compile(r"^(?=.*[A-Z0-9])[A-Z0-9][A-Z0-9 \-]{1,10}[A-Z0-9]$")

_CANADA_NAMES = {"", "CA", "CAN", "CANADA"}
_US_NAMES = {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}


def _country_key(country: str | None) -> str:
    return re.sub(r"\s+", " ", (country or "").strip().upper())


def normalize_postal_code(raw: str | None, country: str | None = None) -> str:
    """Upper-case, trimmed; a Canadian code typed without the space gets it back."""
    text = (raw or "").strip().upper()
    if not text:
        return ""
    if _country_key(country) in _CANADA_NAMES:
        compact = re.sub(r"\s+", "", text)
        if len(compact) == 6:
            return f"{compact[:3]} {compact[3:]}"
    return text


def is_valid_postal_code(raw: str | None, country: str | None = None) -> bool:
    """Blank is valid (the field is optional). ``country=None`` means Canada."""
    cleaned = normalize_postal_code(raw, country)
    if not cleaned:
        return True
    key = _country_key(country)
    if key in _CANADA_NAMES:
        return bool(_CANADA.match(cleaned))
    if key in _US_NAMES:
        return bool(_UNITED_STATES.match(cleaned))
    return bool(_GENERIC.match(cleaned))


def postal_code_hint(country: str | None = None) -> str:
    key = _country_key(country)
    if key in _CANADA_NAMES:
        return "Postal code must match format A1A 1A1 (letter-number-letter space number-letter-number)"
    if key in _US_NAMES:
        return "ZIP code must be 5 digits, or ZIP+4 (12345-6789)"
    return "Postal code may contain only letters, numbers, spaces and hyphens (3 to 12 characters)"
