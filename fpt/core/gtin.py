"""GTIN normalization and GS1 mod-10 checksum validation.

Per architecture doc 4.2: "all digit strings found, left-padded to 14, GS1
mod-10 checksum validated; invalid ones dropped with a warning. Field names
are ignored (Academy labels a 13-digit value `gtin8`)."
"""

from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"\d+")


def _gs1_checksum_valid(gtin14: str) -> bool:
    """GS1 mod-10: sum digits from the right, alternating weights 3/1
    (rightmost non-check digit gets weight 3), check digit makes the total
    a multiple of 10.
    """
    if len(gtin14) != 14 or not gtin14.isdigit():
        return False
    digits = [int(d) for d in gtin14]
    check_digit = digits[-1]
    body = digits[:-1]
    total = 0
    # Rightmost body digit (index -1 of body) gets weight 3.
    for i, d in enumerate(reversed(body)):
        weight = 3 if i % 2 == 0 else 1
        total += d * weight
    computed_check = (10 - (total % 10)) % 10
    return computed_check == check_digit


def normalize_gtin(raw: str) -> str | None:
    """Extract digits from `raw`, left-pad to 14, validate checksum.

    Returns the checksum-valid 14-digit GTIN string, or None if the input
    is not a plausible barcode (wrong length after extraction, or fails the
    GS1 mod-10 check).
    """
    digits = "".join(_DIGITS_RE.findall(raw or ""))
    if not digits:
        return None
    # Plausible barcode lengths: UPC-A(12), EAN-13/GTIN-13, GTIN-14, GTIN-8.
    if len(digits) not in (8, 12, 13, 14):
        return None
    padded = digits.zfill(14)
    if not _gs1_checksum_valid(padded):
        return None
    return padded


def normalize_all(raw_values: list[str]) -> list[str]:
    """Normalize a field-name-agnostic list of candidate barcode strings,
    dropping invalid ones. Field names are deliberately ignored by callers
    per the architecture note about Academy's `gtin8` field holding a
    13-digit value.
    """
    out: list[str] = []
    for value in raw_values:
        normalized = normalize_gtin(value)
        if normalized and normalized not in out:
            out.append(normalized)
    return out
