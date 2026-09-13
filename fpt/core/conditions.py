"""Maps a retailer's own condition wording to the canonical `Condition` enum.

Config-driven per architecture doc `config/conditions.yaml` (per-retailer
condition wording -> Condition). Unmappable wording is never guessed: per
the observation quality guard table (6.2), an unmappable condition on a
USED page type becomes `USED_UNGRADED`; elsewhere it is flagged SUSPECT
`condition_unknown` by the validator, not silently defaulted here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from fpt.core.models import Condition

DEFAULT_CONDITIONS_CONFIG = Path(__file__).resolve().parents[2] / "config" / "conditions.yaml"


def load_condition_map(config_path: Path | None = None) -> dict[str, dict[str, str]]:
    """Load {retailer_slug: {raw_wording_lowercased: Condition_value}}."""
    path = config_path or DEFAULT_CONDITIONS_CONFIG
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: dict[str, dict[str, str]] = {}
    for retailer_slug, mapping in (data.get("conditions") or {}).items():
        out[retailer_slug] = {k.strip().lower(): v for k, v in (mapping or {}).items()}
    return out


def map_condition(
    retailer_slug: str,
    raw_wording: str | None,
    *,
    condition_map: dict[str, dict[str, str]] | None = None,
    default_new: bool = True,
) -> tuple[Condition | None, str | None]:
    """Return (Condition or None, raw_wording as given).

    `default_new` should be True for adapters whose page type is
    unconditionally NEW (e.g. Tackle Warehouse regular product pages,
    which carry no used offers at all) so that an absent condition string
    still resolves to Condition.NEW rather than an unknown. Set False for
    any used/mixed-condition page so an unmapped wording surfaces as
    unmapped (None) instead of being silently guessed as NEW.
    """
    if raw_wording is None or not raw_wording.strip():
        return (Condition.NEW if default_new else None), raw_wording
    table = condition_map or load_condition_map()
    retailer_table = table.get(retailer_slug, {})
    mapped = retailer_table.get(raw_wording.strip().lower())
    if mapped is None:
        return None, raw_wording
    try:
        return Condition(mapped), raw_wording
    except ValueError:
        return None, raw_wording
