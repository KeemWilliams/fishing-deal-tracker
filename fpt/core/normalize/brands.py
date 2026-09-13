"""Brand name normalization via `config/brand_aliases.yaml`."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

DEFAULT_BRAND_ALIASES_CONFIG = (
    Path(__file__).resolve().parents[3] / "config" / "brand_aliases.yaml"
)


def brand_key(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def load_brand_aliases(config_path: Path | None = None) -> dict[str, str]:
    """Returns {alias_key: canonical_brand_name}."""
    path = config_path or DEFAULT_BRAND_ALIASES_CONFIG
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: dict[str, str] = {}
    for canonical, aliases in (data.get("brand_aliases") or {}).items():
        out[brand_key(canonical)] = canonical
        for alias in aliases or []:
            key = brand_key(alias)
            if key:
                out[key] = canonical
    return out


def canonical_brand(raw: str | None, *, aliases: dict[str, str] | None = None) -> str | None:
    if not raw:
        return None
    table = aliases if aliases is not None else load_brand_aliases()
    key = brand_key(raw)
    if key is None:
        return None
    return table.get(key, raw.strip())
