"""Price string parsing. Returns integer cents, or None on any ambiguity.

Never guesses. A malformed or missing price is None, not 0 -- this matches
the adapter contract rule in models.py: "a missing price is None, not 0".
"""

from __future__ import annotations

import re

_PRICE_RE = re.compile(r"\$?\s*([0-9][0-9,]*)(?:\.(\d{1,2}))?")


def parse_price_cents(raw: str | None) -> int | None:
    """Parse a US-formatted price string (e.g. "$130.00", "6.99", "1,299")
    into integer cents. Returns None if no plausible price is found.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    match = _PRICE_RE.search(text)
    if not match:
        return None
    dollars_str = match.group(1).replace(",", "")
    cents_str = match.group(2)
    try:
        dollars = int(dollars_str)
    except ValueError:
        return None
    cents = int(cents_str.ljust(2, "0")) if cents_str else 0
    total = dollars * 100 + cents
    if total <= 0:
        return None
    return total


def format_cents(cents: int | None) -> str | None:
    """Inverse of parse_price_cents, for logging/debugging only."""
    if cents is None:
        return None
    return f"${cents // 100}.{cents % 100:02d}"
