"""Rod attribute normalization (architecture doc 4.2 rod row).

Required attributes: rod_type, length_in (int), power (UL/L/ML/M/MH/H/XH),
action (XF/F/MF/M/MOD/S), pieces. "Missing action goes to review; never
guessed" -- this module returns None for any attribute it cannot parse
confidently rather than defaulting.
"""

from __future__ import annotations

import re
from typing import Mapping

_LENGTH_RE = re.compile(r"(\d+)\s*'\s*(\d+)?\"?")

_POWER_ALIASES = {
    "ultralight": "UL", "ul": "UL",
    "light": "L", "l": "L",
    "medium light": "ML", "medium-light": "ML", "ml": "ML",
    "medium": "M", "m": "M", "md": "M",
    "medium heavy": "MH", "medium-heavy": "MH", "mh": "MH",
    "heavy": "H", "h": "H",
    "extra heavy": "XH", "xh": "XH",
}

_ACTION_ALIASES = {
    "extra fast": "XF", "xf": "XF",
    "fast": "F", "f": "F",
    "moderate fast": "MF", "medium fast": "MF", "mf": "MF",
    "moderate": "MOD", "mod": "MOD",
    "slow": "S", "s": "S",
    "medium": "M", "m": "M",
}


def parse_length_in(raw: str | None) -> int | None:
    """"7'6"" -> 90 inches. Returns None if unparseable."""
    if not raw:
        return None
    match = _LENGTH_RE.search(raw)
    if not match:
        return None
    feet = int(match.group(1))
    inches = int(match.group(2)) if match.group(2) else 0
    return feet * 12 + inches


def parse_power_action(raw: str | None) -> tuple[str | None, str | None]:
    """TW exposes a combined "Power - Taper" style item, e.g.
    "Medium - Fast" -> ("M", "F"). Splits on the first " - " only; returns
    (None, None) components it cannot map rather than guessing."""
    if not raw:
        return None, None
    parts = [p.strip() for p in raw.split(" - ")]
    if len(parts) != 2:
        return None, None
    power = _POWER_ALIASES.get(parts[0].strip().lower())
    action = _ACTION_ALIASES.get(parts[1].strip().lower())
    return power, action


def normalize_rod_attributes(
    style_items: Mapping[str, str],
    *,
    rod_type: str = "spinning",
    pieces: int = 1,
) -> dict[str, object]:
    """`style_items` maps TW style names (e.g. "Spinning Rod Length",
    "Spinning Rod Power - Spinning Rod Taper") to their value text. Rod
    type and pieces default to the common case (spinning, 1-piece); TW's
    style list does not consistently expose either as a separate field on
    the pages sampled, so callers may override from title/category context.
    """
    length_in = None
    power = None
    action = None
    for style_name, value in style_items.items():
        lowered = style_name.lower()
        if "length" in lowered:
            length_in = parse_length_in(value)
        elif "power" in lowered and "taper" in lowered:
            power, action = parse_power_action(value)
        elif "power" in lowered:
            power = _POWER_ALIASES.get(value.strip().lower())
        elif "taper" in lowered or "action" in lowered:
            action = _ACTION_ALIASES.get(value.strip().lower())
        if "casting" in lowered:
            rod_type = "casting"
    return {
        "rod_type": rod_type,
        "length_in": length_in,
        "power": power,
        "action": action,
        "pieces": pieces,
    }
