"""Observation quality guards, applied at storage time (architecture doc 6.2).

These are pure functions over plain values so they're testable without a
database: the caller (store.py, owned alongside the DB layer) supplies
"last OK" context pulled from `price_observations` and gets back a
`(quality, reasons)` verdict to persist alongside the observation.

SUSPECT and REJECTED observations are retained (never dropped) and are
excluded from references, deals, alerts, and export by downstream code --
this module only classifies, it does not decide what happens next, except
for one exception written into the architecture doc itself: "A SUSPECT
`large_move` observation still enqueues a CONFIRM fetch" -- callers should
special-case that reason when deciding whether to enqueue a CONFIRM task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from fpt.core.models import Availability

DEFAULT_CATEGORIES_CONFIG = Path(__file__).resolve().parents[2] / "config" / "categories.yaml"
DEFAULT_DEAL_RULES_CONFIG = Path(__file__).resolve().parents[2] / "config" / "deal_rules.yaml"


def load_category_bands(config_path: Path | None = None) -> dict[str, dict[str, int]]:
    path = config_path or DEFAULT_CATEGORIES_CONFIG
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("categories") or {}


def load_guard_thresholds(config_path: Path | None = None) -> dict:
    path = config_path or DEFAULT_DEAL_RULES_CONFIG
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return (data.get("deal_rules") or {}).get("observation_guards", {})


@dataclass(frozen=True)
class ObservationInput:
    price_cents: int | None
    currency: str
    category: str
    claimed_reference_cents: int | None
    on_clearance: bool
    unit_count: int | None
    variant_key_observed: str | None
    availability: Availability
    condition_wording_mapped: bool  # False when the retailer's wording could not be mapped
    is_used_page_type: bool


@dataclass(frozen=True)
class ObservationContext:
    last_ok_price_cents: int | None = None
    last_ok_unit_count: int | None = None
    listing_variant_key_observed: str | None = None
    listing_unit_count: int | None = None
    previous_variant_count: int | None = None
    current_variant_count: int | None = None
    previous_distinct_price_count: int | None = None
    current_distinct_price_count: int | None = None


@dataclass(frozen=True)
class ValidationResult:
    quality: str  # "OK" | "SUSPECT" | "REJECTED"
    reasons: list[str] = field(default_factory=list)


def validate_observation(
    obs: ObservationInput,
    ctx: ObservationContext,
    *,
    category_bands: dict[str, dict[str, int]] | None = None,
    thresholds: dict | None = None,
) -> ValidationResult:
    bands = category_bands if category_bands is not None else load_category_bands()
    guard = thresholds if thresholds is not None else load_guard_thresholds()

    reasons: list[str] = []

    # price NULL/<=0, currency not USD -> REJECTED (hard stop, no further checks matter)
    if obs.price_cents is None or obs.price_cents <= 0 or obs.currency != "USD":
        return ValidationResult(quality="REJECTED", reasons=["invalid_price_or_currency"])

    band = bands.get(obs.category, {})
    floor = band.get("floor_cents")
    ceiling = band.get("ceiling_cents")
    if (floor is not None and obs.price_cents < floor) or (
        ceiling is not None and obs.price_cents > ceiling
    ):
        reasons.append("out_of_band")

    if ctx.last_ok_price_cents:
        low_pct = guard.get("large_move_min_pct", 35.0)
        high_pct = guard.get("large_move_max_pct", 300.0)
        ratio_pct = (obs.price_cents / ctx.last_ok_price_cents) * 100
        if ratio_pct < low_pct or ratio_pct > high_pct:
            reasons.append("large_move")

    if obs.on_clearance and obs.claimed_reference_cents is not None:
        if obs.claimed_reference_cents <= obs.price_cents:
            reasons.append("claimed_not_above_price")

    if ctx.listing_unit_count is not None and obs.unit_count is not None:
        if obs.unit_count != ctx.listing_unit_count:
            reasons.append("unit_mismatch")
    elif ctx.last_ok_price_cents and obs.unit_count and ctx.listing_unit_count:
        tolerance = guard.get("unit_mismatch_tolerance_pct", 5.0) / 100.0
        ratio = obs.price_cents / ctx.last_ok_price_cents
        for candidate in (1 / obs.unit_count if obs.unit_count else None, obs.unit_count):
            if candidate and abs(ratio - candidate) <= tolerance:
                reasons.append("unit_mismatch")
                break

    if (
        ctx.listing_variant_key_observed is not None
        and obs.variant_key_observed is not None
        and obs.variant_key_observed != ctx.listing_variant_key_observed
    ):
        reasons.append("variant_identity_changed")

    if (
        ctx.previous_variant_count is not None
        and ctx.current_variant_count is not None
        and ctx.previous_variant_count > 0
    ):
        collapse_pct = guard.get("discovery_collapse_pct", 70.0)
        drop_pct = (
            (ctx.previous_variant_count - ctx.current_variant_count)
            / ctx.previous_variant_count
        ) * 100
        if ctx.current_variant_count < ctx.previous_variant_count * 0.5 or drop_pct >= collapse_pct:
            reasons.append("variant_collapse")

    if (
        ctx.previous_distinct_price_count is not None
        and ctx.previous_distinct_price_count >= 3
        and ctx.current_distinct_price_count == 1
    ):
        reasons.append("price_flattening")

    if not obs.condition_wording_mapped:
        # USED page types get a graceful fallback to USED_UNGRADED upstream
        # in the adapter (never guessed at validation time); a SUSPECT here
        # is only for non-USED pages where wording appeared unexpectedly.
        if not obs.is_used_page_type:
            reasons.append("condition_unknown")

    if not reasons:
        return ValidationResult(quality="OK", reasons=[])
    return ValidationResult(quality="SUSPECT", reasons=reasons)
