"""Adversarial pure-function tests (no DB required) requested by the TEST
phase mission: a parse error yielding a near-zero price, and a multi-pack
price/unit_count confusion. These always run as part of the main suite.
"""

from __future__ import annotations

from fpt.core.models import Availability
from fpt.core.money import parse_price_cents
from fpt.pipeline.validate import ObservationContext, ObservationInput, validate_observation


def test_near_zero_parse_error_price_never_becomes_zero_cents():
    # "$0.00" is the classic parse-failure artifact -- money.py already
    # turns this into None, never a 0 (existing coverage in test_money.py);
    # this test proves the guard survives into the quality-validation layer
    # if a caller ever mistakenly coerced that None to 0 before calling it.
    assert parse_price_cents("$0.00") is None

    obs_zero = ObservationInput(
        price_cents=0, currency="USD", category="rod", claimed_reference_cents=None,
        on_clearance=False, unit_count=None, variant_key_observed=None,
        availability=Availability.IN_STOCK, condition_wording_mapped=True, is_used_page_type=False,
    )
    result = validate_observation(obs_zero, ObservationContext())
    assert result.quality == "REJECTED"
    assert "invalid_price_or_currency" in result.reasons


def test_near_zero_but_nonzero_price_is_suspect_not_ok():
    # A 1-cent price for a rod (category floor 1500 cents) is a real,
    # non-crashing scrape result but obvious nonsense -- must be flagged
    # SUSPECT out_of_band, never pass through as OK.
    obs = ObservationInput(
        price_cents=1, currency="USD", category="rod", claimed_reference_cents=None,
        on_clearance=False, unit_count=None, variant_key_observed=None,
        availability=Availability.IN_STOCK, condition_wording_mapped=True, is_used_page_type=False,
    )
    result = validate_observation(obs, ObservationContext())
    assert result.quality == "SUSPECT"
    assert "out_of_band" in result.reasons


def test_FIXED_unit_mismatch_ratio_heuristic_fires_when_listing_unit_count_unknown():
    """Regression test for the dead-code defect (see test-engineer HANDOFF,
    HIGH-2, and fpt/pipeline/validate.py's DEFECT FIX comment).

    fpt/pipeline/validate.py's unit_mismatch guard used to have two branches:

        if ctx.listing_unit_count is not None and obs.unit_count is not None:
            ...
        elif ctx.last_ok_price_cents and obs.unit_count and ctx.listing_unit_count:
            ...

    The `elif` required `ctx.listing_unit_count` to be truthy to run, but it
    was only ever *reached* when the `if`'s condition was False -- which,
    given the `if` already required `ctx.listing_unit_count is not None`,
    meant `ctx.listing_unit_count` was always None whenever the `elif` was
    checked. The ratio heuristic for "price ratio implies a different pack
    size than claimed" could never fire. It was unreachable dead code.

    Concretely: a multi-pack offer whose price silently drops to price/N
    (a classic per-unit-vs-pack-price scraping bug, called out explicitly
    in this test-engineer's mission as an adversarial case) was NOT flagged
    SUSPECT when the current listing's own unit_count was unknown at
    validation time -- which is exactly the situation a DISCOVERY-grid-only
    sighting is in (the grid row doesn't carry a listing_unit_count; only
    the product page does). That halved price then looked like a legitimate
    50%+ discount to fpt/deals/detect.py with nothing downstream to catch it.

    THIS TEST NOW ASSERTS THE CORRECTED BEHAVIOR: the offer's own claimed
    unit_count (2) matches the price ratio against last_ok (0.5 == 1/2),
    which is direct evidence of the per-unit-vs-pack bug, so it must be
    flagged SUSPECT unit_mismatch even though the listing's stored
    unit_count is unknown.
    """
    obs = ObservationInput(
        price_cents=1000,  # looks like a per-unit price ($10.00)
        currency="USD", category="other", claimed_reference_cents=None,
        on_clearance=False, unit_count=2,  # offer still claims to be the 2-pack
        variant_key_observed=None, availability=Availability.IN_STOCK,
        condition_wording_mapped=True, is_used_page_type=False,
    )
    # Last confirmed OK price was $20.00 for the 2-pack; the new price is
    # exactly half of that -- the textbook "someone divided the pack price
    # by the unit count" signature the guard's docstring describes.
    ctx = ObservationContext(last_ok_price_cents=2000, listing_unit_count=None)
    result = validate_observation(obs, ctx)

    assert result.quality == "SUSPECT"
    assert "unit_mismatch" in result.reasons


def _mk_obs(*, price_cents, unit_count, category="other"):
    return ObservationInput(
        price_cents=price_cents, currency="USD", category=category,
        claimed_reference_cents=None, on_clearance=False, unit_count=unit_count,
        variant_key_observed=None, availability=Availability.IN_STOCK,
        condition_wording_mapped=True, is_used_page_type=False,
    )


def test_unit_mismatch_flags_when_both_unit_counts_known_and_differ():
    # (a) Both counts known and they differ -> flag, regardless of price ratio.
    obs = _mk_obs(price_cents=2000, unit_count=4)
    ctx = ObservationContext(last_ok_price_cents=2000, listing_unit_count=2)
    result = validate_observation(obs, ctx)
    assert result.quality == "SUSPECT"
    assert "unit_mismatch" in result.reasons


def test_unit_mismatch_price_ratio_heuristic_uses_guessed_pack_multiples():
    # (b) listing_unit_count unknown, obs.unit_count also unknown -> fall
    # back to guessed common pack multiples. A price landing on 1/3 of the
    # last OK price (a discount an honest single-item markdown essentially
    # never produces) must still be caught.
    obs = _mk_obs(price_cents=1000, unit_count=None)  # $10.00, was $30.00 -> ratio 1/3
    ctx = ObservationContext(last_ok_price_cents=3000, listing_unit_count=None)
    result = validate_observation(obs, ctx)
    assert result.quality == "SUSPECT"
    assert "unit_mismatch" in result.reasons


def test_no_false_positive_when_unit_counts_agree_at_one_with_genuine_half_off():
    # (c) A genuine ~50% single-unit markdown must not be flagged when the
    # unit counts are known and agree (both 1).
    obs = _mk_obs(price_cents=1000, unit_count=1)  # $10.00, was $20.00 -> 50% off
    ctx = ObservationContext(last_ok_price_cents=2000, listing_unit_count=1)
    result = validate_observation(obs, ctx)
    assert "unit_mismatch" not in result.reasons


def test_no_false_positive_for_known_single_unit_when_listing_count_unknown():
    # (c) Same genuine 50% markdown, but the listing's stored unit_count is
    # unknown -- the ratio heuristic runs, but the offer's own claimed
    # unit_count (1) doesn't match a 0.5 ratio (candidates are 1/1 and 1),
    # so no false positive.
    obs = _mk_obs(price_cents=1000, unit_count=1)  # $10.00, was $20.00 -> 50% off
    ctx = ObservationContext(last_ok_price_cents=2000, listing_unit_count=None)
    result = validate_observation(obs, ctx)
    assert "unit_mismatch" not in result.reasons


def test_ratio_exactly_half_with_zero_pack_info_is_not_flagged_ambiguous_case():
    # AMBIGUITY DECISION: with NO pack-count evidence at all (both
    # obs.unit_count and ctx.listing_unit_count unknown), a ratio of
    # exactly 0.5 is indistinguishable from a genuine 50%+ single-unit
    # markdown -- deal_rules.yaml's own headline threshold. Per
    # architecture 6.2 ("real deals must not be silently dropped"), this
    # case is deliberately NOT flagged unit_mismatch: n=2 is excluded from
    # the guessed-multiple set precisely to avoid suppressing the
    # platform's most common real deal shape. See the DEFECT FIX comment
    # in fpt/pipeline/validate.py for the full rationale.
    obs = _mk_obs(price_cents=1000, unit_count=None)  # $10.00, was $20.00 -> ratio 0.5
    ctx = ObservationContext(last_ok_price_cents=2000, listing_unit_count=None)
    result = validate_observation(obs, ctx)
    assert "unit_mismatch" not in result.reasons
