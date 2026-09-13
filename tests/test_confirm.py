from datetime import datetime, timedelta, timezone

from fpt.deals.confirm import ConfirmContext, confirm_candidate

DETECTED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


def _base_ctx(**overrides) -> ConfirmContext:
    defaults = dict(
        detected_at=DETECTED_AT,
        confirming_observed_at=DETECTED_AT + timedelta(minutes=15),
        detected_via="DISCOVERY_GRID",
        confirming_page_type="PRODUCT",
        detected_retailer_sku="MBTSR706M",
        confirming_retailer_sku="MBTSR706M",
        detected_offer_key="MBTSR706M",
        confirming_offer_key="MBTSR706M",
        detected_condition="NEW",
        confirming_condition="NEW",
        detected_seller_key="tackle_warehouse",
        confirming_seller_key="tackle_warehouse",
        detected_variant_key_observed="length_in=90|power=M",
        confirming_variant_key_observed="length_in=90|power=M",
        listing_variant_key_observed="length_in=90|power=M",
        detected_unit_count=None,
        confirming_unit_count=None,
        listing_unit_count=None,
        confirming_quality="OK",
        confirming_quality_reasons=[],
        detected_price_cents=6497,
        confirming_price_cents=6497,
        recomputed_discount_pct=50.0,
        recomputed_reference_variant_key="length_in=90|power=M",
        recomputed_reference_condition_group="NEW",
        offer_variant_key="length_in=90|power=M",
        offer_condition_group="NEW",
        confirming_price_cents_for_floor=6497,
        category_floor_cents=1500,
    )
    defaults.update(overrides)
    return ConfirmContext(**defaults)


def test_happy_path_confirms_active():
    result = confirm_candidate(_base_ctx())
    assert result.status == "ACTIVE"
    assert result.reject_reason is None


def test_c1_confirm_too_soon_rejected():
    ctx = _base_ctx(confirming_observed_at=DETECTED_AT + timedelta(minutes=5))
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "confirm_too_soon"


def test_c2_discovery_grid_confirmation_must_be_product_page():
    ctx = _base_ctx(detected_via="DISCOVERY_GRID", confirming_page_type="CLEARANCE_LISTING")
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "confirm_not_from_product_page"


def test_c3_offer_identity_mismatch_on_sku_change():
    ctx = _base_ctx(confirming_retailer_sku="DIFFERENT_SKU")
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "offer_identity_mismatch"


def test_c3_offer_identity_mismatch_on_condition_change():
    ctx = _base_ctx(confirming_condition="USED_GOOD")
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "offer_identity_mismatch"


def test_c4_variant_mismatch_rejected():
    ctx = _base_ctx(confirming_variant_key_observed="length_in=84|power=M")
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "variant_mismatch"


def test_c4_unit_count_mismatch_rejected():
    ctx = _base_ctx(detected_unit_count=13, confirming_unit_count=13, listing_unit_count=10)
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "variant_mismatch"


def test_c5_suspect_large_move_only_is_allowed_through():
    ctx = _base_ctx(confirming_quality="SUSPECT", confirming_quality_reasons=["large_move"])
    result = confirm_candidate(ctx)
    assert result.status == "ACTIVE"


def test_c5_suspect_other_reason_rejected():
    ctx = _base_ctx(confirming_quality="SUSPECT", confirming_quality_reasons=["out_of_band"])
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "quality:out_of_band"


def test_c5_rejected_quality_is_rejected():
    ctx = _base_ctx(confirming_quality="REJECTED", confirming_quality_reasons=[])
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "quality:rejected"


def test_c6_price_increase_within_tolerance_ok():
    ctx = _base_ctx(detected_price_cents=10000, confirming_price_cents=10150)  # +1.5%
    result = confirm_candidate(ctx)
    assert result.status == "ACTIVE"


def test_c6_price_increase_beyond_tolerance_rejected():
    ctx = _base_ctx(detected_price_cents=10000, confirming_price_cents=10300)  # +3%
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "price_changed"


def test_c6_price_decrease_always_fine():
    ctx = _base_ctx(detected_price_cents=10000, confirming_price_cents=5000)
    result = confirm_candidate(ctx)
    assert result.status == "ACTIVE"


def test_c7_below_threshold_on_confirm_rejected():
    ctx = _base_ctx(recomputed_discount_pct=42.0)
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "below_threshold_on_confirm"


def test_c8_reference_scope_mismatch_on_variant_key():
    ctx = _base_ctx(recomputed_reference_variant_key="length_in=84|power=M")
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "reference_scope_mismatch"


def test_c8_reference_scope_mismatch_on_condition_group():
    ctx = _base_ctx(recomputed_reference_condition_group="USED")
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "reference_scope_mismatch"


def test_c8_reference_scope_mismatch_on_unit_count():
    # H3/H4: the reference offer's own unit_count (e.g. a 25-pack cross-
    # retailer reference) must match this offer's own unit_count (a
    # 5-pack) -- a mismatched pack size is not the same product even when
    # linked to the same variant_id via GTIN.
    ctx = _base_ctx(
        offer_unit_count=5,
        recomputed_reference_unit_count=25,
    )
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "reference_scope_mismatch"


def test_c8_matching_unit_counts_pass():
    ctx = _base_ctx(offer_unit_count=5, recomputed_reference_unit_count=5)
    result = confirm_candidate(ctx)
    assert result.status == "ACTIVE"


def test_c8_unit_count_none_on_either_side_does_not_reject():
    # NULL is never comparable -- absence of pack-count evidence on either
    # side must not be treated as a mismatch (it would incorrectly reject
    # every unit_count-less category).
    ctx = _base_ctx(offer_unit_count=None, recomputed_reference_unit_count=25)
    result = confirm_candidate(ctx)
    assert result.status == "ACTIVE"


def test_c9_held_review_band_85_to_95():
    ctx = _base_ctx(recomputed_discount_pct=90.0)
    result = confirm_candidate(ctx)
    assert result.status == "HELD_REVIEW"
    assert result.hold_reason == "discount_85_to_95"


def test_c9_implausible_discount_over_95_rejected():
    ctx = _base_ctx(recomputed_discount_pct=97.0)
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "implausible_discount"


def test_c9_exactly_85_publishes_active():
    ctx = _base_ctx(recomputed_discount_pct=85.0)
    result = confirm_candidate(ctx)
    assert result.status == "ACTIVE"


def test_c10_below_category_floor_rejected():
    ctx = _base_ctx(confirming_price_cents_for_floor=1000, category_floor_cents=1500)
    result = confirm_candidate(ctx)
    assert result.status == "REJECTED"
    assert result.reject_reason == "below_floor"
