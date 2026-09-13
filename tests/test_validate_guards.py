from fpt.core.models import Availability
from fpt.pipeline.validate import (
    ObservationContext,
    ObservationInput,
    validate_observation,
)

BANDS = {"rod": {"floor_cents": 1500, "ceiling_cents": 200000}}
THRESHOLDS = {
    "large_move_min_pct": 35.0,
    "large_move_max_pct": 300.0,
    "unit_mismatch_tolerance_pct": 5.0,
    "discovery_collapse_pct": 70.0,
}


def _base_obs(**overrides) -> ObservationInput:
    defaults = dict(
        price_cents=13000,
        currency="USD",
        category="rod",
        claimed_reference_cents=None,
        on_clearance=False,
        unit_count=None,
        variant_key_observed=None,
        availability=Availability.IN_STOCK,
        condition_wording_mapped=True,
        is_used_page_type=False,
    )
    defaults.update(overrides)
    return ObservationInput(**defaults)


def test_missing_price_is_rejected():
    obs = _base_obs(price_cents=None)
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "REJECTED"
    assert "invalid_price_or_currency" in result.reasons


def test_zero_price_is_rejected():
    obs = _base_obs(price_cents=0)
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "REJECTED"


def test_non_usd_is_rejected():
    obs = _base_obs(currency="CAD")
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "REJECTED"


def test_price_below_category_floor_is_suspect():
    obs = _base_obs(price_cents=1000)  # below rod floor of 1500
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "out_of_band" in result.reasons


def test_price_above_category_ceiling_is_suspect():
    obs = _base_obs(price_cents=300000)
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "out_of_band" in result.reasons


def test_large_drop_vs_last_ok_is_suspect_but_still_a_deal_candidate():
    # 130.00 -> 30.00 is ~23% of last OK, below the 35% floor -> large_move.
    obs = _base_obs(price_cents=3000)
    ctx = ObservationContext(last_ok_price_cents=13000)
    result = validate_observation(obs, ctx, category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "large_move" in result.reasons


def test_normal_price_move_is_ok():
    obs = _base_obs(price_cents=12000)
    ctx = ObservationContext(last_ok_price_cents=13000)
    result = validate_observation(obs, ctx, category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "OK"
    assert result.reasons == []


def test_claimed_reference_not_above_price_while_on_clearance_is_suspect():
    obs = _base_obs(on_clearance=True, claimed_reference_cents=10000, price_cents=13000)
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "claimed_not_above_price" in result.reasons


def test_variant_identity_changed_is_suspect():
    obs = _base_obs(variant_key_observed="length_in=90|power=M")
    ctx = ObservationContext(listing_variant_key_observed="length_in=84|power=M")
    result = validate_observation(obs, ctx, category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "variant_identity_changed" in result.reasons


def test_variant_collapse_is_suspect():
    obs = _base_obs()
    ctx = ObservationContext(previous_variant_count=20, current_variant_count=5)
    result = validate_observation(obs, ctx, category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "variant_collapse" in result.reasons


def test_price_flattening_is_suspect():
    obs = _base_obs()
    ctx = ObservationContext(previous_distinct_price_count=5, current_distinct_price_count=1)
    result = validate_observation(obs, ctx, category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "price_flattening" in result.reasons


def test_unmapped_condition_on_non_used_page_is_suspect():
    obs = _base_obs(condition_wording_mapped=False, is_used_page_type=False)
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert result.quality == "SUSPECT"
    assert "condition_unknown" in result.reasons


def test_unmapped_condition_on_used_page_falls_back_gracefully():
    # Adapter already resolves this to USED_UNGRADED upstream; validator
    # must not double-flag it as condition_unknown on a USED page type.
    obs = _base_obs(condition_wording_mapped=False, is_used_page_type=True)
    result = validate_observation(obs, ObservationContext(), category_bands=BANDS, thresholds=THRESHOLDS)
    assert "condition_unknown" not in result.reasons
