"""Soft/hard bait attribute normalization (architecture doc 4.2).

Minimal MVP normalizer: TW exposes color as a per-variant style item
("Color": "Black") and no explicit unit_count field, so unit_count is left
None here unless the caller supplies it from title text (e.g. "13-Pack").
A missing unit_count means no variant_key (soft_bait requires it) -- the
offer is still tracked, just unmatched, per architecture 4.2.
"""

from __future__ import annotations

import re
from typing import Mapping

_UNIT_COUNT_RE = re.compile(r"(\d+)\s*[- ]?(?:pack|pk|ct|count)\b", re.IGNORECASE)


def parse_unit_count(title_raw: str) -> int | None:
    match = _UNIT_COUNT_RE.search(title_raw)
    if not match:
        return None
    return int(match.group(1))


def color_key(raw_color: str | None) -> str | None:
    if not raw_color:
        return None
    return re.sub(r"\s+", "_", raw_color.strip().lower())


def normalize_bait_attributes(
    style_items: Mapping[str, str],
    *,
    title_raw: str,
) -> dict[str, object]:
    color = None
    for style_name, value in style_items.items():
        if "color" in style_name.lower():
            color = color_key(value)
    return {
        "color_key": color,
        "unit_count": parse_unit_count(title_raw),
        "length_in": None,
        "weight_oz": None,
    }
