"""Two-fetch confirmation (architecture doc 6.4, checks C1-C10).

A CANDIDATE becomes ACTIVE only if a CONFIRM observation satisfies ALL
checks below. This module is pure: given a `ConfirmContext` built by the
caller from the detecting and confirming observations plus a freshly
resolved reference, it returns either an ACTIVE/HELD_REVIEW verdict or a
REJECTED verdict with the specific reason code from the architecture's
table -- so a downstream reviewer or test can point at exactly which
check failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from fpt.core.models import CONDITION_GROUP


@dataclass(frozen=True)
class ConfirmContext:
    detected_at: datetime
    confirming_observed_at: datetime
    detected_via: str  # "DISCOVERY_GRID" | "PRODUCT_PAGE" | "DATA_API"
    confirming_page_type: str  # "PRODUCT" | "CLEARANCE_LISTING" | "USED_LISTING" | "API_BATCH"

    detected_retailer_sku: str
    confirming_retailer_sku: str
    detected_offer_key: str
    confirming_offer_key: str
    detected_condition: str
    confirming_condition: str
    detected_seller_key: str
    confirming_seller_key: str

    detected_variant_key_observed: str | None
    confirming_variant_key_observed: str | None
    listing_variant_key_observed: str | None
    detected_unit_count: int | None
    confirming_unit_count: int | None
    listing_unit_count: int | None

    confirming_quality: str  # "OK" | "SUSPECT" | "REJECTED"
    confirming_quality_reasons: list[str]

    detected_price_cents: int
    confirming_price_cents: int

    recomputed_discount_pct: float
    recomputed_reference_variant_key: str | None
    recomputed_reference_condition_group: str | None
    offer_variant_key: str | None
    offer_condition_group: str

    confirming_price_cents_for_floor: int
    category_floor_cents: int | None

    # H3/H4: the reference offer's own unit_count (from the reference
    # lookup) vs this offer's own stored unit_count -- C8 rejects a
    # reference whose pack count doesn't match, since a per-piece price
    # compared against a multi-pack price (or vice versa) is not the same
    # product even when GTIN-linked to the same variant_id.
    recomputed_reference_unit_count: int | None = None
    offer_unit_count: int | None = None

    # L1: the freshly resolved reference price, persisted onto the deal row
    # by the caller (fpt/store/deals.py apply_confirm_result) when this
    # context's verdict is ACTIVE/HELD_REVIEW -- kept on the context (not
    # just computed ad hoc by the caller) so the number written to the DB
    # is provably the SAME number this module's checks were evaluated
    # against.
    reference_cents: int | None = None

    min_delay_minutes: int = 10
    max_price_increase_pct: float = 2.0
    min_discount_pct: float = 50.0
    held_review_upper_pct: float = 85.0
    implausible_discount_pct: float = 95.0


@dataclass(frozen=True)
class ConfirmResult:
    status: str  # "ACTIVE" | "HELD_REVIEW" | "REJECTED"
    reject_reason: str | None = None
    hold_reason: str | None = None


def confirm_candidate(ctx: ConfirmContext) -> ConfirmResult:
    # C1: separate fetch at least min_delay_minutes after detection.
    min_gap = timedelta(minutes=ctx.min_delay_minutes)
    if ctx.confirming_observed_at - ctx.detected_at < min_gap:
        return ConfirmResult(status="REJECTED", reject_reason="confirm_too_soon")

    # C2: if detected from a discovery grid, confirmation must come from
    # the product page, never the grid again.
    if ctx.detected_via == "DISCOVERY_GRID" and ctx.confirming_page_type != "PRODUCT":
        return ConfirmResult(status="REJECTED", reject_reason="confirm_not_from_product_page")

    # C3: same retailer_sku, offer_key, condition, seller.
    if (
        ctx.detected_retailer_sku != ctx.confirming_retailer_sku
        or ctx.detected_offer_key != ctx.confirming_offer_key
        or ctx.detected_condition != ctx.confirming_condition
        or ctx.detected_seller_key != ctx.confirming_seller_key
    ):
        return ConfirmResult(status="REJECTED", reject_reason="offer_identity_mismatch")

    # C4: same variant_key_observed and unit_count as detection AND the
    # listing record.
    variant_keys = {
        ctx.detected_variant_key_observed,
        ctx.confirming_variant_key_observed,
        ctx.listing_variant_key_observed,
    }
    if len(variant_keys) > 1:
        return ConfirmResult(status="REJECTED", reject_reason="variant_mismatch")
    unit_counts = {ctx.detected_unit_count, ctx.confirming_unit_count, ctx.listing_unit_count}
    if len(unit_counts) > 1:
        return ConfirmResult(status="REJECTED", reject_reason="variant_mismatch")

    # C5: confirming observation OK, or SUSPECT for large_move only.
    if ctx.confirming_quality == "REJECTED":
        return ConfirmResult(status="REJECTED", reject_reason="quality:rejected")
    if ctx.confirming_quality == "SUSPECT":
        other_reasons = [r for r in ctx.confirming_quality_reasons if r != "large_move"]
        if other_reasons:
            return ConfirmResult(status="REJECTED", reject_reason=f"quality:{other_reasons[0]}")

    # C6: confirming price within +2% of detected price (lower is fine).
    max_allowed = ctx.detected_price_cents * (1 + ctx.max_price_increase_pct / 100.0)
    if ctx.confirming_price_cents > max_allowed:
        return ConfirmResult(status="REJECTED", reject_reason="price_changed")

    # C7: recomputed discount still >= threshold.
    if ctx.recomputed_discount_pct < ctx.min_discount_pct:
        return ConfirmResult(status="REJECTED", reject_reason="below_threshold_on_confirm")

    # C8: reference used is for the same variant_key, unit_count, and
    # condition group -- the reference OFFER's own identity (from the
    # reference lookup), not this offer's identity re-asserted at itself.
    # For a used offer's U1 reference and a NEW offer's R2 cross-retailer
    # reference, the reference is a genuinely different offer/listing, so
    # this is the guard that actually catches a mis-scoped reference.
    if (
        ctx.recomputed_reference_variant_key is not None
        and ctx.offer_variant_key is not None
        and ctx.recomputed_reference_variant_key != ctx.offer_variant_key
    ):
        return ConfirmResult(status="REJECTED", reject_reason="reference_scope_mismatch")
    if (
        ctx.recomputed_reference_unit_count is not None
        and ctx.offer_unit_count is not None
        and ctx.recomputed_reference_unit_count != ctx.offer_unit_count
    ):
        return ConfirmResult(status="REJECTED", reject_reason="reference_scope_mismatch")
    if (
        ctx.recomputed_reference_condition_group is not None
        and ctx.recomputed_reference_condition_group != ctx.offer_condition_group
    ):
        return ConfirmResult(status="REJECTED", reject_reason="reference_scope_mismatch")

    # C10: category floor met by confirming price.
    if ctx.category_floor_cents is not None and ctx.confirming_price_cents_for_floor < ctx.category_floor_cents:
        return ConfirmResult(status="REJECTED", reject_reason="below_floor")

    # C9: discount band decides ACTIVE / HELD_REVIEW / REJECTED.
    if ctx.recomputed_discount_pct > ctx.implausible_discount_pct:
        return ConfirmResult(status="REJECTED", reject_reason="implausible_discount")
    if ctx.recomputed_discount_pct > ctx.held_review_upper_pct:
        return ConfirmResult(status="HELD_REVIEW", hold_reason="discount_85_to_95")

    return ConfirmResult(status="ACTIVE")


def condition_group(condition: str) -> str:
    return CONDITION_GROUP.get(condition, "USED")
