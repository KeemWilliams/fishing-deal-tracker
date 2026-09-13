"""Unit tests for the TackleDirect adapter, against real (fetched
2026-09-12, trimmed to the relevant markup) and synthetic fixtures.

Live URLs the fixtures were derived from:
- product_single_sku.html:
  https://www.tackledirect.com/tsunami-tsshdii4000-shield-ii-spinning-reel.html
- product_multivariant_series_js_only.html:
  https://www.tackledirect.com/tsunami-evict-ii-spinning-reels.html
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.tackledirect import TackleDirectAdapter
from fpt.core.models import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tackledirect"


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
    url: str = "https://www.tackledirect.com/tsunami-tsshdii4000-shield-ii-spinning-reel.html",
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
def adapter() -> TackleDirectAdapter:
    return TackleDirectAdapter()


def test_capabilities_match_findings(adapter: TackleDirectAdapter) -> None:
    assert adapter.slug == "tackledirect"
    # Corrects the research doc's JSON-LD-only finding -- see module
    # docstring: plain HTML carries a real UPC via dd[data-product-upc].
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_stock_qty is False
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset({PageType.PRODUCT})


def test_build_request_carries_task_fields(adapter: TackleDirectAdapter) -> None:
    task = _FakeTask(
        id=3,
        url="https://www.tackledirect.com/tsunami-tsshdii4000-shield-ii-spinning-reel.html",
        page_type=PageType.PRODUCT,
        params={},
    )
    request = adapter.build_request(task)
    assert request.task_id == 3
    assert request.render_js is False


class TestSingleSkuProduct:
    def test_parses_ok_with_one_listing(self, adapter: TackleDirectAdapter) -> None:
        body = _load("product_single_sku.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.OK
        assert result.block_signature is None
        assert len(result.listings) == 1
        assert result.discovered == ()

    def test_listing_and_offer_fields_match_live_page(
        self, adapter: TackleDirectAdapter
    ) -> None:
        body = _load("product_single_sku.html")
        result = adapter.parse(_response(body))

        listing = result.listings[0]
        assert listing.retailer_sku == "TSU-0442"
        assert listing.title_raw == "Tsunami Shield II TSSHDII4000 Saltwater Spinning Reel"
        assert listing.source == "html_text"
        assert "00799967561364" in listing.gtin_raw  # 12-digit UPC, GS1-padded to 14

        offer = listing.offers[0]
        assert offer.price_cents == 15000
        assert offer.condition == Condition.NEW
        assert offer.seller_key == "tackledirect"
        assert offer.availability == Availability.IN_STOCK
        assert offer.claimed_reference_cents is None  # not on sale in this fixture
        assert offer.on_clearance is False


class TestMultiVariantSeriesRequiresJS:
    def test_price_not_in_static_html_is_reported_not_faked(
        self, adapter: TackleDirectAdapter
    ) -> None:
        body = _load("product_multivariant_series_js_only.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.listings == ()
        assert result.discovered == ()
        assert "multi_variant_series_requires_js" in result.warnings

    def test_never_emits_a_zero_price_offer(self, adapter: TackleDirectAdapter) -> None:
        """The raw page literally contains `$0.00` in a hidden price span --
        must never be surfaced as a real ParsedOffer price."""
        body = _load("product_multivariant_series_js_only.html")
        result = adapter.parse(_response(body))

        for listing in result.listings:
            for offer in listing.offers:
                assert offer.price_cents != 0


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: TackleDirectAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.listings == ()

    def test_empty_response_detected(self, adapter: TackleDirectAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.listings == ()

    def test_404_is_not_found(self, adapter: TackleDirectAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, status=404))
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: TackleDirectAdapter) -> None:
        garbage = b"\x00\x01\xff not html at all { broken json"
        result = adapter.parse(_response(garbage))
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present(self, adapter: TackleDirectAdapter) -> None:
        sentinels = adapter.content_sentinels(PageType.PRODUCT)
        assert len(sentinels) > 0
