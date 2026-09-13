"""Unit tests for the Fishing Online (fishingonline.com) adapter, against
real (fetched 2026-09-12, trimmed) and lightly-adapted fixtures.

Live endpoints the fixtures were captured from:
- collection_sale.json: https://www.fishingonline.com/collections/sale/products.json?limit=5
  (trimmed to 2 of the 5 returned products, each trimmed to a handful of
  variants -- kept the "Owner Ultrahead Finesse Jigs" product, whose 9
  variants all share one price with some unavailable, and the "412 Bait
  Company Free Worm" product, whose 33 variants span two distinct prices,
  to exercise both the uniform-price and price-varies discovery paths).
- product_confirm.js: https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs.js
  (trimmed from 9 variants to 3; all fields including barcodes are the
  real captured values).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.fishingonline import FishingOnlineAdapter
from fpt.core.models import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fishingonline"


@dataclasses.dataclass
class _FakeTask:
    id: int
    url: str
    page_type: PageType
    params: dict


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _response(
    body: bytes,
    page_type: PageType,
    url: str,
    params: dict | None = None,
    status: int = 200,
) -> FetchResponse:
    request = FetchRequest(
        task_id=1,
        page_type=page_type,
        url=url,
        params=params or {},
    )
    return FetchResponse(
        request=request,
        status=status,
        final_url=url,
        headers={},
        body=body,
        elapsed_ms=100,
        fetched_at=datetime(2026, 9, 12, tzinfo=timezone.utc),
        egress_mode="DIRECT",
        snapshot_ref="snap://test",
    )


@pytest.fixture()
def adapter() -> FishingOnlineAdapter:
    return FishingOnlineAdapter()


def test_capabilities_match_adapter_matrix(adapter: FishingOnlineAdapter) -> None:
    assert adapter.slug == "fishingonline"
    assert adapter.base_url == "https://www.fishingonline.com"
    assert adapter.sale_collection_handle == "sale"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.exposes_used_offers is False
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset(
        {PageType.PRODUCT, PageType.CLEARANCE_LISTING}
    )


def test_discovery_url_uses_sale_not_clearance_handle(adapter: FishingOnlineAdapter) -> None:
    # H2/gotcha from the research doc: `/collections/clearance` is empty on
    # this host -- the real handle is `sale`.
    assert adapter.discovery_url() == (
        "https://www.fishingonline.com/collections/sale/products.json"
    )


def test_build_request_appends_js_for_product_page(adapter: FishingOnlineAdapter) -> None:
    task = _FakeTask(
        id=7,
        url="https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs",
        page_type=PageType.PRODUCT,
        params={},
    )
    request = adapter.build_request(task)
    assert request.task_id == 7
    assert request.url == (
        "https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs.js"
    )
    assert request.render_js is False


def test_build_request_leaves_discovery_url_unchanged(adapter: FishingOnlineAdapter) -> None:
    task = _FakeTask(
        id=8,
        url=adapter.discovery_url(),
        page_type=PageType.CLEARANCE_LISTING,
        params={"category_hint": "other"},
    )
    request = adapter.build_request(task)
    assert request.url == adapter.discovery_url()


class TestCollectionDiscovery:
    def test_parses_ok_with_one_discovered_item_per_product(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(
                body,
                PageType.CLEARANCE_LISTING,
                url=adapter.discovery_url(),
                params={"category_hint": "other"},
            )
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.listings == ()
        assert len(result.discovered) == 2

    def test_uniform_price_product_picks_available_variant_and_is_not_a_range(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        jigs = next(
            item for item in result.discovered if "Ultrahead" in item.title_raw
        )
        assert jigs.price_cents == 575
        assert jigs.price_is_range is False
        assert jigs.claimed_reference_cents == 650
        assert jigs.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT
        assert jigs.product_url == (
            "https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs"
        )

    def test_price_varies_product_is_marked_a_range_and_picks_cheapest_available(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        worm = next(
            item for item in result.discovered if "Free Worm" in item.title_raw
        )
        assert worm.price_is_range is True
        # cheapest AVAILABLE variant among the trimmed set is 4.49 (the
        # 4.49/unavailable variant is skipped in favor of an available one
        # at the same price) -- never the unavailable one, and never
        # picking the pricier 4.86 tier.
        assert worm.price_cents == 449
        assert worm.claimed_reference_cents == 598

    def test_discovered_items_are_never_observations(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.listings == ()


class TestProductConfirm:
    def test_parses_ok_with_one_listing_per_variant(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs.js",
            )
        )
        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 3
        assert result.discovered == ()

    def test_canonical_product_url_has_js_suffix_stripped(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs.js",
            )
        )
        assert all(
            listing.url == "https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs"
            for listing in result.listings
        )

    def test_variant_price_barcode_and_claimed_reference(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs.js",
            )
        )
        by_sku = {listing.retailer_sku: listing for listing in result.listings}
        first = by_sku["5149-901"]
        assert first.gtin_raw == ["054831004898"]
        offer = first.offers[0]
        assert offer.price_cents == 575
        assert offer.claimed_reference_cents == 650
        assert offer.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT
        assert offer.on_clearance is True
        assert offer.availability == Availability.IN_STOCK
        assert offer.seller_key == "fishingonline"
        assert offer.condition == Condition.NEW

    def test_unavailable_variant_reports_out_of_stock(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs.js",
            )
        )
        oos = next(l for l in result.listings if l.retailer_sku == "5149-011")
        assert oos.offers[0].availability == Availability.OUT_OF_STOCK

    def test_brand_taken_from_vendor(self, adapter: FishingOnlineAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://www.fishingonline.com/products/owner-ultrahead-finesse-jigs.js",
            )
        )
        assert all(l.brand_raw == "Owner" for l in result.listings)


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: FishingOnlineAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://www.fishingonline.com/products/x.js")
        )
        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.listings == ()

    def test_empty_collection_is_ok_with_no_discovered_items(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        # A validly-shaped {"products": []} response is a legitimate "no
        # sale items right now" state, not an error.
        body = _load("empty_response.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.discovered == ()

    def test_empty_body_on_product_page_is_empty(self, adapter: FishingOnlineAdapter) -> None:
        result = adapter.parse(
            _response(b"", PageType.PRODUCT, url="https://www.fishingonline.com/products/x.js")
        )
        assert result.outcome == ResponseOutcome.EMPTY

    def test_404_is_not_found(self, adapter: FishingOnlineAdapter) -> None:
        result = adapter.parse(
            _response(
                b"",
                PageType.PRODUCT,
                url="https://www.fishingonline.com/products/x.js",
                status=404,
            )
        )
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: FishingOnlineAdapter) -> None:
        garbage = b"\x00\x01\xff not json at all { broken"
        result = adapter.parse(
            _response(garbage, PageType.PRODUCT, url="https://www.fishingonline.com/products/x.js")
        )
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present_for_each_page_type(
        self, adapter: FishingOnlineAdapter
    ) -> None:
        product_sentinels = adapter.content_sentinels(PageType.PRODUCT)
        listing_sentinels = adapter.content_sentinels(PageType.CLEARANCE_LISTING)
        assert len(product_sentinels) > 0
        assert len(listing_sentinels) > 0
        assert product_sentinels != listing_sentinels
