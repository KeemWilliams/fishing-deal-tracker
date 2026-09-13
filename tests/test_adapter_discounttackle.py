"""Unit tests for the Discount Tackle (discounttackle.com) adapter, against
real (fetched 2026-09-12, trimmed) fixtures.

Live endpoints the fixtures were captured from:
- collection_clearance.json: https://discounttackle.com/collections/clearance/products.json?limit=5
  (trimmed to 2 of the 5 returned products, each trimmed to 2 variants --
  kept the Berkley Trilene monofilament, whose 10 variants span two
  distinct prices, and the Yo-Zuri squid jig, whose variants share one
  price, to exercise both discovery paths).
- product_confirm.js: https://discounttackle.com/products/berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools.js
  (trimmed from 10 variants to 3; all fields including barcodes are the
  real captured values).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.discounttackle import DiscountTackleAdapter
from fpt.core.models import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "discounttackle"


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
def adapter() -> DiscountTackleAdapter:
    return DiscountTackleAdapter()


def test_capabilities_match_adapter_matrix(adapter: DiscountTackleAdapter) -> None:
    assert adapter.slug == "discounttackle"
    assert adapter.base_url == "https://discounttackle.com"
    assert adapter.sale_collection_handle == "clearance"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.requires_js is False
    assert adapter.capabilities.page_types == frozenset(
        {PageType.PRODUCT, PageType.CLEARANCE_LISTING}
    )


def test_discovery_url_uses_clearance_handle(adapter: DiscountTackleAdapter) -> None:
    assert adapter.discovery_url() == (
        "https://discounttackle.com/collections/clearance/products.json"
    )


def test_build_request_appends_js_for_product_page(adapter: DiscountTackleAdapter) -> None:
    task = _FakeTask(
        id=3,
        url="https://discounttackle.com/products/berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools",
        page_type=PageType.PRODUCT,
        params={},
    )
    request = adapter.build_request(task)
    assert request.url.endswith(
        "berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools.js"
    )


class TestCollectionDiscovery:
    def test_parses_ok_with_one_discovered_item_per_product(
        self, adapter: DiscountTackleAdapter
    ) -> None:
        body = _load("collection_clearance.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.listings == ()
        assert len(result.discovered) == 2

    def test_price_varies_product_is_range_and_picks_cheapest_available(
        self, adapter: DiscountTackleAdapter
    ) -> None:
        body = _load("collection_clearance.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        berkley = next(item for item in result.discovered if "Berkley" in item.title_raw)
        assert berkley.price_is_range is True
        assert berkley.price_cents == 927  # cheapest of the two variants (9.27)
        assert berkley.claimed_reference_cents == 1199
        assert berkley.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT
        assert berkley.product_url == (
            "https://discounttackle.com/products/"
            "berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools"
        )

    def test_uniform_price_product_is_not_a_range(
        self, adapter: DiscountTackleAdapter
    ) -> None:
        body = _load("collection_clearance.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        squid_jig = next(item for item in result.discovered if "Squid Jig" in item.title_raw)
        assert squid_jig.price_is_range is False
        assert squid_jig.price_cents == 639
        assert squid_jig.claimed_reference_cents == 799


class TestProductConfirm:
    def test_parses_ok_with_one_listing_per_variant(
        self, adapter: DiscountTackleAdapter
    ) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url=(
                    "https://discounttackle.com/products/"
                    "berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools.js"
                ),
            )
        )
        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 3

    def test_variant_price_barcode_and_claimed_reference(
        self, adapter: DiscountTackleAdapter
    ) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url=(
                    "https://discounttackle.com/products/"
                    "berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools.js"
                ),
            )
        )
        by_sku = {listing.retailer_sku: listing for listing in result.listings}
        cheap = by_sku["BGQS50C-15"]
        assert cheap.gtin_raw == ["028632028339"]
        offer = cheap.offers[0]
        assert offer.price_cents == 927
        assert offer.claimed_reference_cents == 1199
        assert offer.claimed_reference_kind == ClaimedReferenceKind.COMPARE_AT
        assert offer.on_clearance is True
        assert offer.availability == Availability.IN_STOCK
        assert offer.seller_key == "discounttackle"
        assert offer.condition == Condition.NEW
        assert cheap.url == (
            "https://discounttackle.com/products/"
            "berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools"
        )

    def test_brand_taken_from_vendor(self, adapter: DiscountTackleAdapter) -> None:
        body = _load("product_confirm.js")
        result = adapter.parse(
            _response(
                body,
                PageType.PRODUCT,
                url=(
                    "https://discounttackle.com/products/"
                    "berkley-trilene-big-game-monofilament-line-clear-quarter-pound-spools.js"
                ),
            )
        )
        assert all(l.brand_raw == "Berkley" for l in result.listings)


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: DiscountTackleAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(
            _response(body, PageType.PRODUCT, url="https://discounttackle.com/products/x.js")
        )
        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None

    def test_empty_collection_is_ok_with_no_discovered_items(
        self, adapter: DiscountTackleAdapter
    ) -> None:
        body = _load("empty_response.json")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, url=adapter.discovery_url())
        )
        assert result.outcome == ResponseOutcome.OK
        assert result.discovered == ()

    def test_404_is_not_found(self, adapter: DiscountTackleAdapter) -> None:
        result = adapter.parse(
            _response(
                b"", PageType.PRODUCT, url="https://discounttackle.com/products/x.js", status=404
            )
        )
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: DiscountTackleAdapter) -> None:
        garbage = b"\x00\x01\xff not json at all { broken"
        result = adapter.parse(
            _response(garbage, PageType.PRODUCT, url="https://discounttackle.com/products/x.js")
        )
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present_for_each_page_type(
        self, adapter: DiscountTackleAdapter
    ) -> None:
        product_sentinels = adapter.content_sentinels(PageType.PRODUCT)
        listing_sentinels = adapter.content_sentinels(PageType.CLEARANCE_LISTING)
        assert len(product_sentinels) > 0
        assert len(listing_sentinels) > 0
        assert product_sentinels != listing_sentinels
