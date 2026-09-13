"""Builds `variant_key` from normalized attributes (architecture doc 4.2).

`variant_key` is built from required attributes in listed order as
`k=v|k=v`. Missing a required attribute means no auto-match and no
cross-retailer reference, but the offer is still tracked -- so this
function returns None rather than guessing when a required key is absent,
per the "never guessed" rule for identity fields.
"""

from __future__ import annotations

from typing import Mapping, Sequence

REQUIRED_ATTRIBUTES: dict[str, Sequence[str]] = {
    "rod": ("rod_type", "length_in", "power", "action", "pieces"),
    "reel": ("reel_type", "size_code", "gear_ratio"),
    "combo": ("rod_type", "length_in", "power", "action", "pieces", "reel_size_code"),
    "soft_bait": ("length_in", "color_key", "unit_count"),
    "hard_bait": ("length_in", "color_key"),
    "line": ("line_type", "lb_test", "yards", "color_key"),
    "terminal": ("size", "weight_oz", "unit_count"),
    "other": (),
}


def build_variant_key(category: str, attributes: Mapping[str, object]) -> str | None:
    """Return the `k=v|k=v` variant key for `category`, or None if any
    required attribute is missing/None. `other` has no required
    attributes and therefore never auto-matches (architecture 4.2 note)."""
    required = REQUIRED_ATTRIBUTES.get(category)
    if required is None:
        return None
    if category == "other":
        return None
    parts = []
    for key in required:
        value = attributes.get(key)
        if value is None or value == "":
            return None
        parts.append(f"{key}={value}")
    return "|".join(parts)
