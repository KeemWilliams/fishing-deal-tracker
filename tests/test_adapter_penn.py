"""Unit tests for the Penn (pennfishing.com) adapter, against real (fetched
2026-09-13, trimmed) fixtures.

Live endpoints the fixtures were captured from:
- collection_sale.json: https://www.pennfishing.com/collections/sale/products.json
- product_confirm.js: https://www.pennfishing.com/products/fierce-iv-spinning-rod-and-reel-combo-1573577.js
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.penn import PennAdapter
from fpt.core.models import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "penn"


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
        fetched_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
        egress_mode="DIRECT",
        snapshot_ref="snap://test",
    )


@pytest.fixture()
def adapter() -> PennAdapter:
    return PennAdapter()


def test_capabilities_match_adapter_matrix(adapter: PennAdapter) -> None:
    assert adapter.slug == "penn"
    assert adapter.base_url == "https://www.pennfishing.com"
    assert adapter.sale_collection_handle == "sale"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset(
        {PageType.PRODUCT, PageType.CLEARANCE_LISTING}
    )


def test_discovery_url(adapter: PennAdapter) -> None:
    assert adapter.discovery_url() == "https://www.pennfishing.com/collections/sale/products.json"


class TestCollectionDiscovery:
    def test_parses_ok_with_discovered_items(self, adapter: PennAdapter) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.listings == ()
        assert len(result.discovered) == 2

    def test_real_discount_gets_claimed_reference(self, adapter: PennAdapter) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        item = next(i for i in result.discovered if "Live Liner" in i.title_raw)
        assert item.price_cents == 13199
        assert item.claimed_reference_cents == 16499
        assert item.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT


class TestProductConfirm:
    def test_parses_ok_with_one_listing_per_variant(self, adapter: PennAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://www.pennfishing.com/products/fierce-iv-spinning-rod-and-reel-combo-1573577.js")
        )
        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 2

    def test_variant_price_barcode_and_claimed_reference(self, adapter: PennAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://www.pennfishing.com/products/fierce-iv-spinning-rod-and-reel-combo-1573577.js")
        )
        by_sku = {listing.retailer_sku: listing for listing in result.listings}
        variant = by_sku["1558666"]
        assert variant.gtin_raw == ["031324280205"]
        assert variant.brand_raw == "PENN"
        offer = variant.offers[0]
        assert offer.price_cents == 10799
        assert offer.claimed_reference_cents == 13499
        assert offer.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT
        assert offer.availability == Availability.IN_STOCK
        assert offer.seller_key == "penn"
        assert offer.condition == Condition.NEW
        assert variant.url == "https://www.pennfishing.com/products/fierce-iv-spinning-rod-and-reel-combo-1573577"


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: PennAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://www.pennfishing.com/products/x.js")
        )
        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None

    def test_empty_collection_is_ok_with_no_discovered_items(self, adapter: PennAdapter) -> None:
        body = _load("empty_response.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.discovered == ()

    def test_404_is_not_found(self, adapter: PennAdapter) -> None:
        result = adapter.parse(
            _response(b"", PageType.PRODUCT, url="https://www.pennfishing.com/products/x.js", status=404)
        )
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: PennAdapter) -> None:
        garbage = b"\x00\x01\xff not json at all { broken"
        result = adapter.parse(
            _response(garbage, PageType.PRODUCT, url="https://www.pennfishing.com/products/x.js")
        )
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )
