"""Unit tests for the alltackle.com adapter, against real (fetched
2026-09-12, trimmed to the relevant markup) and synthetic fixtures.

Live URLs the fixtures were derived from:
- product_on_sale_with_upc.html:
  https://alltackle.com/yo-zuri-bonita-high-speed-trolling-lure-8-1-4-wahoo/
  (out of stock, on sale -- Was $43.99 -> Now $39.99, has a UPC on file)
- product_full_price_no_upc.html:
  https://alltackle.com/daiwa-2026-bg-lt-2500-sw-spinning-reel/
  (in stock, full price, no UPC on file for this SKU)
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.alltackle import AlltackleAdapter
from fpt.core.models import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "alltackle"


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
    page_type: PageType = PageType.PRODUCT,
    url: str = "https://alltackle.com/yo-zuri-bonita-high-speed-trolling-lure-8-1-4-wahoo/",
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
def adapter() -> AlltackleAdapter:
    return AlltackleAdapter()


def test_capabilities_match_findings(adapter: AlltackleAdapter) -> None:
    assert adapter.slug == "alltackle"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_stock_qty is False
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset({PageType.PRODUCT})


def test_build_request_carries_task_fields(adapter: AlltackleAdapter) -> None:
    task = _FakeTask(
        id=9,
        url="https://alltackle.com/daiwa-2026-bg-lt-2500-sw-spinning-reel/",
        page_type=PageType.PRODUCT,
        params={},
    )
    request = adapter.build_request(task)
    assert request.task_id == 9
    assert request.render_js is False


class TestOnSaleProductWithUpc:
    def test_parses_ok_with_one_listing(self, adapter: AlltackleAdapter) -> None:
        body = _load("product_on_sale_with_upc.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 1
        assert result.discovered == ()

    def test_upc_and_price_fields_match_live_page(self, adapter: AlltackleAdapter) -> None:
        body = _load("product_on_sale_with_upc.html")
        result = adapter.parse(_response(body))

        listing = result.listings[0]
        assert listing.retailer_sku == "R1158CWH"
        assert listing.title_raw == 'Yo-Zuri Bonita High Speed Trolling Lure 8-1/4" Wahoo'
        assert "00756791494343" in listing.gtin_raw  # 12-digit UPC, GS1-padded to 14

        offer = listing.offers[0]
        assert offer.price_cents == 3999
        assert offer.availability == Availability.OUT_OF_STOCK  # og:availability="oos"
        assert offer.seller_key == "alltackle"

    def test_was_now_pricing_captured_as_claimed_reference(
        self, adapter: AlltackleAdapter
    ) -> None:
        body = _load("product_on_sale_with_upc.html")
        result = adapter.parse(_response(body))

        offer = result.listings[0].offers[0]
        assert offer.claimed_reference_cents == 4399
        assert offer.claimed_reference_kind == ClaimedReferenceKind.WAS
        assert offer.on_clearance is True


class TestFullPriceProductNoUpc:
    def test_in_stock_no_upc_no_discount(self, adapter: AlltackleAdapter) -> None:
        body = _load("product_full_price_no_upc.html")
        result = adapter.parse(
            _response(
                body,
                url="https://alltackle.com/daiwa-2026-bg-lt-2500-sw-spinning-reel/",
            )
        )

        assert result.outcome == ResponseOutcome.OK
        listing = result.listings[0]
        assert listing.retailer_sku == "BGLT2500D-XH-DAI"
        assert listing.brand_raw == "Daiwa"
        assert listing.gtin_raw == []  # no UPC on file for this SKU

        offer = listing.offers[0]
        assert offer.price_cents == 17999
        assert offer.availability == Availability.IN_STOCK
        assert offer.claimed_reference_cents is None
        assert offer.on_clearance is False
        assert offer.condition == Condition.NEW


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: AlltackleAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.listings == ()

    def test_empty_response_detected(self, adapter: AlltackleAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.listings == ()

    def test_404_is_not_found(self, adapter: AlltackleAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, status=404))
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: AlltackleAdapter) -> None:
        garbage = b"\x00\x01\xff not html at all { broken json"
        result = adapter.parse(_response(garbage))
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present(self, adapter: AlltackleAdapter) -> None:
        sentinels = adapter.content_sentinels(PageType.PRODUCT)
        assert len(sentinels) > 0
