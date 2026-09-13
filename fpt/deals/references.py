"""Reference price resolution (architecture doc 6.1).

Pure functions over plain data -- callers (store/deal-engine glue, mostly
DB-facing and out of this task's scope) are responsible for pulling the
inputs (daily non-clearance price history, cross-retailer offers, the
retailer's own claimed reference) out of Postgres and handing them here.

Key rule this module enforces: "VERIFIED reference = minimum of available
R1, R2, R3. Taking the lowest understates discounts, which is the safe
direction." A used offer is NEVER compared against a NEW offer's R1/R2/R3/
R4 -- see `resolve_used_reference`, which is a completely separate code
path from `resolve_verified_reference` and never touches NEW-offer inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median


@dataclass(frozen=True)
class NonClearanceDay:
    day: date
    price_cents: int


@dataclass(frozen=True)
class OwnHistoryResult:
    median_cents: int
    observed_days: int
    span_days: int


def resolve_own_history_median(
    days: list[NonClearanceDay],
    *,
    min_days: int = 21,
    min_span_days: int = 30,
) -> OwnHistoryResult | None:
    """R1: median of daily OK prices for this offer's listing, same
    condition group, same seller type, over the prior 90 days, EXCLUDING
    days with on_clearance = true. `days` must already be pre-filtered to
    that scope and window by the caller -- this function only checks the
    minimum-evidence bar and computes the median."""
    if not days:
        return None
    ordered = sorted(days, key=lambda d: d.day)
    span = (ordered[-1].day - ordered[0].day).days
    if len(ordered) < min_days or span < min_span_days:
        return None
    med = median(d.price_cents for d in ordered)
    return OwnHistoryResult(median_cents=round(med), observed_days=len(ordered), span_days=span)


@dataclass(frozen=True)
class CrossRetailerCandidate:
    retailer_slug: str
    price_cents: int
    is_r1_quality: bool  # has its own 90-day non-clearance median evidence
    match_status: str  # "auto_gtin" | "auto_mpn" | "manual" | "auto_attributes" | ...
    # The reference LISTING's own identity (not our offer's) -- carried
    # through so C8 (confirm.py) can verify the reference is scoped to the
    # same variant_key/unit_count it claims to be, rather than trivially
    # comparing our own offer's identity against itself (H3/H4).
    variant_key_observed: str | None = None
    unit_count: int | None = None


def resolve_cross_retailer_new(
    candidates: list[CrossRetailerCandidate],
) -> CrossRetailerCandidate | None:
    """R2: NEW offers only. Listing-to-variant link must be auto_gtin,
    auto_mpn, or manual on BOTH sides -- attribute-only matches cannot
    verify (architecture doc: "Usable for display grouping, not for R2
    verification"). Returns the candidate with the lowest price among the
    verifiable set, or None if no candidate qualifies."""
    verifiable = [c for c in candidates if c.match_status in ("auto_gtin", "auto_mpn", "manual")]
    if not verifiable:
        return None
    return min(verifiable, key=lambda c: c.price_cents)


@dataclass(frozen=True)
class VerifiedReference:
    kind: str  # "OWN_HISTORY_MEDIAN_90D" | "CROSS_RETAILER_NEW" | "DATA_API_HISTORY"
    cents: int
    detail: dict


def resolve_verified_reference(
    *,
    own_history: OwnHistoryResult | None,
    cross_retailer: CrossRetailerCandidate | None,
    data_api_median_cents: int | None = None,
) -> VerifiedReference | None:
    """Minimum of whichever of R1/R2/R3 are available. R3 (data API) is a
    Phase 2 input, accepted here as a plain int so this function needs no
    knowledge of the keepa_amazon adapter."""
    candidates: list[VerifiedReference] = []
    if own_history is not None:
        candidates.append(
            VerifiedReference(
                kind="OWN_HISTORY_MEDIAN_90D",
                cents=own_history.median_cents,
                detail={"days": own_history.observed_days, "span_days": own_history.span_days},
            )
        )
    if cross_retailer is not None:
        candidates.append(
            VerifiedReference(
                kind="CROSS_RETAILER_NEW",
                cents=cross_retailer.price_cents,
                detail={
                    "retailer": cross_retailer.retailer_slug,
                    "match": cross_retailer.match_status,
                    # H3/H4: the reference LISTING's own variant_key/unit_count,
                    # not ours -- consumed by build_confirm_context (C8).
                    "variant_key_observed": cross_retailer.variant_key_observed,
                    "unit_count": cross_retailer.unit_count,
                },
            )
        )
    if data_api_median_cents is not None:
        candidates.append(
            VerifiedReference(kind="DATA_API_HISTORY", cents=data_api_median_cents, detail={})
        )
    if not candidates:
        return None
    return min(candidates, key=lambda r: r.cents)


def claimed_inflated(
    claimed_reference_cents: int | None,
    verified_reference_cents: int | None,
    *,
    ratio: float = 1.25,
) -> bool | None:
    """None when no verified reference exists to compare against (the
    caller should then treat the claim as an unverified CLAIMED-lane
    reference instead, per 6.1)."""
    if claimed_reference_cents is None or verified_reference_cents is None:
        return None
    return claimed_reference_cents > ratio * verified_reference_cents


def resolve_used_reference(current_new_offers_cents: list[int]) -> int | None:
    """U1: for USED/OPEN_BOX/REFURB offers, the lowest current confirmed
    in-stock NEW price for the same variant across trusted retailers
    (landed price when shipping known -- the caller passes landed prices
    already resolved). Deliberately takes ONLY new-offer prices; there is
    no code path from here back into R1/R2/R3/R4 for the used offer
    itself, matching "a used offer is never measured against R1/R2/R3/R4
    of a NEW offer" read the other direction as well."""
    if not current_new_offers_cents:
        return None
    return min(current_new_offers_cents)
