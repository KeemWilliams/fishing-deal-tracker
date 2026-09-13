"""Integration tests for fpt.store.deals's DB-facing reference resolution
(H4 cross-retailer guards) and the H2 expiry sweep. Uses the same seeded-
retailer + rollback-per-test convention as tests/integration/test_export_
pipeline.py.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fpt.store import deals as deals_store

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


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _seed_reference_scenario(db, *, own_unit_count, ref_unit_count, mark_gtin_conflict=False):
    """One 'own' NEW offer at tackle_warehouse, one candidate reference NEW
    offer at academy, both matched to the same product_variant via GTIN
    (match_status='auto_gtin'). Returns (own_offer_id, own_variant_id,
    own_unit_count, own_category)."""
    cur = db.cursor()
    uid = _uid()
    now = datetime.now(timezone.utc)

    tw_id = get_retailer_id(cur, "tackle_warehouse")
    ac_id = get_retailer_id(cur, "academy")
    brand_id = seed_brand(cur, f"H4Brand-{uid}")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug=f"h4-rod-{uid}", category="rod",
        model_key=f"model-{uid}", variant_key=f"vk-{uid}", label="7ft Medium",
        gtin14=["00000000000001"],
    )

    tw_seller = seed_seller(cur, retailer_id=tw_id, seller_key=f"tw_{uid}", seller_type="FIRST_PARTY", name="Tackle Warehouse")
    ac_seller = seed_seller(cur, retailer_id=ac_id, seller_key=f"ac_{uid}", seller_type="FIRST_PARTY", name="Academy")

    own_listing_id = seed_listing(
        cur, retailer_id=tw_id, retailer_sku=f"own-{uid}", url=f"https://www.tacklewarehouse.com/{uid}",
        title_raw="Test Rod", variant_label_raw="7ft Medium", attributes_raw={}, attributes_norm={},
        gtin14=["00000000000001"], unit_count=own_unit_count, variant_id=variant_id,
        variant_key_observed=f"vk-{uid}", match_status="auto_gtin",
    )
    own_offer_id = seed_offer(cur, listing_id=own_listing_id, seller_id=tw_seller, offer_key=f"own-offer-{uid}", condition="NEW")

    ref_listing_id = seed_listing(
        cur, retailer_id=ac_id, retailer_sku=f"ref-{uid}", url=f"https://www.academy.com/p/{uid}",
        title_raw="Test Rod", variant_label_raw="7ft Medium", attributes_raw={}, attributes_norm={},
        gtin14=["00000000000001"], unit_count=ref_unit_count, variant_id=variant_id,
        variant_key_observed=f"vk-ref-{uid}", match_status="auto_gtin",
    )
    ref_offer_id = seed_offer(cur, listing_id=ref_listing_id, seller_id=ac_seller, offer_key=f"ref-offer-{uid}", condition="NEW")

    crawl_task_id = seed_crawl_task(
        cur, retailer_id=ac_id, kind="BASELINE", page_type="PRODUCT",
        url=f"https://www.academy.com/p/{uid}", dedupe_key=f"dedupe-h4-{uid}",
    )
    insert_observation(
        cur, offer_id=ref_offer_id, crawl_task_id=crawl_task_id, task_kind="BASELINE",
        observed_at=now, price_cents=9999, on_clearance=False, availability="IN_STOCK",
        quality="OK", reasons=[],
    )

    if mark_gtin_conflict:
        cur.execute(
            "INSERT INTO match_candidates (listing_id, variant_id, score, reason) VALUES (%s, %s, 0.5, 'gtin_attr_conflict')",
            (ref_listing_id, variant_id),
        )

    return own_offer_id, variant_id


class TestResolveCrossRetailerH4Guards:
    def test_matching_unit_count_finds_reference(self, db):
        own_offer_id, variant_id = _seed_reference_scenario(db, own_unit_count=5, ref_unit_count=5)
        cur = db.cursor()
        result = deals_store.resolve_cross_retailer(
            cur, offer_id=own_offer_id, variant_id=variant_id, condition_group="NEW",
            unit_count=5, category="rod",
        )
        assert result is not None
        assert result.price_cents == 9999

    def test_mismatched_unit_count_excludes_reference(self, db):
        own_offer_id, variant_id = _seed_reference_scenario(db, own_unit_count=5, ref_unit_count=25)
        cur = db.cursor()
        result = deals_store.resolve_cross_retailer(
            cur, offer_id=own_offer_id, variant_id=variant_id, condition_group="NEW",
            unit_count=5, category="rod",
        )
        assert result is None

    def test_own_null_unit_count_never_matches_anything(self, db):
        # H4: "NULL is not comparable, never a match" -- even a reference
        # listing that ALSO has unit_count=NULL must not be treated as a
        # match; the whole R2 lookup is disqualified when our own
        # unit_count is unknown.
        own_offer_id, variant_id = _seed_reference_scenario(db, own_unit_count=None, ref_unit_count=None)
        cur = db.cursor()
        result = deals_store.resolve_cross_retailer(
            cur, offer_id=own_offer_id, variant_id=variant_id, condition_group="NEW",
            unit_count=None, category="rod",
        )
        assert result is None

    def test_gtin_attr_conflict_listing_excluded_even_with_matching_unit_count(self, db):
        own_offer_id, variant_id = _seed_reference_scenario(
            db, own_unit_count=5, ref_unit_count=5, mark_gtin_conflict=True
        )
        cur = db.cursor()
        result = deals_store.resolve_cross_retailer(
            cur, offer_id=own_offer_id, variant_id=variant_id, condition_group="NEW",
            unit_count=5, category="rod",
        )
        assert result is None

    def test_get_offer_context_loads_unit_count_and_category_from_stored_listing_row(self, db):
        own_offer_id, variant_id = _seed_reference_scenario(db, own_unit_count=5, ref_unit_count=5)
        cur = db.cursor()
        ctx = deals_store.get_offer_context(cur, own_offer_id)
        assert ctx["unit_count"] == 5
        assert ctx["category"] == "rod"
        assert ctx["variant_id"] == variant_id


class TestExpireStaleDeals:
    def test_expires_active_deal_with_no_recent_observation(self, db):
        cur = db.cursor()
        uid = _uid()
        now = datetime.now(timezone.utc)
        retailer_id = get_retailer_id(cur, "tackle_warehouse")
        brand_id = seed_brand(cur, f"ExpireBrand-{uid}")
        _, variant_id = seed_product_variant(
            cur, brand_id=brand_id, slug=f"expire-rod-{uid}", category="rod",
            model_key=f"model-{uid}", variant_key=f"vk-{uid}", label="7ft Medium",
        )
        seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key=f"seller-{uid}", seller_type="FIRST_PARTY")
        listing_id = seed_listing(
            cur, retailer_id=retailer_id, retailer_sku=f"sku-{uid}", url=f"https://www.tacklewarehouse.com/{uid}",
            title_raw="Test Rod", variant_label_raw="7ft Medium", attributes_raw={}, attributes_norm={},
            gtin14=[], unit_count=None, variant_id=variant_id, variant_key_observed=f"vk-{uid}", match_status="manual",
        )
        offer_id = seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key=f"offer-{uid}", condition="NEW")
        crawl_task_id = seed_crawl_task(
            cur, retailer_id=retailer_id, kind="CONFIRM", page_type="PRODUCT",
            url=f"https://www.tacklewarehouse.com/{uid}", dedupe_key=f"dedupe-expire-{uid}",
        )
        old_obs_at = now - timedelta(hours=48)
        obs_id = insert_observation(
            cur, offer_id=offer_id, crawl_task_id=crawl_task_id, task_kind="CONFIRM",
            observed_at=old_obs_at, price_cents=6497, on_clearance=True, availability="IN_STOCK",
            quality="OK", reasons=[],
        )
        deal_id = insert_deal(
            cur, offer_id=offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="ACTIVE",
            detected_observation_id=obs_id, confirming_observation_id=obs_id,
            detected_via="PRODUCT_PAGE", price_cents=6497,
            reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=12999,
            discount_pct=50.0,
        )

        expired_count = deals_store.expire_stale_deals(db, now, stale_hours=24)
        assert expired_count == 1

        cur.execute("SELECT status, expire_reason FROM deals WHERE id = %s", (deal_id,))
        status, expire_reason = cur.fetchone()
        assert status == "EXPIRED"
        assert expire_reason == "not_observed"

    def test_does_not_expire_active_deal_with_recent_observation(self, db):
        cur = db.cursor()
        uid = _uid()
        now = datetime.now(timezone.utc)
        retailer_id = get_retailer_id(cur, "tackle_warehouse")
        brand_id = seed_brand(cur, f"FreshBrand-{uid}")
        _, variant_id = seed_product_variant(
            cur, brand_id=brand_id, slug=f"fresh-rod-{uid}", category="rod",
            model_key=f"model-{uid}", variant_key=f"vk-{uid}", label="7ft Medium",
        )
        seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key=f"seller-fresh-{uid}", seller_type="FIRST_PARTY")
        listing_id = seed_listing(
            cur, retailer_id=retailer_id, retailer_sku=f"sku-fresh-{uid}", url=f"https://www.tacklewarehouse.com/f-{uid}",
            title_raw="Test Rod", variant_label_raw="7ft Medium", attributes_raw={}, attributes_norm={},
            gtin14=[], unit_count=None, variant_id=variant_id, variant_key_observed=f"vk-{uid}", match_status="manual",
        )
        offer_id = seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key=f"offer-fresh-{uid}", condition="NEW")
        crawl_task_id = seed_crawl_task(
            cur, retailer_id=retailer_id, kind="CONFIRM", page_type="PRODUCT",
            url=f"https://www.tacklewarehouse.com/f-{uid}", dedupe_key=f"dedupe-fresh-{uid}",
        )
        recent_obs_at = now - timedelta(hours=1)
        obs_id = insert_observation(
            cur, offer_id=offer_id, crawl_task_id=crawl_task_id, task_kind="CONFIRM",
            observed_at=recent_obs_at, price_cents=6497, on_clearance=True, availability="IN_STOCK",
            quality="OK", reasons=[],
        )
        deal_id = insert_deal(
            cur, offer_id=offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="ACTIVE",
            detected_observation_id=obs_id, confirming_observation_id=obs_id,
            detected_via="PRODUCT_PAGE", price_cents=6497,
            reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=12999,
            discount_pct=50.0,
        )

        deals_store.expire_stale_deals(db, now, stale_hours=24)

        cur.execute("SELECT status FROM deals WHERE id = %s", (deal_id,))
        assert cur.fetchone()[0] == "ACTIVE"
