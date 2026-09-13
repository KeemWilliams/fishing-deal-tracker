"""Unit tests for the Berkley (berkley-fishing.com) adapter, against real (fetched
2026-09-13, trimmed) fixtures.

Live endpoints the fixtures were captured from:
- collection_sale.json: https://www.berkley-fishing.com/collections/sale/products.json
- product_confirm.js: https://www.berkley-fishing.com/products/hit-stick.js
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.berkley import BerkleyAdapter
from fpt.core.models import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "berkley"


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
def adapter() -> BerkleyAdapter:
    return BerkleyAdapter()


def test_capabilities_match_adapter_matrix(adapter: BerkleyAdapter) -> None:
    assert adapter.slug == "berkley"
    assert adapter.base_url == "https://www.berkley-fishing.com"
    assert adapter.sale_collection_handle == "sale"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset(
        {PageType.PRODUCT, PageType.CLEARANCE_LISTING}
    )


def test_discovery_url(adapter: BerkleyAdapter) -> None:
    assert adapter.discovery_url() == "https://www.berkley-fishing.com/collections/sale/products.json"


class TestCollectionDiscovery:
    def test_parses_ok_with_discovered_items(self, adapter: BerkleyAdapter) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.listings == ()
        assert len(result.discovered) == 3

    def test_real_discount_gets_claimed_reference(self, adapter: BerkleyAdapter) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        item = next(i for i in result.discovered if "Hit Stick" in i.title_raw)
        assert item.price_cents == 399
        assert item.claimed_reference_cents == 799
        assert item.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT

    def test_no_real_discount_is_never_a_claimed_reference(self, adapter: BerkleyAdapter) -> None:
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        item = next(i for i in result.discovered if "Choppo" in i.title_raw)
        assert item.claimed_reference_cents is None
        assert item.claimed_reference_kind is None

    def test_edlp_compare_at_equal_to_price_is_never_a_claimed_reference(
        self, adapter: BerkleyAdapter
    ) -> None:
        # Synthesized fixture row (see tests/fixtures/berkley/collection_sale.json and
        # fpt/adapters/berkley.py's module docstring): no live 2026-09-13 sample showed
        # compare_at_price == price, but the scout report flagged this brand's low-price
        # tackle SKUs as sometimes carrying an unchanged everyday-low-price rather than a
        # real markdown -- this proves the shared adapter's guard handles that case too.
        body = _load("collection_sale.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        edlp = next(i for i in result.discovered if "EDLP" in i.title_raw)
        assert edlp.claimed_reference_cents is None
        assert edlp.claimed_reference_kind is None


class TestProductConfirm:
    def test_parses_ok_with_one_listing_per_variant(self, adapter: BerkleyAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://www.berkley-fishing.com/products/hit-stick.js")
        )
        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 2

    def test_variant_price_barcode_and_claimed_reference(self, adapter: BerkleyAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://www.berkley-fishing.com/products/hit-stick.js")
        )
        by_sku = {listing.retailer_sku: listing for listing in result.listings}
        variant = by_sku["1522601"]
        assert variant.gtin_raw == ["28632929438"]
        assert variant.brand_raw == "Berkley"
        offer = variant.offers[0]
        assert offer.price_cents == 399
        assert offer.claimed_reference_cents == 799
        assert offer.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT
        assert offer.availability == Availability.OUT_OF_STOCK
        assert offer.seller_key == "berkley"
        assert offer.condition == Condition.NEW
        assert variant.url == "https://www.berkley-fishing.com/products/hit-stick"


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: BerkleyAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://www.berkley-fishing.com/products/x.js")
        )
        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None

    def test_empty_collection_is_ok_with_no_discovered_items(self, adapter: BerkleyAdapter) -> None:
        body = _load("empty_response.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.discovered == ()

    def test_404_is_not_found(self, adapter: BerkleyAdapter) -> None:
        result = adapter.parse(
            _response(b"", PageType.PRODUCT, url="https://www.berkley-fishing.com/products/x.js", status=404)
        )
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: BerkleyAdapter) -> None:
        garbage = b"\x00\x01\xff not json at all { broken"
        result = adapter.parse(
            _response(garbage, PageType.PRODUCT, url="https://www.berkley-fishing.com/products/x.js")
        )
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )
