"""Unit tests for fpt.export.build / fpt.export.validate -- no database, no
network. Exercises the row-to-document mapping and the publish rules
(condition always present for used, retailer-claimed kept separate from
verified reference, contract-gap enum mappings)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fpt.export import build
from fpt.export.validate import ExportValidationError, validate_deals_feed, validate_meta, validate_product


def _base_deal_row(**overrides) -> dict:
    row = {
        "deal_id": "dl_test_001",
        "lane": "VERIFIED",
        "rule": "DEEP_DISCOUNT_NEW",
        "product_slug": "test-rod",
        "product_name": "Test Rod",
        "brand": "Acme",
        "category": "rod",
        "variant_pub_id": "vr_test_001",
        "variant_label": "7ft Medium",
        "retailer_slug": "tackle_warehouse",
        "retailer_name": "Tackle Warehouse",
        "retailer_url": "https://www.tacklewarehouse.com/x",
        "condition": "NEW",
        "seller_name": "Tackle Warehouse",
        "seller_type": "FIRST_PARTY",
        "price_cents": 5000,
        "shipping_cents": None,
        "landed_price_cents": None,
        "reference_kind": "OWN_HISTORY_MEDIAN_90D",
        "reference_cents": 10000,
        "reference_detail": {"days": 74},
        "claimed_reference_kind": None,
        "claimed_reference_cents": None,
        "claimed_inflated": None,
        "discount_pct": 50.0,
        "availability": "IN_STOCK",
        "stock_qty": 2,
        "confirmed_at": datetime(2026, 9, 11, 12, 0, 0, tzinfo=timezone.utc),
        "detected_at": datetime(2026, 9, 11, 11, 0, 0, tzinfo=timezone.utc),
        "last_observed_at": datetime(2026, 9, 11, 13, 0, 0, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


class TestBuildDeal:
    def test_verified_deal_has_reference_and_no_claimed(self):
        deal = build.build_deal(_base_deal_row())
        assert deal["reference"]["kind"] == "OWN_HISTORY_MEDIAN_90D"
        assert deal["reference"]["observed_days"] == 74
        assert deal["claimed_reference"] is None
        assert deal["condition"] == "NEW"

    def test_claimed_lane_omits_verified_reference_keeps_claim_separate(self):
        row = _base_deal_row(
            lane="CLAIMED",
            rule="DEEP_DISCOUNT_CLAIMED",
            reference_kind="RETAILER_CLAIMED",
            reference_cents=None,
            reference_detail=None,
            claimed_reference_kind="MSRP",
            claimed_reference_cents=13999,
            claimed_inflated=False,
        )
        deal = build.build_deal(row)
        assert deal["reference"] is None
        assert deal["claimed_reference"] == {
            "kind": "MSRP",
            "cents": 13999,
            "inflated_vs_reference": False,
        }

    def test_used_lane_current_new_maps_to_cross_retailer_new(self):
        row = _base_deal_row(
            lane="USED",
            rule="USED_VS_CURRENT_NEW",
            condition="USED_LIKE_NEW",
            reference_kind="CURRENT_NEW",
            reference_cents=12999,
            reference_detail={},
        )
        deal = build.build_deal(row)
        assert deal["reference"]["kind"] == "CROSS_RETAILER_NEW"
        assert "current in-stock" in deal["reference"]["label"]
        assert deal["condition"] == "USED_LIKE_NEW"

    def test_missing_condition_refuses_to_build(self):
        row = _base_deal_row(condition=None)
        with pytest.raises(ValueError, match="no condition"):
            build.build_deal(row)

    def test_compare_at_claimed_kind_maps_to_list(self):
        row = _base_deal_row(
            lane="CLAIMED",
            reference_kind="RETAILER_CLAIMED",
            reference_cents=None,
            reference_detail=None,
            claimed_reference_kind="COMPARE_AT",
            claimed_reference_cents=8999,
            claimed_inflated=True,
        )
        deal = build.build_deal(row)
        assert deal["claimed_reference"]["kind"] == "LIST"

    def test_marketplace_3p_seller_type_maps_to_marketplace(self):
        row = _base_deal_row(seller_type="MARKETPLACE_3P")
        deal = build.build_deal(row)
        assert deal["seller_type"] == "MARKETPLACE"

    def test_retailer_resale_seller_type_maps_to_first_party(self):
        row = _base_deal_row(seller_type="RETAILER_RESALE")
        deal = build.build_deal(row)
        assert deal["seller_type"] == "FIRST_PARTY"

    def test_backorder_and_unknown_availability_map_to_out_of_stock(self):
        for raw in ("BACKORDER", "UNKNOWN"):
            deal = build.build_deal(_base_deal_row(availability=raw))
            assert deal["availability"] == "OUT_OF_STOCK"

    def test_missing_confirmed_at_falls_back_to_detected_at(self):
        row = _base_deal_row(confirmed_at=None, last_observed_at=None)
        deal = build.build_deal(row)
        assert deal["first_confirmed_at"] == "2026-09-11T11:00:00Z"
        assert deal["last_confirmed_at"] == "2026-09-11T11:00:00Z"

    def test_discount_pct_is_a_json_float_not_decimal(self):
        from decimal import Decimal

        row = _base_deal_row(discount_pct=Decimal("50.00"))
        deal = build.build_deal(row)
        assert isinstance(deal["discount_pct"], float)


class TestBuildDealsFeedAndSchema:
    def test_valid_feed_passes_schema(self):
        feed = build.build_deals_feed([_base_deal_row()], generated_at=datetime.now(timezone.utc))
        validate_deals_feed(feed)  # must not raise

    def test_claimed_lane_with_leaked_reference_fails_cross_check(self):
        feed = {
            "generated_at": "2026-09-12T00:00:00Z",
            "deals": [
                {
                    **build.build_deal(_base_deal_row()),
                    "lane": "CLAIMED",
                }
            ],
        }
        with pytest.raises(ExportValidationError):
            validate_deals_feed(feed)

    def test_verified_lane_without_reference_fails_cross_check(self):
        deal = build.build_deal(_base_deal_row())
        deal["reference"] = None
        feed = {"generated_at": "2026-09-12T00:00:00Z", "deals": [deal]}
        with pytest.raises(ExportValidationError):
            validate_deals_feed(feed)

    def test_bad_enum_value_fails_schema(self):
        deal = build.build_deal(_base_deal_row())
        deal["lane"] = "NOT_A_LANE"
        feed = {"generated_at": "2026-09-12T00:00:00Z", "deals": [deal]}
        with pytest.raises(ExportValidationError):
            validate_deals_feed(feed)


class TestBuildMeta:
    def _retailer_row(self, **overrides) -> dict:
        row = {
            "slug": "tackle_warehouse",
            "name": "Tackle Warehouse",
            "enabled": True,
            "breaker_open_until": None,
            "last_successful_fetch_at": datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc),
            "last_discovery_sweep_at": datetime(2026, 9, 12, 9, 0, 0, tzinfo=timezone.utc),
            "fetched_24h": 100,
            "blocked_24h": 2,
        }
        row.update(overrides)
        return row

    def test_healthy_retailer(self):
        now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
        meta_row = build.build_retailer_meta(self._retailer_row(), now=now)
        assert meta_row["health"] == "HEALTHY"
        assert meta_row["stale"] is False

    def test_stale_after_12h_no_successful_fetch(self):
        now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
        row = self._retailer_row(last_successful_fetch_at=datetime(2026, 9, 11, 23, 0, 0, tzinfo=timezone.utc))
        meta_row = build.build_retailer_meta(row, now=now)
        assert meta_row["stale"] is True
        assert meta_row["health"] == "DEGRADED"

    def test_never_fetched_is_stale(self):
        now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
        row = self._retailer_row(last_successful_fetch_at=None)
        meta_row = build.build_retailer_meta(row, now=now)
        assert meta_row["stale"] is True

    def test_breaker_open_is_failed(self):
        now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
        row = self._retailer_row(breaker_open_until=datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc))
        meta_row = build.build_retailer_meta(row, now=now)
        assert meta_row["health"] == "FAILED"

    def test_high_block_rate_is_degraded(self):
        now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
        row = self._retailer_row(fetched_24h=100, blocked_24h=30)
        meta_row = build.build_retailer_meta(row, now=now)
        assert meta_row["health"] == "DEGRADED"
        assert meta_row["stale"] is False

    def test_meta_passes_schema(self):
        now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)
        counts = build.counts_from_deals(
            [_base_deal_row(), _base_deal_row(lane="USED", condition="USED_GOOD")],
            product_count=1,
            offers_tracked=5,
        )
        meta = build.build_meta(
            retailer_rows=[self._retailer_row()],
            counts=counts,
            generated_at=now,
            export_id=build.build_export_id(generated_at=now),
        )
        validate_meta(meta)
        assert meta["counts"]["verified_deals"] == 1
        assert meta["counts"]["used_deals"] == 1


class TestProductSlugsFromDeals:
    def test_dedupes_and_preserves_first_seen_order(self):
        rows = [
            _base_deal_row(product_slug="a"),
            _base_deal_row(product_slug="b"),
            _base_deal_row(product_slug="a"),
        ]
        assert build.product_slugs_from_deals(rows) == ["a", "b"]


class TestBuildProduct:
    def test_product_with_offers_and_history_passes_schema(self):
        inputs = build.ProductBuildInputs(
            product_slug="test-rod",
            product_name="Test Rod",
            brand="Acme",
            category="rod",
            variants=[
                {
                    "variant_id": 1,
                    "variant_pub_id": "vr_test_001",
                    "variant_label": "7ft Medium",
                    "attributes": {"length_in": 84},
                }
            ],
        )
        inputs.offers_by_variant = {
            1: [
                {
                    "retailer_slug": "tackle_warehouse",
                    "retailer_name": "Tackle Warehouse",
                    "url": "https://www.tacklewarehouse.com/y",
                    "condition": "NEW",
                    "seller_name": "Tackle Warehouse",
                    "seller_type": "FIRST_PARTY",
                    "price_cents": 5000,
                    "shipping_cents": None,
                    "availability": "IN_STOCK",
                    "on_clearance": True,
                    "observed_at": datetime(2026, 9, 11, 13, 0, 0, tzinfo=timezone.utc),
                }
            ]
        }
        inputs.history_by_variant = {
            1: [
                {"variant_id": 1, "retailer_slug": "tackle_warehouse", "condition_group": "NEW", "day": __import__("datetime").date(2026, 9, 1), "price_cents": 10000},
            ]
        }
        product = build.build_product(inputs)
        validate_product(product)
        assert product["variants"][0]["offers"][0]["seller_type"] == "FIRST_PARTY"
        assert product["variants"][0]["history"][0]["date"] == "2026-09-01"
