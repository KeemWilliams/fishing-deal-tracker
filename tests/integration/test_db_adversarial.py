"""Adversarial cases called out in the test-engineer mission: near-zero
parse errors, variant mismatch, retailer-claimed inflated was-price,
multi-pack pricing, and missing GTIN -- each proven against the real schema.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fpt.adapters.base import Availability, Condition, FetchRequest, FetchResponse, PageType
from fpt.adapters.jandh import JandhAdapter
from fpt.adapters.tackle_warehouse import TackleWarehouseAdapter
from fpt.deals.confirm import ConfirmContext, confirm_candidate
from fpt.deals.detect import detect_deep_discount_claimed
from fpt.deals.references import claimed_inflated

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
        request=request, status=200, final_url=url, headers={}, body=body,
        elapsed_ms=10, fetched_at=datetime.now(timezone.utc), egress_mode="DIRECT", snapshot_ref="test",
    )


def _seed_offer_stack(cur, *, retailer_slug, sku, condition="NEW", category="rod", unit_count=None, gtin14=None):
    retailer_id = get_retailer_id(cur, retailer_slug)
    brand_id = seed_brand(cur, f"AdversarialBrand-{sku}")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug=f"adv-{sku}", category=category,
        model_key=f"model-{sku}", variant_key=f"vk-{sku}", label=f"Variant {sku}",
        unit_count=unit_count, gtin14=gtin14,
    )
    listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku=sku, url=f"https://example.test/{sku}",
        title_raw="Adversarial Test Item", variant_label_raw="v1", attributes_raw={}, attributes_norm={},
        gtin14=gtin14 or [], unit_count=unit_count, variant_id=variant_id, variant_key_observed=f"vk-{sku}",
    )
    seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key=retailer_slug, seller_type="FIRST_PARTY")
    offer_id = seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key=f"offer-{sku}", condition=condition)
    return retailer_id, offer_id


# NOTE: the near-zero-price and unit_mismatch-dead-code defect tests live in
# tests/test_adversarial_pure.py (no DB required) so they always run as part
# of the main 143+-test suite, not only when Docker/DATABASE_URL is present.


# ---------------------------------------------------------------------------
# 3. Retailer-claimed inflated was-price: CLAIMED lane must never be used
#    once a VERIFIED reference exists, and claimed_inflated must be
#    persisted on the deals row for audit even when the deal is VERIFIED.
# ---------------------------------------------------------------------------


def test_inflated_claimed_reference_never_backs_a_deal_when_verified_exists(db):
    verified_reference_cents = 13000  # true 90-day median
    claimed_reference_cents = 29999   # retailer's "was" price, 2.3x verified -- not credible

    assert claimed_inflated(claimed_reference_cents, verified_reference_cents) is True

    # Architecture rule (6.1): a VERIFIED reference always wins; CLAIMED
    # never backs a deal when one exists, regardless of how attractive the
    # claimed discount looks.
    claimed_candidate = detect_deep_discount_claimed(
        condition=Condition.NEW, price_cents=6000, claimed_reference_cents=claimed_reference_cents,
        on_clearance=True, has_verified_reference=True,
    )
    assert claimed_candidate is None

    cur = db.cursor()
    retailer_id, offer_id = _seed_offer_stack(cur, retailer_slug="tackle_warehouse", sku="INFLATED-1")
    task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="DISCOVERY", page_type="CLEARANCE_LISTING",
        url="https://example.test/inflated", dedupe_key="DISCOVERY:inflated",
    )
    obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="DISCOVERY",
        observed_at=datetime.now(timezone.utc), price_cents=6000, on_clearance=True,
        availability="IN_STOCK", quality="OK", reasons=[],
        claimed_reference_cents=claimed_reference_cents, claimed_reference_kind="WAS",
    )
    # The deal is recorded against the VERIFIED reference; claimed_inflated=True
    # is stored purely for audit/display, never as the reference_kind.
    deal_id = insert_deal(
        cur, offer_id=offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="CANDIDATE",
        detected_observation_id=obs_id, detected_via="DISCOVERY_GRID", price_cents=6000,
        reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=verified_reference_cents,
        discount_pct=round((1 - 6000 / verified_reference_cents) * 100, 2),
        claimed_reference_cents=claimed_reference_cents, claimed_reference_kind="WAS",
        claimed_inflated=True,
    )
    cur.execute("SELECT reference_kind, claimed_inflated FROM deals WHERE id = %s", (deal_id,))
    row = cur.fetchone()
    assert row[0] == "OWN_HISTORY_MEDIAN_90D"
    assert row[1] is True


# ---------------------------------------------------------------------------
# 4. Variant mismatch between the clearance/discovery row and the product
#    page must block confirmation outright -- no ACTIVE deal.
# ---------------------------------------------------------------------------


def test_variant_mismatch_between_clearance_and_product_page_rejects_confirmation(db):
    detected_at = datetime(2026, 6, 1, tzinfo=timezone.utc)
    confirming_at = detected_at + timedelta(minutes=15)
    result = confirm_candidate(ConfirmContext(
        detected_at=detected_at, confirming_observed_at=confirming_at,
        detected_via="DISCOVERY_GRID", confirming_page_type="PRODUCT",
        detected_retailer_sku="MBTSR706M", confirming_retailer_sku="MBTSR706M",
        detected_offer_key="MBTSR706M", confirming_offer_key="MBTSR706M",
        detected_condition="NEW", confirming_condition="NEW",
        detected_seller_key="tackle_warehouse", confirming_seller_key="tackle_warehouse",
        # Discovery grid implied a 7'6" M rod; product page confirms a 7'0" M --
        # a real Tackle Warehouse clearance-grid/product-page variant slippage.
        detected_variant_key_observed="length_in=90|power=M",
        confirming_variant_key_observed="length_in=84|power=M",
        listing_variant_key_observed="length_in=84|power=M",
        detected_unit_count=None, confirming_unit_count=None, listing_unit_count=None,
        confirming_quality="OK", confirming_quality_reasons=[],
        detected_price_cents=6000, confirming_price_cents=6000,
        recomputed_discount_pct=53.8,
        recomputed_reference_variant_key="length_in=84|power=M", recomputed_reference_condition_group="NEW",
        offer_variant_key="length_in=84|power=M", offer_condition_group="NEW",
        confirming_price_cents_for_floor=6000, category_floor_cents=1500,
    ))
    assert result.status == "REJECTED"
    assert result.reject_reason == "variant_mismatch"

    cur = db.cursor()
    retailer_id, offer_id = _seed_offer_stack(cur, retailer_slug="tackle_warehouse", sku="VARMISMATCH-1")
    task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="DISCOVERY", page_type="CLEARANCE_LISTING",
        url="https://example.test/varmismatch", dedupe_key="DISCOVERY:varmismatch",
    )
    obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="DISCOVERY",
        observed_at=detected_at, price_cents=6000, on_clearance=True,
        availability="IN_STOCK", quality="OK", reasons=[],
    )
    deal_id = insert_deal(
        cur, offer_id=offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="REJECTED",
        detected_observation_id=obs_id, detected_via="DISCOVERY_GRID", price_cents=6000,
        reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=13000, discount_pct=53.8,
        reject_reason=result.reject_reason,
    )
    cur.execute("SELECT status, reject_reason FROM deals WHERE id = %s", (deal_id,))
    row = cur.fetchone()
    assert row == ("REJECTED", "variant_mismatch")
    # A REJECTED deal must never simultaneously appear in the ACTIVE lane view.
    cur.execute("SELECT count(*) FROM deals WHERE id = %s AND status = 'ACTIVE'", (deal_id,))
    assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# 5. Multi-pack pricing (J&H terminal tackle) and missing GTIN (TW backorder
#    bait variant) both insert cleanly -- the schema tolerates both; the
#    risk is entirely in the missing store.py wiring (see HANDOFF CRITICAL-1),
#    not in the schema itself.
# ---------------------------------------------------------------------------


def test_multi_pack_jandh_offer_inserts_with_correct_unit_count(db):
    body = (FIXTURES / "jandh" / "product_terminal_pack_pricing.html").read_bytes()
    response = _response(PageType.PRODUCT, body, "https://www.jandh.com/gamakatsu-octopus-hooks.html")
    result = JandhAdapter().parse(response)
    five_pack = next(l for l in result.listings if l.retailer_sku == "02420")
    offer = five_pack.offers[0]
    assert offer.unit_count == 5
    assert offer.price_cents == 599

    cur = db.cursor()
    retailer_id = get_retailer_id(cur, "jandh")
    brand_id = seed_brand(cur, "Gamakatsu")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug="octopus-hooks-10-0-5pk", category="terminal",
        model_key="octopus-10-0", variant_key="size=10/0|pack=5", label="10/0 5 Pack", unit_count=5,
        gtin14=list(five_pack.gtin_raw),
    )
    listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku=five_pack.retailer_sku, url=response.final_url,
        title_raw=five_pack.title_raw, variant_label_raw=five_pack.variant_label_raw,
        attributes_raw=dict(five_pack.attributes_raw), attributes_norm={},
        gtin14=list(five_pack.gtin_raw), unit_count=5, variant_id=variant_id,
        variant_key_observed="size=10/0|pack=5",
    )
    seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key="jandh", seller_type="FIRST_PARTY")
    offer_id = seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key=offer.offer_key, condition="NEW")
    task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="ENROLL", page_type="PRODUCT",
        url=response.final_url, dedupe_key="ENROLL:jandh:02420",
    )
    obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="ENROLL",
        observed_at=datetime.now(timezone.utc), price_cents=offer.price_cents, on_clearance=False,
        availability="IN_STOCK", quality="OK", reasons=[], unit_count=5,
    )
    cur.execute("SELECT unit_count, price_cents FROM price_observations WHERE id = %s", (obs_id,))
    row = cur.fetchone()
    assert row == (5, 599)

    # The neighboring 25-pack at a higher total price must never be treated
    # as the "same" offer/price series as the 5-pack -- confirm they land as
    # genuinely distinct offers with distinct unit_count.
    big_pack = next(l for l in result.listings if l.retailer_sku == "02417-25")
    assert big_pack.offers[0].unit_count == 25
    assert big_pack.retailer_sku != five_pack.retailer_sku


def test_missing_gtin_bait_variant_inserts_with_empty_array_not_null(db):
    body = (FIXTURES / "tackle_warehouse" / "product_bait_multivariant.html").read_bytes()
    response = _response(PageType.PRODUCT, body, "https://www.tacklewarehouse.com/Zoom_Trick_Worm/descpage-ZTW.html")
    result = TackleWarehouseAdapter().parse(response)
    backorder = next(l for l in result.listings if l.retailer_sku == "ZTWBS")
    assert backorder.gtin_raw == []  # gtin13="0" placeholder correctly dropped by the normalizer

    cur = db.cursor()
    retailer_id = get_retailer_id(cur, "tackle_warehouse")
    brand_id = seed_brand(cur, "Zoom")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug="trick-worm-bs", category="soft_bait",
        model_key="trick-worm", variant_key="vk-ztwbs", label="Trick Worm BS", gtin14=[],
    )
    listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku=backorder.retailer_sku, url=response.final_url,
        title_raw=backorder.title_raw, variant_label_raw=backorder.variant_label_raw,
        attributes_raw=dict(backorder.attributes_raw), attributes_norm={},
        gtin14=list(backorder.gtin_raw), unit_count=None, variant_id=variant_id,
        variant_key_observed="vk-ztwbs",
    )
    cur.execute("SELECT gtin14 FROM listings WHERE id = %s", (listing_id,))
    row = cur.fetchone()
    assert row[0] == []  # NOT NULL DEFAULT '{}' honored; never NULL for "no barcode found"


# ---------------------------------------------------------------------------
# 6. price_observations.currency (migration 014): defaults to 'USD' when
#    omitted, and the format CHECK rejects anything not exactly [A-Z]{3} --
#    including a correctly-valued-but-wrong-case 'usd'.
# ---------------------------------------------------------------------------


def test_price_observations_currency_defaults_to_usd(db):
    cur = db.cursor()
    retailer_id, offer_id = _seed_offer_stack(cur, retailer_slug="tackle_warehouse", sku="CURRENCY-DEFAULT-1")
    task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="DISCOVERY", page_type="CLEARANCE_LISTING",
        url="https://example.test/currency-default", dedupe_key="DISCOVERY:currency-default",
    )
    obs_id = insert_observation(
        cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="DISCOVERY",
        observed_at=datetime.now(timezone.utc), price_cents=6000, on_clearance=True,
        availability="IN_STOCK", quality="OK", reasons=[],
        # currency intentionally omitted -- must fall back to the column DEFAULT.
    )
    cur.execute("SELECT currency FROM price_observations WHERE id = %s", (obs_id,))
    assert cur.fetchone()[0] == "USD"


def test_price_observations_currency_rejects_lowercase_usd(db):
    cur = db.cursor()
    retailer_id, offer_id = _seed_offer_stack(cur, retailer_slug="tackle_warehouse", sku="CURRENCY-LOWER-1")
    task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="DISCOVERY", page_type="CLEARANCE_LISTING",
        url="https://example.test/currency-lower", dedupe_key="DISCOVERY:currency-lower",
    )
    cur.execute("SAVEPOINT before_bad_currency")
    with pytest.raises(Exception) as excinfo:
        insert_observation(
            cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="DISCOVERY",
            observed_at=datetime.now(timezone.utc), price_cents=6000, on_clearance=True,
            availability="IN_STOCK", quality="OK", reasons=[], currency="usd",
        )
    assert "price_observations_currency_format" in str(excinfo.value)
    cur.execute("ROLLBACK TO SAVEPOINT before_bad_currency")
