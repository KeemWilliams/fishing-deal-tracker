"""Unit tests for the J&H Tackle adapter, against real (fetched 2026-09-12,
trimmed) and synthetic fixtures.

Live URLs the fixtures were captured from:
- catalog_listing.html: https://jandh.com/collections/rods
- product_rod_multivariant.html: https://jandh.com/products/dark-matter-john-skinner-jig-and-bounce-spinning-rods
- product_terminal_pack_pricing.html: https://jandh.com/products/gamakatsu-octopus-hooks
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.jandh import JandhAdapter
from fpt.core.models import (
    Availability,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "jandh"


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
    url: str = "https://jandh.com/products/example",
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
def adapter() -> JandhAdapter:
    return JandhAdapter()


def test_capabilities_match_adapter_matrix(adapter: JandhAdapter) -> None:
    assert adapter.slug == "jandh"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_claimed_reference is False
    assert adapter.capabilities.exposes_used_offers is False
    assert adapter.capabilities.requires_js is False
    assert PageType.PRODUCT in adapter.capabilities.page_types
    assert PageType.CATALOG_LISTING in adapter.capabilities.page_types


def test_build_request_carries_task_fields(adapter: JandhAdapter) -> None:
    task = _FakeTask(
        id=7,
        url="https://jandh.com/products/example",
        page_type=PageType.PRODUCT,
        params={"category_hint": "other"},
    )
    request = adapter.build_request(task)
    assert request.task_id == 7
    assert request.url == task.url
    assert request.params == {"category_hint": "other"}
    assert request.render_js is False


class TestRodMultiVariantProduct:
    """Dark Matter John Skinner Jig and Bounce Spinning Rods -- 4 variants,
    2 rod models x 2 colors, all sharing one AggregateOffer price."""

    def test_parses_ok_with_one_listing_per_variant(self, adapter: JandhAdapter) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 4

    def test_variant_skus_and_barcodes_from_product_json(
        self, adapter: JandhAdapter
    ) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        by_sku = {listing.retailer_sku: listing for listing in result.listings}
        assert set(by_sku) == {
            "DMSJB62MLS", "DMSJB62MLS-AG", "DMSJB63MS", "DMSJB63MS-AG",
        }
        # Full UPC-A barcode, straight from ProductJson.variants[].barcode --
        # this is the cross-retailer matching anchor (architecture section 8).
        assert by_sku["DMSJB62MLS"].gtin_raw == ["810057871436"]
        assert by_sku["DMSJB63MS-AG"].gtin_raw == ["810057873805"]

    def test_variant_price_and_availability(self, adapter: JandhAdapter) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        listing = next(
            l for l in result.listings if l.retailer_sku == "DMSJB62MLS"
        )
        offer = listing.offers[0]
        assert offer.price_cents == 21999
        assert offer.condition == Condition.NEW
        assert offer.availability == Availability.IN_STOCK
        assert offer.seller_key == "jandh"
        # J&H is a reference anchor only -- never a claimed-reference source.
        assert offer.claimed_reference_cents is None
        assert offer.claimed_reference_kind is None

    def test_brand_taken_from_product_json_vendor(self, adapter: JandhAdapter) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))
        assert all(l.brand_raw == "Dark Matter" for l in result.listings)

    def test_variant_attributes_from_options(self, adapter: JandhAdapter) -> None:
        body = _load("product_rod_multivariant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        listing = next(
            l for l in result.listings if l.retailer_sku == "DMSJB62MLS-AG"
        )
        assert listing.variant_label_raw == '6\'2" ML Moderate / Avocado Green'
        # option names come from ProductJson.options (this product's option2
        # is literally named "skinner rod color"); values from option1/2.
        assert listing.attributes_raw.get("skinner rod color") == "Avocado Green"


class TestTerminalPackPricing:
    """Gamakatsu Octopus Hooks -- 19 pack-size variants, per-piece / FINAL
    PRICE display on the page (not used for price -- see adapter docstring),
    ProductJson.variants[].price is authoritative."""

    def test_parses_ok_with_one_listing_per_pack_size(
        self, adapter: JandhAdapter
    ) -> None:
        body = _load("product_terminal_pack_pricing.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 19

    def test_five_pack_variant_price_and_unit_count(
        self, adapter: JandhAdapter
    ) -> None:
        body = _load("product_terminal_pack_pricing.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        five_pack = next(
            l for l in result.listings if l.retailer_sku == "02420"
        )
        assert five_pack.variant_label_raw == "02420 - 10/0 - 5 Pack"
        offer = five_pack.offers[0]
        assert offer.price_cents == 599
        assert offer.unit_count == 5
        assert "089726001751" in five_pack.gtin_raw

    def test_twenty_five_pack_variant_has_higher_price_and_unit_count(
        self, adapter: JandhAdapter
    ) -> None:
        body = _load("product_terminal_pack_pricing.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        big_pack = next(
            l for l in result.listings if l.retailer_sku == "02417-25"
        )
        offer = big_pack.offers[0]
        assert offer.price_cents == 2299
        assert offer.unit_count == 25

    def test_unavailable_variant_reports_out_of_stock(
        self, adapter: JandhAdapter
    ) -> None:
        body = _load("product_terminal_pack_pricing.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        oos = next(l for l in result.listings if l.retailer_sku == "02415-25")
        assert oos.offers[0].availability == Availability.OUT_OF_STOCK

    def test_per_piece_and_final_price_dom_fields_not_used_for_price(
        self, adapter: JandhAdapter
    ) -> None:
        """The page's '.jh-main-price /piece' and '.jh-final-price' DOM
        elements both echo the SAME number as the selected variant's
        ProductJson price (confirmed on the live fixture: both show
        $5.99, matching the first variant's 599-cent price) -- they are
        not a separate per-unit-of-the-pack price. The adapter must not
        derive price from those fields at all."""
        body = _load("product_terminal_pack_pricing.html")
        assert b"jh-final-price" in body  # sanity: field really is on page
        result = adapter.parse(_response(body, PageType.PRODUCT))

        first_variant = next(
            l for l in result.listings if l.retailer_sku == "02420"
        )
        assert first_variant.offers[0].price_cents == 599


class TestCatalogListing:
    """collections/rods grid -- product-card-wrapper blocks, some with a
    'From' marker (multi-variant products priced at their cheapest variant)."""

    def test_parses_ok_with_discovered_items(self, adapter: JandhAdapter) -> None:
        body = _load("catalog_listing.html")
        result = adapter.parse(
            _response(
                body,
                PageType.CATALOG_LISTING,
                url="https://jandh.com/collections/rods",
                params={"category_hint": "rod"},
            )
        )

        assert result.outcome == ResponseOutcome.OK
        assert result.listings == ()
        assert len(result.discovered) == 6  # 6 cards kept in the trimmed fixture

    def test_discovered_items_are_never_observations(
        self, adapter: JandhAdapter
    ) -> None:
        body = _load("catalog_listing.html")
        result = adapter.parse(
            _response(body, PageType.CATALOG_LISTING, params={"category_hint": "rod"})
        )

        assert result.listings == ()
        for item in result.discovered:
            assert item.claimed_reference_cents is None  # never a claimed ref for jandh

    def test_from_tag_marks_price_as_range(self, adapter: JandhAdapter) -> None:
        body = _load("catalog_listing.html")
        result = adapter.parse(
            _response(body, PageType.CATALOG_LISTING, params={"category_hint": "rod"})
        )

        dm_rod = next(
            item
            for item in result.discovered
            if "John Skinner Jig and Bounce Spinning" in item.title_raw
        )
        assert dm_rod.price_is_range is True
        assert dm_rod.price_cents == 21999
        # H1: config/retailers.yaml's `allowed_hosts` for jandh is
        # `www.jandh.com` only -- a bare `jandh.com` relative href is
        # rewritten with `www.` to stay on the allowed host (see
        # fpt/adapters/jandh.py's `_parse_catalog_listing`).
        assert dm_rod.product_url == (
            "https://www.jandh.com/products/dark-matter-john-skinner-jig-and-bounce-spinning-rods"
        )


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: JandhAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.listings == ()

    def test_empty_response_detected(self, adapter: JandhAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.listings == ()

    def test_404_is_not_found(self, adapter: JandhAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, PageType.PRODUCT, status=404))
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: JandhAdapter) -> None:
        garbage = b"\x00\x01\xff not html at all { broken json"
        result = adapter.parse(_response(garbage, PageType.PRODUCT))
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present_for_each_page_type(
        self, adapter: JandhAdapter
    ) -> None:
        product_sentinels = adapter.content_sentinels(PageType.PRODUCT)
        catalog_sentinels = adapter.content_sentinels(PageType.CATALOG_LISTING)
        assert len(product_sentinels) > 0
        assert len(catalog_sentinels) > 0
        assert product_sentinels != catalog_sentinels
