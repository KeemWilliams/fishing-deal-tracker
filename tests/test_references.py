from datetime import date, timedelta

from fpt.deals.references import (
    CrossRetailerCandidate,
    NonClearanceDay,
    claimed_inflated,
    resolve_cross_retailer_new,
    resolve_own_history_median,
    resolve_used_reference,
    resolve_verified_reference,
)


def _days(prices: list[int], start: date) -> list[NonClearanceDay]:
    return [NonClearanceDay(day=start + timedelta(days=i), price_cents=p) for i, p in enumerate(prices)]


def test_own_history_requires_minimum_days_and_span():
    too_few = _days([13000] * 10, date(2026, 1, 1))
    assert resolve_own_history_median(too_few) is None


def test_own_history_median_excludes_clearance_days_by_construction():
    # Caller is responsible for pre-filtering clearance days out; this test
    # documents that the function trusts its input and just computes.
    # 31 non-clearance days spanning 30 calendar days satisfies both the
    # min-days (21) and min-span (30) evidence bars.
    prices = [13000] * 30 + [12000]
    days = _days(prices, date(2026, 1, 1))
    result = resolve_own_history_median(days)
    assert result is not None
    assert result.median_cents == 13000
    assert result.observed_days == 31
    assert result.span_days == 30


def test_cross_retailer_requires_verifiable_match_status():
    candidates = [
        CrossRetailerCandidate("academy", 11000, is_r1_quality=False, match_status="auto_attributes"),
        CrossRetailerCandidate("jandh", 12500, is_r1_quality=True, match_status="auto_gtin"),
    ]
    result = resolve_cross_retailer_new(candidates)
    assert result is not None
    assert result.retailer_slug == "jandh"  # the only verifiable one, even though not cheapest


def test_cross_retailer_picks_lowest_among_verifiable():
    candidates = [
        CrossRetailerCandidate("academy", 11000, is_r1_quality=False, match_status="auto_mpn"),
        CrossRetailerCandidate("jandh", 12500, is_r1_quality=True, match_status="auto_gtin"),
    ]
    result = resolve_cross_retailer_new(candidates)
    assert result.retailer_slug == "academy"


def test_cross_retailer_none_when_nothing_verifiable():
    candidates = [
        CrossRetailerCandidate("academy", 11000, is_r1_quality=False, match_status="auto_attributes"),
    ]
    assert resolve_cross_retailer_new(candidates) is None


def test_cross_retailer_candidate_carries_its_own_variant_key_and_unit_count():
    # H3/H4: the candidate's own identity (not our offer's) must survive
    # into resolve_cross_retailer_new's output, since it is a DIFFERENT
    # listing entirely -- consumed downstream by build_confirm_context (C8).
    candidates = [
        CrossRetailerCandidate(
            "jandh", 11999, is_r1_quality=True, match_status="auto_gtin",
            variant_key_observed="length_in=90|power=M", unit_count=5,
        ),
    ]
    result = resolve_cross_retailer_new(candidates)
    assert result.variant_key_observed == "length_in=90|power=M"
    assert result.unit_count == 5


def test_verified_reference_detail_carries_reference_variant_key_and_unit_count():
    cross = CrossRetailerCandidate(
        "jandh", 11999, is_r1_quality=True, match_status="auto_gtin",
        variant_key_observed="length_in=90|power=M", unit_count=5,
    )
    result = resolve_verified_reference(own_history=None, cross_retailer=cross)
    assert result is not None
    assert result.detail["variant_key_observed"] == "length_in=90|power=M"
    assert result.detail["unit_count"] == 5


def test_verified_reference_takes_minimum_of_r1_and_r2():
    own_history = resolve_own_history_median(_days([13000] * 30, date(2026, 1, 1)))
    cross = CrossRetailerCandidate("jandh", 11999, is_r1_quality=True, match_status="auto_gtin")
    result = resolve_verified_reference(own_history=own_history, cross_retailer=cross)
    assert result is not None
    assert result.cents == 11999
    assert result.kind == "CROSS_RETAILER_NEW"


def test_verified_reference_none_when_nothing_available():
    assert resolve_verified_reference(own_history=None, cross_retailer=None) is None


def test_claimed_inflated_true_when_far_above_verified():
    assert claimed_inflated(20999, 12999, ratio=1.25) is True


def test_claimed_inflated_false_when_close_to_verified():
    assert claimed_inflated(13999, 12999, ratio=1.25) is False


def test_claimed_inflated_none_without_a_verified_reference():
    assert claimed_inflated(20999, None) is None


def test_used_reference_never_touches_new_offer_r1_r2_r4_inputs():
    # resolve_used_reference's signature only accepts a list of NEW-offer
    # prices -- there is no parameter through which a claimed/R1/R2
    # reference for the used offer itself could leak in.
    result = resolve_used_reference([13000, 12500, 14000])
    assert result == 12500


def test_used_reference_none_with_no_new_offers():
    assert resolve_used_reference([]) is None
