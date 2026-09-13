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


def test_DEFECT_unit_mismatch_guard_is_dead_code_when_listing_unit_count_unknown():
    """DEFECT (see test-engineer HANDOFF, HIGH-2).

    fpt/pipeline/validate.py's unit_mismatch guard has two branches:

        if ctx.listing_unit_count is not None and obs.unit_count is not None:
            ...
        elif ctx.last_ok_price_cents and obs.unit_count and ctx.listing_unit_count:
            ...

    The `elif` requires `ctx.listing_unit_count` to be truthy to run, but it
    is only ever *reached* when the `if`'s condition was False -- which,
    given the `if` already requires `ctx.listing_unit_count is not None`,
    means `ctx.listing_unit_count` must be None (or `obs.unit_count` is
    None) whenever the `elif` is checked. Either way the `elif` guard for
    "price ratio implies a different pack size than claimed" can never
    fire. It is unreachable dead code.

    Concretely: a multi-pack offer whose price silently drops to price/N
    (a classic per-unit-vs-pack-price scraping bug, called out explicitly
    in this test-engineer's mission as an adversarial case) is NOT flagged
    SUSPECT when the current listing's own unit_count is unknown at
    validation time -- which is exactly the situation a DISCOVERY-grid-only
    sighting is in (the grid row doesn't carry a listing_unit_count; only
    the product page does). That halved price then looks like a legitimate
    50%+ discount to fpt/deals/detect.py with nothing downstream to catch it.

    THIS TEST DOCUMENTS THE BUG AS IT EXISTS TODAY: it asserts the (wrong)
    current behavior so it fails loudly the moment someone fixes the dead
    branch, forcing this test to be updated to assert the CORRECT behavior
    (quality == 'SUSPECT', 'unit_mismatch' in reasons) at that time.
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

    # ACTUAL (buggy) behavior today -- see docstring above.
    assert result.quality == "OK"
    assert "unit_mismatch" not in result.reasons
