"""Unit tests for the Rod Locker (rodlocker.com) adapter, against real
(fetched 2026-09-12, trimmed) fixtures, plus one adapted variant.

Live endpoints the fixtures were captured from:
- collection_sale.json: https://rodlocker.com/collections/sale/products.json?limit=5
  (trimmed to 2 of the 5 returned products, each trimmed to 2 variants --
  kept Power Pro Braided Line, whose variants have a real discount
  [compare_at_price > price], and Power Pro Super 8 Slick V2, whose
  variants have compare_at_price EQUAL to price -- a real, live-observed
  "not actually discounted" case that must never surface a claimed
  reference).
- product_confirm.js: https://rodlocker.com/products/power-pro-braided-line.js
  (trimmed from 92 variants to 2; the first variant's barcode
  "712649106417" is the real captured value -- the second variant's
  barcode "712649106431" is an adapted placeholder in the same UPC family,
  since only one variant of this product was fetched via `.js` during
  this task's research; see HANDOFF).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.rodlocker import RodLockerAdapter
from fpt.core.models import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "rodlocker"


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
def adapter() -> RodLockerAdapter:
    return RodLockerAdapter()


def test_capabilities_match_adapter_matrix(adapter: RodLockerAdapter) -> None:
    assert adapter.slug == "rodlocker"
    assert adapter.base_url == "https://rodlocker.com"
    assert adapter.sale_collection_handle == "sale"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset(
        {PageType.PRODUCT, PageType.CLEARANCE_LISTING}
    )


def test_discovery_url_uses_sale_not_clearance_handle(adapter: RodLockerAdapter) -> None:
    # Same gotcha as fishingonline: `/collections/clearance` is empty here
    # too -- the real handle is `sale`.
    assert adapter.discovery_url() == "https://rodlocker.com/collections/sale/products.json"


class TestCollectionDiscovery:
    def test_parses_ok_with_one_discovered_item_per_product(
        self, adapter: RodLockerAdapter
    ) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.listings == ()
        assert len(result.discovered) == 2

    def test_real_discount_product_gets_claimed_reference(
        self, adapter: RodLockerAdapter
    ) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        braided = next(item for item in result.discovered if "Braided Line" in item.title_raw)
        assert braided.price_is_range is True
        # The cheaper 9.25 variant is unavailable in this fixture; the
        # representative price must be the cheapest AVAILABLE variant
        # (11.25), never the unavailable-but-cheaper one.
        assert braided.price_cents == 1125
        assert braided.claimed_reference_cents == 2250
        assert braided.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT

    def test_compare_at_price_equal_to_price_is_never_a_claimed_reference(
        self, adapter: RodLockerAdapter
    ) -> None:
        # Live-observed case: Power Pro Super 8 Slick V2's variants all
        # show compare_at_price == price. This must never be surfaced as a
        # discount -- the "compare_cents > price_cents" guard in
        # _shopify_collection.py is exactly for this.
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        super8 = next(item for item in result.discovered if "Super 8" in item.title_raw)
        assert super8.price_cents == 2450
        assert super8.claimed_reference_cents is None
        assert super8.claimed_reference_kind is None
        assert super8.price_is_range is False


class TestProductConfirm:
    def test_parses_ok_with_one_listing_per_variant(self, adapter: RodLockerAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://rodlocker.com/products/power-pro-braided-line.js",
            )
        )
        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 2

    def test_variant_price_barcode_and_claimed_reference(
        self, adapter: RodLockerAdapter
    ) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://rodlocker.com/products/power-pro-braided-line.js",
            )
        )
        by_sku = {listing.retailer_sku: listing for listing in result.listings}
        variant = by_sku["21100400150Y"]
        assert variant.gtin_raw == ["712649106417"]
        offer = variant.offers[0]
        assert offer.price_cents == 925
        assert offer.claimed_reference_cents == 1850
        assert offer.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT
        assert offer.availability == Availability.OUT_OF_STOCK
        assert offer.seller_key == "rodlocker"
        assert offer.condition == Condition.NEW
        assert variant.url == "https://rodlocker.com/products/power-pro-braided-line"

    def test_available_variant_reports_in_stock(self, adapter: RodLockerAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://rodlocker.com/products/power-pro-braided-line.js",
            )
        )
        in_stock = next(l for l in result.listings if l.retailer_sku == "21100650150Y")
        assert in_stock.offers[0].availability == Availability.IN_STOCK

    def test_attributes_from_options(self, adapter: RodLockerAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url="https://rodlocker.com/products/power-pro-braided-line.js",
            )
        )
        listing = next(l for l in result.listings if l.retailer_sku == "21100400150Y")
        assert listing.attributes_raw.get("color") == "Hi-Vis Yellow"
        assert listing.attributes_raw.get("test") == "40 lb."


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: RodLockerAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://rodlocker.com/products/x.js")
        )
        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None

    def test_empty_collection_is_ok_with_no_discovered_items(
        self, adapter: RodLockerAdapter
    ) -> None:
        body = _load("empty_response.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.discovered == ()

    def test_404_is_not_found(self, adapter: RodLockerAdapter) -> None:
        result = adapter.parse(
            _response(b"", PageType.PRODUCT, url="https://rodlocker.com/products/x.js", status=404)
        )
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: RodLockerAdapter) -> None:
        garbage = b"\x00\x01\xff not json at all { broken"
        result = adapter.parse(
            _response(garbage, PageType.PRODUCT, url="https://rodlocker.com/products/x.js")
        )
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present_for_each_page_type(
        self, adapter: RodLockerAdapter
    ) -> None:
        product_sentinels = adapter.content_sentinels(PageType.PRODUCT)
        listing_sentinels = adapter.content_sentinels(PageType.CLEARANCE_LISTING)
        assert len(product_sentinels) > 0
        assert len(listing_sentinels) > 0
        assert product_sentinels != listing_sentinels
