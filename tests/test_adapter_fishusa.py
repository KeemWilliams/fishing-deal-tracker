"""Unit tests for the FishUSA adapter, against real (fetched 2026-09-12,
trimmed to the relevant markup) and synthetic fixtures.

Live URLs the fixtures were derived from:
- product_rod_multivariant.html: https://www.fishusa.com/Daiwa-Spinmatic-D-Spinning-Rods/
  (trimmed to 3 of the page's 9 real variant-container "rod cards")
- clearance_grid_js_only.html: https://www.fishusa.com/Clearance/Clearance-Rods/
  (trimmed -- demonstrates the empty, JS-only grid container)
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.fishusa import FishUSAAdapter
from fpt.core.models import (
    Availability,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fishusa"


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
    url: str = "https://www.fishusa.com/Daiwa-Spinmatic-D-Spinning-Rods/",
    status: int = 200,
) -> FetchResponse:
    request = FetchRequest(task_id=1, page_type=page_type, url=url, params={})
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
def adapter() -> FishUSAAdapter:
    return FishUSAAdapter()


def test_capabilities_match_findings(adapter: FishUSAAdapter) -> None:
    assert adapter.slug == "fishusa"
    assert adapter.capabilities.exposes_gtin is False
    assert adapter.capabilities.exposes_stock_qty is True
    assert adapter.capabilities.exposes_claimed_reference is False
    assert adapter.capabilities.exposes_used_offers is False
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset({PageType.PRODUCT})


def test_build_request_carries_task_fields(adapter: FishUSAAdapter) -> None:
    task = _FakeTask(
        id=7,
        url="https://www.fishusa.com/Daiwa-Spinmatic-D-Spinning-Rods/",
        page_type=PageType.PRODUCT,
        params={},
    )
    request = adapter.build_request(task)
    assert request.task_id == 7
    assert request.url == task.url
    assert request.render_js is False


class TestMultiVariantRodProduct:
    """Daiwa Spinmatic D -- one rod card per length, own SKU/price/stock."""

    def test_parses_ok_with_one_listing_per_variant_row(
        self, adapter: FishUSAAdapter
    ) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.OK
        assert result.block_signature is None
        assert len(result.listings) == 3  # fixture trimmed to 3 of 9 real variants
        assert result.discovered == ()

    def test_out_of_stock_variant_fields(self, adapter: FishUSAAdapter) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        oos = next(l for l in result.listings if l.retailer_sku == "133465")
        assert oos.brand_raw == "Daiwa"
        assert oos.title_raw == "Daiwa Spinmatic D Spinning Rod"
        assert oos.variant_label_raw == "SMD401ULFS"
        assert oos.attributes_raw["Length"] == "4' 0\""
        assert oos.attributes_raw["Power"] == "Ultralight"
        assert oos.gtin_raw == []  # capabilities.exposes_gtin=False

        offer = oos.offers[0]
        assert offer.price_cents == 3999
        assert offer.availability == Availability.OUT_OF_STOCK
        assert offer.stock_qty == 0
        assert offer.condition == Condition.NEW
        assert offer.seller_key == "fishusa"
        assert offer.claimed_reference_cents is None

    def test_in_stock_variant_has_real_quantity(self, adapter: FishUSAAdapter) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        in_stock = next(l for l in result.listings if l.retailer_sku == "133471")
        offer = in_stock.offers[0]
        assert offer.availability == Availability.IN_STOCK
        assert offer.stock_qty == 11
        assert offer.price_cents == 4999
        assert in_stock.variant_label_raw == "SMD702ULFS"

    def test_each_variant_shares_parent_product_code(
        self, adapter: FishUSAAdapter
    ) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        codes = {listing.retailer_product_code for listing in result.listings}
        assert codes == {"3933"}

    def test_skus_are_unique_per_variant(self, adapter: FishUSAAdapter) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        skus = {listing.retailer_sku for listing in result.listings}
        assert skus == {"133465", "133471", "133473"}
        assert len(skus) == 3


class TestClearanceListingRequiresJS:
    def test_unsupported_page_type_reported_cleanly(
        self, adapter: FishUSAAdapter
    ) -> None:
        """CLEARANCE_LISTING is not in this adapter's capabilities.page_types
        (Searchspring/JS-only grid, confirmed empty via plain HTTP) -- if
        ever routed here anyway, parse() must not crash or fabricate data."""
        body = _load("clearance_grid_js_only.html")
        result = adapter.parse(_response(body, PageType.CLEARANCE_LISTING))

        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.listings == ()
        assert result.discovered == ()
        assert any("requires_js" in w for w in result.warnings)


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: FishUSAAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.listings == ()

    def test_empty_response_detected(self, adapter: FishUSAAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.listings == ()

    def test_404_is_not_found(self, adapter: FishUSAAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, PageType.PRODUCT, status=404))
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: FishUSAAdapter) -> None:
        garbage = b"\x00\x01\xff not html at all { broken json"
        result = adapter.parse(_response(garbage, PageType.PRODUCT))
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present_for_each_page_type(
        self, adapter: FishUSAAdapter
    ) -> None:
        product_sentinels = adapter.content_sentinels(PageType.PRODUCT)
        listing_sentinels = adapter.content_sentinels(PageType.CLEARANCE_LISTING)
        assert len(product_sentinels) > 0
        assert len(listing_sentinels) > 0
        assert product_sentinels != listing_sentinels
