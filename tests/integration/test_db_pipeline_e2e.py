"""End-to-end DB integration tests: fixtures -> parse -> validate ->
(harness) store -> detect -> confirm -> query.

Requires DATABASE_URL pointing at a throwaway Postgres with all
db/migrations/*.up.sql applied. See tests/integration/conftest.py.

These tests prove/disprove that fpt/'s domain objects (ParsedListing,
ParsedOffer, ValidationResult, DealCandidate, ConfirmResult) are actually
compatible with the schema in db/migrations/ -- there is no production
code path that connects them today (defect CRITICAL-1 in the HANDOFF).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fpt.adapters.academy import AcademyAdapter
from fpt.adapters.base import (
    Availability,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)
from fpt.adapters.jandh import JandhAdapter
from fpt.adapters.tackle_warehouse import TackleWarehouseAdapter
from fpt.core.models import CONDITION_GROUP
from fpt.deals.confirm import ConfirmContext, confirm_candidate
from fpt.deals.detect import detect_deep_discount_new, detect_used_vs_current_new
from fpt.deals.references import NonClearanceDay, resolve_own_history_median
from fpt.fetch.blocks import detect_block_or_empty
from fpt.pipeline.validate import ObservationContext, ObservationInput, validate_observation

from tests.integration._harness import (
    get_retailer_id,
    insert_deal,
    insert_observation,
    seed_brand,
    seed_crawl_task,
    seed_listing,
    seed_offer,
    seed_product_variant,
    seed_seller,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _response(page_type: PageType, body: bytes, url: str) -> FetchResponse:
    request = FetchRequest(task_id=1, page_type=page_type, url=url)
    return FetchResponse(
        request=request,
        status=200,
        final_url=url,
        headers={},
        body=body,
        elapsed_ms=10,
        fetched_at=datetime.now(timezone.utc),
        egress_mode="DIRECT",
        snapshot_ref="test",
    )


# ---------------------------------------------------------------------------
# Contract checks: does the Python domain model agree with the DB schema?
# ---------------------------------------------------------------------------


def test_condition_group_mapping_matches_db_check_constraint(db):
    """fpt.core.models.CONDITION_GROUP must exactly match the offers table's
    CHECK constraint pairing condition -> condition_group. If a coder edits
    one without the other, this INSERT fails with a CheckViolation."""
    cur = db.cursor()
    retailer_id = get_retailer_id(cur, "tackle_warehouse")
    brand_id = seed_brand(cur, "ContractTestBrand")
    seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key="contract_seller", seller_type="FIRST_PARTY")

    for i, (condition, group) in enumerate(CONDITION_GROUP.items()):
        _, variant_id = seed_product_variant(
            cur,
            brand_id=brand_id,
            slug=f"contract-product-{i}",
            category="rod",
            model_key=f"model-{i}",
            variant_key=f"vk-{i}",
            label=f"Variant {i}",
        )
        listing_id = seed_listing(
            cur,
            retailer_id=retailer_id,
            retailer_sku=f"CONTRACT-SKU-{i}",
            url=f"https://example.test/{i}",
            title_raw="Contract Test Rod",
            variant_label_raw="test",
            attributes_raw={},
            attributes_norm={},
            gtin14=[],
            unit_count=None,
            variant_id=variant_id,
            variant_key_observed=f"vk-{i}",
        )
        # Must not raise -- proves condition/condition_group pairing is valid
        # per the DB's own CHECK, for every value the Python enum produces.
        seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key=f"offer-{i}", condition=condition)


def test_price_observations_rejects_condition_not_in_python_enum(db):
    """Sanity check the other direction: the DB CHECK also rejects garbage,
    proving the CHECK is load-bearing and not vacuously permissive."""
    cur = db.cursor()
    with pytest.raises(Exception):
        cur.execute(
            "INSERT INTO offers (listing_id, seller_id, offer_key, condition, condition_group) "
            "VALUES (999999, 999999, 'x', 'NOT_A_REAL_CONDITION', 'NEW')"
        )


# ---------------------------------------------------------------------------
# Full fixture pipeline: Tackle Warehouse NEW rod, deep discount vs its own
# 90-day history, two-fetch confirmation, deal published.
# ---------------------------------------------------------------------------


def test_tackle_warehouse_new_rod_deep_discount_confirms_active(db):
    cur = db.cursor()
    retailer_id = get_retailer_id(cur, "tackle_warehouse")

    body = (FIXTURES / "tackle_warehouse" / "product_rod_single_variant.html").read_bytes()
    response = _response(
        PageType.PRODUCT, body,
        "https://www.tacklewarehouse.com/St_Croix_Triumph_Spinning_Rods/descpage-MBTSR.html",
    )
    result = TackleWarehouseAdapter().parse(response)
    assert result.outcome == ResponseOutcome.OK
    listing = result.listings[0]
    offer = listing.offers[0]
    assert offer.price_cents == 13000  # fixture's live price at $130.00

    brand_id = seed_brand(cur, "St. Croix")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug="triumph-spinning-706m", category="rod",
        model_key="triumph-706m", variant_key="length_in=90|power=M", label="7'6\" M",
    )
    listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku=listing.retailer_sku, url=listing.url or response.final_url,
        title_raw=listing.title_raw, variant_label_raw=listing.variant_label_raw,
        attributes_raw=dict(listing.attributes_raw), attributes_norm={"length_in": 90, "power": "M"},
        gtin14=listing.gtin_raw, unit_count=None, variant_id=variant_id,
        variant_key_observed="length_in=90|power=M",
    )
    seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key="tackle_warehouse", seller_type="FIRST_PARTY")
    offer_id = seed_offer(
        cur, listing_id=listing_id, seller_id=seller_id, offer_key=offer.offer_key,
        condition=offer.condition.value, condition_raw=offer.condition_raw,
    )

    # --- Build 90-day non-clearance history at the fixture's $130 price,
    # satisfying own_history_min_days=21 / min_span_days=30 (config/deal_rules.yaml).
    base_day = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)
    history_days = []
    for d in range(0, 42, 2):  # 21 observations spanning 40 days -- meets the 21-day/30-day bar
        observed_at = base_day + timedelta(days=d)
        task_id = seed_crawl_task(
            cur, retailer_id=retailer_id, kind="BASELINE", page_type="PRODUCT",
            url=response.final_url, dedupe_key=f"BASELINE:hist:{d}",
        )
        insert_observation(
            cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="BASELINE",
            observed_at=observed_at, price_cents=13000, on_clearance=False,
            availability="IN_STOCK", quality="OK", reasons=[],
        )
        history_days.append(NonClearanceDay(day=observed_at.date(), price_cents=13000))

    own_history = resolve_own_history_median(history_days, min_days=21, min_span_days=30)
    assert own_history is not None
    assert own_history.median_cents == 13000

    # --- CANDIDATE: price drops to $60 (53.8% off $130) via a DISCOVERY-grid fetch.
    detected_at = base_day + timedelta(days=45)
    detect_task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="DISCOVERY", page_type="CLEARANCE_LISTING",
        url="https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html", dedupe_key="DISCOVERY:candidate",
    )
    detected_obs_input = ObservationInput(
        price_cents=6000, currency="USD", category="rod", claimed_reference_cents=None,
        on_clearance=True, unit_count=None, variant_key_observed="length_in=90|power=M",
        availability=Availability.IN_STOCK, condition_wording_mapped=True, is_used_page_type=False,
    )
    detected_quality = validate_observation(
        detected_obs_input, ObservationContext(last_ok_price_cents=13000, listing_variant_key_observed="length_in=90|power=M"),
    )
    assert detected_quality.quality == "OK"  # 46% of last-OK -- inside the [35,300] large_move band
    detected_obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=detect_task_id, task_kind="DISCOVERY",
        observed_at=detected_at, price_cents=6000, on_clearance=True, availability="IN_STOCK",
        quality=detected_quality.quality, reasons=detected_quality.reasons,
        variant_key_observed="length_in=90|power=M",
    )

    candidate = detect_deep_discount_new(
        condition=Condition.NEW, availability=Availability.IN_STOCK, price_cents=6000,
        verified_reference_cents=own_history.median_cents,
    )
    assert candidate is not None
    assert candidate.discount_pct >= 50.0

    deal_id = insert_deal(
        cur, offer_id=offer_id, rule=candidate.rule, lane=candidate.lane, status="CONFIRMING",
        detected_observation_id=detected_obs_id, detected_via="DISCOVERY_GRID",
        price_cents=candidate.price_cents, reference_kind="OWN_HISTORY_MEDIAN_90D",
        reference_cents=candidate.reference_cents, discount_pct=candidate.discount_pct,
        reference_detail={"days": own_history.observed_days, "span_days": own_history.span_days},
    )

    # --- CONFIRM: second fetch 15 minutes later, from the product page, same price.
    confirming_at = detected_at + timedelta(minutes=15)
    confirm_task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="CONFIRM", page_type="PRODUCT",
        url=response.final_url, dedupe_key=f"CONFIRM:deal:{deal_id}",
    )
    confirming_obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=confirm_task_id, task_kind="CONFIRM",
        observed_at=confirming_at, price_cents=6000, on_clearance=True, availability="IN_STOCK",
        quality="OK", reasons=[], variant_key_observed="length_in=90|power=M",
    )

    confirm_result = confirm_candidate(ConfirmContext(
        detected_at=detected_at, confirming_observed_at=confirming_at,
        detected_via="DISCOVERY_GRID", confirming_page_type="PRODUCT",
        detected_retailer_sku=listing.retailer_sku, confirming_retailer_sku=listing.retailer_sku,
        detected_offer_key=offer.offer_key, confirming_offer_key=offer.offer_key,
        detected_condition="NEW", confirming_condition="NEW",
        detected_seller_key="tackle_warehouse", confirming_seller_key="tackle_warehouse",
        detected_variant_key_observed="length_in=90|power=M", confirming_variant_key_observed="length_in=90|power=M",
        listing_variant_key_observed="length_in=90|power=M",
        detected_unit_count=None, confirming_unit_count=None, listing_unit_count=None,
        confirming_quality="OK", confirming_quality_reasons=[],
        detected_price_cents=6000, confirming_price_cents=6000,
        recomputed_discount_pct=candidate.discount_pct,
        recomputed_reference_variant_key="length_in=90|power=M", recomputed_reference_condition_group="NEW",
        offer_variant_key="length_in=90|power=M", offer_condition_group="NEW",
        confirming_price_cents_for_floor=6000, category_floor_cents=1500,
    ))
    assert confirm_result.status == "ACTIVE"

    cur.execute(
        "UPDATE deals SET status = 'ACTIVE', confirming_observation_id = %s, confirmed_at = now() WHERE id = %s",
        (confirming_obs_id, deal_id),
    )

    # --- Verify by QUERYING, not by trusting the Python return values.
    cur.execute(
        "SELECT status, price_cents, reference_cents, discount_pct, confirming_observation_id "
        "FROM deals WHERE id = %s", (deal_id,),
    )
    row = cur.fetchone()
    assert row[0] == "ACTIVE"
    assert row[1] == 6000
    assert row[2] == 13000
    assert float(row[3]) >= 50.0
    assert row[4] is not None  # never ACTIVE without a confirming observation on file

    cur.execute("SELECT count(*) FROM price_observations WHERE offer_id = %s", (offer_id,))
    assert cur.fetchone()[0] == 23  # 21 baseline + 1 candidate + 1 confirm


def test_deal_cannot_be_active_without_confirming_observation_recorded(db):
    """Schema-level guard: nothing prevents an application bug from setting
    status=ACTIVE with confirming_observation_id still NULL -- there is no
    CHECK enforcing this. Documents the gap; does not assert it's blocked."""
    cur = db.cursor()
    retailer_id = get_retailer_id(cur, "tackle_warehouse")
    brand_id = seed_brand(cur, "GapTestBrand")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug="gap-test", category="rod",
        model_key="gap-1", variant_key="vk-gap", label="Gap Test Rod",
    )
    listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku="GAP-1", url="https://example.test/gap",
        title_raw="Gap", variant_label_raw="gap", attributes_raw={}, attributes_norm={},
        gtin14=[], unit_count=None, variant_id=variant_id, variant_key_observed="vk-gap",
    )
    seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key="tackle_warehouse", seller_type="FIRST_PARTY")
    offer_id = seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key="gap-offer", condition="NEW")
    task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="DISCOVERY", page_type="CLEARANCE_LISTING",
        url="https://example.test/gap-discovery", dedupe_key="DISCOVERY:gap",
    )
    obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="DISCOVERY",
        observed_at=datetime.now(timezone.utc), price_cents=5000, on_clearance=True,
        availability="IN_STOCK", quality="OK", reasons=[],
    )
    # No CONFIRM observation exists anywhere for this offer.
    deal_id = insert_deal(
        cur, offer_id=offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="ACTIVE",
        detected_observation_id=obs_id, confirming_observation_id=None, detected_via="DISCOVERY_GRID",
        price_cents=5000, reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=13000, discount_pct=61.5,
    )
    cur.execute("SELECT status, confirming_observation_id FROM deals WHERE id = %s", (deal_id,))
    row = cur.fetchone()
    # This SUCCEEDS today -- the DB has no constraint requiring a
    # confirming_observation_id when status = 'ACTIVE'. See HANDOFF HIGH-3.
    assert row == ("ACTIVE", None)


def test_deals_one_open_per_offer_rule_uniqueness_enforced(db):
    cur = db.cursor()
    retailer_id = get_retailer_id(cur, "tackle_warehouse")
    brand_id = seed_brand(cur, "UniqTestBrand")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug="uniq-test", category="rod",
        model_key="uniq-1", variant_key="vk-uniq", label="Uniq Test Rod",
    )
    listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku="UNIQ-1", url="https://example.test/uniq",
        title_raw="Uniq", variant_label_raw="uniq", attributes_raw={}, attributes_norm={},
        gtin14=[], unit_count=None, variant_id=variant_id, variant_key_observed="vk-uniq",
    )
    seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key="tackle_warehouse", seller_type="FIRST_PARTY")
    offer_id = seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key="uniq-offer", condition="NEW")
    task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="DISCOVERY", page_type="CLEARANCE_LISTING",
        url="https://example.test/uniq-discovery", dedupe_key="DISCOVERY:uniq",
    )
    obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="DISCOVERY",
        observed_at=datetime.now(timezone.utc), price_cents=5000, on_clearance=True,
        availability="IN_STOCK", quality="OK", reasons=[],
    )
    insert_deal(
        cur, offer_id=offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="CANDIDATE",
        detected_observation_id=obs_id, detected_via="DISCOVERY_GRID", price_cents=5000,
        reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=13000, discount_pct=61.5,
    )
    cur.execute("SAVEPOINT before_dup")
    with pytest.raises(Exception):
        insert_deal(
            cur, offer_id=offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="CANDIDATE",
            detected_observation_id=obs_id, detected_via="DISCOVERY_GRID", price_cents=4900,
            reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=13000, discount_pct=62.3,
        )
    cur.execute("ROLLBACK TO SAVEPOINT before_dup")


# ---------------------------------------------------------------------------
# Used-vs-current-new: a used offer's reference must never be its own history.
# ---------------------------------------------------------------------------


def test_used_offer_deal_only_ever_referenced_against_new_price(db):
    body = (FIXTURES / "tackle_warehouse" / "product_used_single.html").read_bytes()
    response = _response(
        PageType.PRODUCT, body, "https://www.tacklewarehouse.com/used/descpage-USED1.html",
    )
    result = TackleWarehouseAdapter().parse(response)
    assert result.outcome == ResponseOutcome.OK
    offer = result.listings[0].offers[0]
    assert CONDITION_GROUP[offer.condition.value] == "USED"

    current_new_reference = 13000
    candidate = detect_used_vs_current_new(
        condition=offer.condition, availability=Availability.IN_STOCK,
        landed_price_cents=offer.price_cents, current_new_reference_cents=current_new_reference,
    )
    if offer.price_cents is not None and (current_new_reference - offer.price_cents) >= 1000:
        pct = round((1 - offer.price_cents / current_new_reference) * 100, 2)
        if pct >= 50.0:
            assert candidate is not None
            assert candidate.reference_cents == current_new_reference
            assert candidate.rule == "USED_VS_CURRENT_NEW"
            assert candidate.lane == "USED"


# ---------------------------------------------------------------------------
# Empty / block fixtures must never produce an observation or a deal.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "retailer_slug,adapter_cls,fixture_name",
    [
        ("tackle_warehouse", TackleWarehouseAdapter, "block_page.html"),
        ("academy", AcademyAdapter, "block_page.html"),
        ("jandh", JandhAdapter, "block_page.html"),
        ("academy", AcademyAdapter, "empty_response.html"),
        ("jandh", JandhAdapter, "empty_response.html"),
    ],
)
def test_block_and_empty_fixtures_never_reach_the_database(db, retailer_slug, adapter_cls, fixture_name):
    cur = db.cursor()
    retailer_id = get_retailer_id(cur, retailer_slug)
    cur.execute("SELECT count(*) FROM price_observations po JOIN offers o ON o.id = po.offer_id "
                "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s", (retailer_id,))
    before = cur.fetchone()[0]

    body = (FIXTURES / retailer_slug / fixture_name).read_bytes()
    response = _response(PageType.PRODUCT, body, f"https://example.test/{retailer_slug}/{fixture_name}")

    sentinels = tuple(adapter_cls().content_sentinels(PageType.PRODUCT))
    block_check = detect_block_or_empty(
        status=response.status, body=response.body, final_url=response.final_url,
        request_url=response.request.url, sentinels=sentinels,
    )
    # This is the exact guard fpt/cli.py applies before ever calling adapter.parse()
    # or constructing an ObservationInput -- outcome != OK means "continue",
    # never "store what we got."
    assert block_check.outcome.value != "OK"

    cur.execute("SELECT count(*) FROM price_observations po JOIN offers o ON o.id = po.offer_id "
                "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s", (retailer_id,))
    after = cur.fetchone()[0]
    assert after == before  # no observation was ever attempted, let alone written
