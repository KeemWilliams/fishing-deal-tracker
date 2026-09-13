"""Tackle Warehouse adapter tests, against fixtures trimmed from live pages
fetched 2026-09-12 (see fpt/adapters/tackle_warehouse.py module docstring).
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.base import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
    SellerType,
)
from fpt.adapters.tackle_warehouse import TackleWarehouseAdapter
from fpt.fetch.blocks import detect_block_or_empty

FIXTURES = Path(__file__).parent / "fixtures" / "tackle_warehouse"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _response(page_type: PageType, body: bytes, url: str) -> FetchResponse:
    request = FetchRequest(task_id=1, page_type=page_type, url=url)
    return FetchResponse(
        request=request,
        status=200,
        final_url=url,
        headers={},
        body=body,
        elapsed_ms=100,
        fetched_at=datetime.now(timezone.utc),
        egress_mode="DIRECT",
        snapshot_ref="",
    )


@pytest.fixture()
def adapter() -> TackleWarehouseAdapter:
    return TackleWarehouseAdapter()


def test_build_request_uses_task_fields(adapter: TackleWarehouseAdapter):
    class FakeTask:
        id = 42
        url = "https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html"
        page_type = PageType.CLEARANCE_LISTING
        params: dict = {}

    request = adapter.build_request(FakeTask())
    assert request.task_id == 42
    assert request.page_type == PageType.CLEARANCE_LISTING
    assert request.url == FakeTask.url


class TestClearanceListing:
    def test_discovers_items_with_msrp_asterisk_and_bare_was_price(
        self, adapter: TackleWarehouseAdapter
    ):
        response = _response(
            PageType.CLEARANCE_LISTING,
            _load("clearance_rods.html"),
            "https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html",
        )
        result = adapter.parse(response)

        assert result.outcome == ResponseOutcome.OK
        assert len(result.discovered) == 2

        first = result.discovered[0]
        assert first.retailer_product_code == "SGBSG"
        assert first.title_raw == "Savage Gear Battletek Series Swimbait Casting Rods"
        assert first.price_cents == 12997
        assert first.claimed_reference_cents == 26999
        assert first.claimed_reference_kind == ClaimedReferenceKind.MSRP
        assert first.product_url == (
            "https://www.tacklewarehouse.com/Savage_Gear_Battletek_Series_"
            "Swimbait_Casting_Rods/descpage-SGBSG.html"
        )
        assert first.condition_hint is None  # not a used listing

        second = result.discovered[1]
        assert second.claimed_reference_cents == 14299
        assert second.claimed_reference_kind == ClaimedReferenceKind.WAS  # no asterisk

    def test_strips_trailing_percent_0d_from_product_url(self, adapter: TackleWarehouseAdapter):
        response = _response(
            PageType.CLEARANCE_LISTING,
            _load("clearance_rods.html"),
            "https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html",
        )
        result = adapter.parse(response)
        for item in result.discovered:
            assert not item.product_url.lower().endswith("%0d")

    def test_no_pagination_link_returns_none(self, adapter: TackleWarehouseAdapter):
        response = _response(
            PageType.CLEARANCE_LISTING,
            _load("clearance_rods.html"),
            "https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html",
        )
        result = adapter.parse(response)
        assert result.next_page_url is None


class TestUsedListing:
    def test_discovered_items_get_used_condition_hint(self, adapter: TackleWarehouseAdapter):
        response = _response(
            PageType.USED_LISTING,
            _load("used_listing.html"),
            "https://www.tacklewarehouse.com/catpage-USED.html",
        )
        result = adapter.parse(response)

        assert result.outcome == ResponseOutcome.OK
        assert len(result.discovered) == 2
        assert all(item.condition_hint == Condition.USED_UNGRADED for item in result.discovered)

        first = result.discovered[0]
        assert first.claimed_reference_cents == 29999
        assert first.claimed_reference_kind == ClaimedReferenceKind.MSRP

        second = result.discovered[1]
        assert second.claimed_reference_cents is None  # no crossout price on this row


class TestProductPageNew:
    def test_single_variant_rod_with_gtin_and_attributes(self, adapter: TackleWarehouseAdapter):
        response = _response(
            PageType.PRODUCT,
            _load("product_rod_single_variant.html"),
            "https://www.tacklewarehouse.com/St_Croix_Triumph_Spinning_Rods/descpage-MBTSR.html",
        )
        result = adapter.parse(response)

        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 1
        listing = result.listings[0]
        assert listing.retailer_sku == "MBTSR706M"
        assert listing.retailer_product_code == "MBTSR"
        assert listing.brand_raw == "St. Croix"
        assert listing.gtin_raw == ["00780647104070"]
        assert listing.attributes_raw["Spinning Rod Length"] == "7'6\""
        assert listing.attributes_raw["Spinning Rod Power - Spinning Rod Taper"] == "Medium - Fast"

        assert len(listing.offers) == 1
        offer = listing.offers[0]
        assert offer.condition == Condition.NEW
        assert offer.seller_type == SellerType.FIRST_PARTY
        assert offer.price_cents == 13000
        assert offer.stock_qty == 2
        assert offer.stock_qty_is_floor is False
        assert offer.availability == Availability.IN_STOCK
        assert offer.on_clearance is False
        assert offer.claimed_reference_cents is None  # not a closeout row

    def test_multivariant_bait_stock_formats(self, adapter: TackleWarehouseAdapter):
        response = _response(
            PageType.PRODUCT,
            _load("product_bait_multivariant.html"),
            "https://www.tacklewarehouse.com/Zoom_Trick_Worm/descpage-ZTW.html",
        )
        result = adapter.parse(response)

        assert len(result.listings) == 3
        by_sku = {listing.retailer_sku: listing for listing in result.listings}

        floor_offer = by_sku["ZTWBK"].offers[0]
        assert floor_offer.stock_qty == 10
        assert floor_offer.stock_qty_is_floor is True
        assert floor_offer.availability == Availability.IN_STOCK

        numeric_offer = by_sku["ZTWMG"].offers[0]
        assert numeric_offer.stock_qty == 4
        assert numeric_offer.stock_qty_is_floor is False

        restock_offer = by_sku["ZTWBS"].offers[0]
        assert restock_offer.stock_qty is None
        assert restock_offer.availability == Availability.BACKORDER

        # gtin13="0" placeholder must never survive normalization.
        assert by_sku["ZTWBS"].gtin_raw == []
        assert by_sku["ZTWBK"].gtin_raw == ["00751981006382"]


class TestProductPageUsed:
    def test_used_product_page_extracts_condition_and_msrp_from_description(
        self, adapter: TackleWarehouseAdapter
    ):
        response = _response(
            PageType.PRODUCT,
            _load("product_used_single.html"),
            "https://www.tacklewarehouse.com/USED_-_Virtus_Red_Diamond_Spinning_610_Senko/descpage-USED1.html",
        )
        result = adapter.parse(response)

        assert len(result.listings) == 1
        offer = result.listings[0].offers[0]
        assert offer.condition == Condition.USED_LIKE_NEW  # "Excellent" -> USED_LIKE_NEW
        assert offer.condition_raw == "Excellent"
        assert offer.seller_type == SellerType.RETAILER_RESALE
        assert offer.price_cents == 22499
        assert offer.claimed_reference_cents == 29999
        assert offer.claimed_reference_kind == ClaimedReferenceKind.MSRP
        assert offer.on_clearance is True
        assert offer.stock_qty == 1


class TestBlockAndEmptyDetection:
    def test_content_sentinels_present_on_real_pages(self, adapter: TackleWarehouseAdapter):
        for name, page_type in (
            ("clearance_rods.html", PageType.CLEARANCE_LISTING),
            ("product_rod_single_variant.html", PageType.PRODUCT),
        ):
            body = _load(name)
            sentinels = tuple(adapter.content_sentinels(page_type))
            check = detect_block_or_empty(
                status=200, body=body, final_url="x", request_url="x", sentinels=sentinels
            )
            assert check.outcome == ResponseOutcome.OK, name

    def test_block_page_detected(self, adapter: TackleWarehouseAdapter):
        body = _load("block_page.html")
        sentinels = tuple(adapter.content_sentinels(PageType.CLEARANCE_LISTING))
        check = detect_block_or_empty(
            status=200, body=body, final_url="x", request_url="x", sentinels=sentinels
        )
        assert check.outcome == ResponseOutcome.BLOCKED
        assert check.block_signature == "edgesuite"

    def test_parse_never_raises_on_garbage_input(self, adapter: TackleWarehouseAdapter):
        response = _response(PageType.PRODUCT, b"<html><body>nonsense</body></html>", "x")
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.EMPTY
        assert result.listings == ()
