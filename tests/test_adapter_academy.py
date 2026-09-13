"""Unit tests for the Academy adapter, against real (fetched 2026-09-12,
trimmed to their JSON-LD payloads) and synthetic fixtures.

Live URLs the fixtures were captured from:
- clearance_listing.html: https://www.academy.com/c/academy-clearance/outdoors-clearance--1/fishing-clearance-items/rods-reels-clearance--1
- product_single_variant_instoreonly.html: https://www.academy.com/p/shimano-stradic-fl-spinning-reel
- product_group_multi_variant.html: https://www.academy.com/p/berkley-original-powerbait-7-power-worms-13-pack
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.academy import AcademyAdapter
from fpt.core.models import (
    Availability,
    Condition,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "academy"


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
    url: str = "https://www.academy.com/p/example",
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
def adapter() -> AcademyAdapter:
    return AcademyAdapter()


def test_capabilities_match_adapter_matrix(adapter: AcademyAdapter) -> None:
    assert adapter.slug == "academy"
    assert adapter.capabilities.exposes_gtin is True
    assert adapter.capabilities.exposes_claimed_reference is False
    assert adapter.capabilities.exposes_used_offers is False
    assert adapter.capabilities.requires_js is False
    assert PageType.PRODUCT in adapter.capabilities.page_types
    assert PageType.CLEARANCE_LISTING in adapter.capabilities.page_types


def test_build_request_carries_task_fields(adapter: AcademyAdapter) -> None:
    task = _FakeTask(
        id=42,
        url="https://www.academy.com/p/example",
        page_type=PageType.PRODUCT,
        params={"category_hint": "reel"},
    )
    request = adapter.build_request(task)
    assert request.task_id == 42
    assert request.url == task.url
    assert request.page_type == PageType.PRODUCT
    assert request.params == {"category_hint": "reel"}
    assert request.render_js is False


class TestSingleVariantProduct:
    """Shimano Stradic FL Spinning Reel -- one SKU, InStoreOnly."""

    def test_parses_ok_with_one_listing(self, adapter: AcademyAdapter) -> None:
        body = _load("product_single_variant_instoreonly.html")
        response = _response(body, PageType.PRODUCT)

        result = adapter.parse(response)

        assert result.outcome == ResponseOutcome.OK
        assert result.block_signature is None
        assert len(result.listings) == 1
        assert result.discovered == ()

    def test_listing_fields_match_live_page(self, adapter: AcademyAdapter) -> None:
        body = _load("product_single_variant_instoreonly.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        listing = result.listings[0]
        assert listing.retailer_sku == "121492321"
        assert listing.brand_raw == "Shimano"
        assert listing.title_raw == "Shimano Stradic FL Spinning Reel"
        assert listing.source == "jsonld"
        # gtin8 field name is ignored per architecture doc 4.2 -- collected
        # as a plain barcode candidate regardless of field name.
        assert "0022255225892" in listing.gtin_raw

    def test_offer_is_new_instore_only_no_claimed_reference(
        self, adapter: AcademyAdapter
    ) -> None:
        body = _load("product_single_variant_instoreonly.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        offer = result.listings[0].offers[0]
        assert offer.condition == Condition.NEW
        assert offer.price_cents == 18997
        assert offer.availability == Availability.STORE_ONLY
        assert offer.seller_key == "academy"
        assert offer.claimed_reference_cents is None
        assert offer.claimed_reference_kind is None
        assert offer.on_clearance is False
        assert offer.stock_qty is None  # Academy never exposes a numeric count


class TestProductGroupMultiVariant:
    """Berkley PowerBait 13-Pack -- 10 color variants, mixed availability."""

    def test_parses_ok_with_one_listing_per_variant(
        self, adapter: AcademyAdapter
    ) -> None:
        body = _load("product_group_multi_variant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.OK
        assert len(result.listings) == 9  # 9 color variants in the fixture

    def test_each_variant_is_its_own_retailer_sku(
        self, adapter: AcademyAdapter
    ) -> None:
        body = _load("product_group_multi_variant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        skus = {listing.retailer_sku for listing in result.listings}
        assert skus == {
            "924562", "924561", "51185634", "924550",
            "51179688", "924551", "924556", "924547", "924554",
        }
        assert len(skus) == 9  # no two variants collapsed onto one SKU

    def test_watermelon_red_variant_in_stock(self, adapter: AcademyAdapter) -> None:
        body = _load("product_group_multi_variant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        watermelon = next(
            l for l in result.listings if l.variant_label_raw == "Watermelon/Red"
        )
        assert watermelon.retailer_sku == "924562"
        assert watermelon.attributes_raw == {"color": "Watermelon/Red"}
        assert "00028632650721" in watermelon.gtin_raw
        offer = watermelon.offers[0]
        assert offer.price_cents == 599
        assert offer.availability == Availability.IN_STOCK

    def test_purple_variant_is_instoreonly_and_pricier(
        self, adapter: AcademyAdapter
    ) -> None:
        body = _load("product_group_multi_variant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        purple = next(l for l in result.listings if l.variant_label_raw == "Purple")
        offer = purple.offers[0]
        assert offer.price_cents == 699
        assert offer.availability == Availability.STORE_ONLY

    def test_pack_count_parsed_from_product_name(
        self, adapter: AcademyAdapter
    ) -> None:
        body = _load("product_group_multi_variant.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        for listing in result.listings:
            assert listing.offers[0].unit_count == 13


class TestClearanceListing:
    """Rods + Reels Clearance grid -- ItemList of product-level offers."""

    def test_parses_ok_with_discovered_items(self, adapter: AcademyAdapter) -> None:
        body = _load("clearance_listing.html")
        result = adapter.parse(
            _response(
                body,
                PageType.CLEARANCE_LISTING,
                url="https://www.academy.com/c/academy-clearance/outdoors-clearance--1/fishing-clearance-items/rods-reels-clearance--1",
                params={"category_hint": "rod"},
            )
        )

        assert result.outcome == ResponseOutcome.OK
        assert result.listings == ()
        assert len(result.discovered) == 5  # positions 1-4 and 6 in the fixture

    def test_discovered_items_are_never_observations(
        self, adapter: AcademyAdapter
    ) -> None:
        """Grid rows must never become ParsedListing/ParsedOffer -- only
        DiscoveredItem, per the variant-mismatch guard (architecture 3.1)."""
        body = _load("clearance_listing.html")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, params={"category_hint": "rod"})
        )

        assert result.listings == ()
        for item in result.discovered:
            assert item.price_is_range is True
            assert item.claimed_reference_cents is None
            assert item.category_hint == "rod"

    def test_discovered_item_fields(self, adapter: AcademyAdapter) -> None:
        body = _load("clearance_listing.html")
        result = adapter.parse(
            _response(body, PageType.CLEARANCE_LISTING, params={"category_hint": "rod"})
        )

        daiwa = next(
            item for item in result.discovered if "Daiwa Laguna" in item.title_raw
        )
        assert daiwa.product_url == (
            "https://www.academy.com/p/daiwa-laguna-freshwater-saltwater-casting-rod"
        )
        assert daiwa.retailer_product_code == "201369450"
        assert daiwa.price_cents == 899

    def test_default_category_hint_when_missing(self, adapter: AcademyAdapter) -> None:
        body = _load("clearance_listing.html")
        result = adapter.parse(_response(body, PageType.CLEARANCE_LISTING, params={}))
        assert result.outcome == ResponseOutcome.OK
        assert all(item.category_hint == "other" for item in result.discovered)


class TestBlockAndEmptyDetection:
    def test_block_signature_detected(self, adapter: AcademyAdapter) -> None:
        body = _load("block_page.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.listings == ()
        assert result.discovered == ()

    def test_empty_response_detected(self, adapter: AcademyAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, PageType.PRODUCT))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.listings == ()

    def test_404_is_not_found(self, adapter: AcademyAdapter) -> None:
        body = _load("empty_response.html")
        result = adapter.parse(_response(body, PageType.PRODUCT, status=404))
        assert result.outcome == ResponseOutcome.NOT_FOUND

    def test_parse_never_raises_on_garbage_body(self, adapter: AcademyAdapter) -> None:
        garbage = b"\x00\x01\xff not html at all { broken json"
        result = adapter.parse(_response(garbage, PageType.PRODUCT))
        # Must return a ParseResult, never raise.
        assert result.outcome in (
            ResponseOutcome.EMPTY,
            ResponseOutcome.STRUCTURE_CHANGED,
            ResponseOutcome.BLOCKED,
        )

    def test_content_sentinels_present_for_each_page_type(
        self, adapter: AcademyAdapter
    ) -> None:
        product_sentinels = adapter.content_sentinels(PageType.PRODUCT)
        clearance_sentinels = adapter.content_sentinels(PageType.CLEARANCE_LISTING)
        assert len(product_sentinels) > 0
        assert len(clearance_sentinels) > 0
        assert product_sentinels != clearance_sentinels
