"""Deal rule application (architecture doc 6.3). Only three public rules
exist; nothing under 50% is published, and the threshold is config, not
code (assumption A6, config/deal_rules.yaml).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from fpt.core.models import Availability, Condition

DEFAULT_DEAL_RULES_CONFIG = Path(__file__).resolve().parents[2] / "config" / "deal_rules.yaml"


def load_deal_rules(config_path: Path | None = None) -> dict:
    path = config_path or DEFAULT_DEAL_RULES_CONFIG
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return data.get("deal_rules") or {}


@dataclass(frozen=True)
class DealCandidate:
    rule: str  # "DEEP_DISCOUNT_NEW" | "DEEP_DISCOUNT_CLAIMED" | "USED_VS_CURRENT_NEW"
    lane: str  # "VERIFIED" | "CLAIMED" | "USED"
    price_cents: int
    reference_cents: int
    discount_pct: float


def _discount_pct(price_cents: int, reference_cents: int) -> float:
    if reference_cents <= 0:
        return 0.0
    return round((1 - (price_cents / reference_cents)) * 100, 2)


def detect_deep_discount_new(
    *,
    condition: Condition,
    availability: Availability,
    price_cents: int,
    verified_reference_cents: int | None,
    rules: dict | None = None,
) -> DealCandidate | None:
    r = rules if rules is not None else load_deal_rules()
    min_pct = r.get("min_discount_pct", 50.0)
    min_saving = r.get("min_saving_cents", 1000)

    if condition != Condition.NEW:
        return None
    if availability not in (Availability.IN_STOCK, Availability.STORE_ONLY):
        return None
    if verified_reference_cents is None:
        return None
    saving = verified_reference_cents - price_cents
    if saving < min_saving:
        return None
    pct = _discount_pct(price_cents, verified_reference_cents)
    if pct < min_pct:
        return None
    return DealCandidate(
        rule="DEEP_DISCOUNT_NEW",
        lane="VERIFIED",
        price_cents=price_cents,
        reference_cents=verified_reference_cents,
        discount_pct=pct,
    )


def detect_deep_discount_claimed(
    *,
    condition: Condition,
    price_cents: int,
    claimed_reference_cents: int | None,
    on_clearance: bool,
    has_verified_reference: bool,
    rules: dict | None = None,
) -> DealCandidate | None:
    r = rules if rules is not None else load_deal_rules()
    min_pct = r.get("min_discount_pct", 50.0)
    min_saving = r.get("min_saving_cents", 1000)

    if condition != Condition.NEW:
        return None
    if has_verified_reference:
        # A VERIFIED reference always wins; CLAIMED never backs a deal
        # when one exists (architecture 6.1).
        return None
    if claimed_reference_cents is None or not on_clearance:
        return None
    saving = claimed_reference_cents - price_cents
    if saving < min_saving:
        return None
    pct = _discount_pct(price_cents, claimed_reference_cents)
    if pct < min_pct:
        return None
    return DealCandidate(
        rule="DEEP_DISCOUNT_CLAIMED",
        lane="CLAIMED",
        price_cents=price_cents,
        reference_cents=claimed_reference_cents,
        discount_pct=pct,
    )


def detect_used_vs_current_new(
    *,
    condition: Condition,
    availability: Availability,
    landed_price_cents: int,
    current_new_reference_cents: int | None,
    rules: dict | None = None,
) -> DealCandidate | None:
    r = rules if rules is not None else load_deal_rules()
    min_pct = r.get("min_discount_pct", 50.0)
    min_saving = r.get("min_saving_cents", 1000)

    if condition not in (
        Condition.USED_LIKE_NEW,
        Condition.USED_VERY_GOOD,
        Condition.USED_GOOD,
        Condition.NEW_OPEN_BOX,
        Condition.REFURBISHED,
        Condition.USED_UNGRADED,
    ):
        return None
    # USED_ACCEPTABLE is tracked/watchable but excluded from public headline
    # deals (assumption A7) -- never surfaces from this function.
    if availability != Availability.IN_STOCK:
        return None
    if current_new_reference_cents is None:
        return None
    saving = current_new_reference_cents - landed_price_cents
    if saving < min_saving:
        return None
    pct = _discount_pct(landed_price_cents, current_new_reference_cents)
    if pct < min_pct:
        return None
    return DealCandidate(
        rule="USED_VS_CURRENT_NEW",
        lane="USED",
        price_cents=landed_price_cents,
        reference_cents=current_new_reference_cents,
        discount_pct=pct,
    )
