"""Sportsman's Warehouse adapter tests, against fixtures trimmed from a
live page fetched 2026-09-13 (see fpt/adapters/sportsmans_warehouse.py
module docstring).
"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.base import (
    ClaimedReferenceKind,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)
from fpt.adapters.sportsmans_warehouse import SportsmansWarehouseAdapter
from fpt.fetch.blocks import detect_block_or_empty

FIXTURES = Path(__file__).parent / "fixtures" / "sportsmans_warehouse"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _response(body: bytes, url: str, params: dict | None = None) -> FetchResponse:
    request = FetchRequest(
        task_id=1,
        page_type=PageType.CATALOG_LISTING,
        url=url,
        params=params or {},
    )
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
def adapter() -> SportsmansWarehouseAdapter:
    return SportsmansWarehouseAdapter()


def test_build_request_uses_task_fields(adapter: SportsmansWarehouseAdapter):
    class FakeTask:
        id = 7
        url = "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209"
        page_type = PageType.CATALOG_LISTING
        params: dict = {"category_hint": "other"}

    request = adapter.build_request(FakeTask())
    assert request.task_id == 7
    assert request.page_type == PageType.CATALOG_LISTING
    assert request.url == FakeTask.url
    assert request.params == {"category_hint": "other"}


def test_capabilities_declare_catalog_listing_only(adapter: SportsmansWarehouseAdapter):
    assert adapter.capabilities.page_types == frozenset({PageType.CATALOG_LISTING})
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.exposes_gtin is False
    assert adapter.capabilities.requires_js is False


class TestClearanceListing:
    def test_discovers_range_priced_item_with_was_range(self, adapter: SportsmansWarehouseAdapter):
        response = _response(
            _load("clearance_fishing.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209",
        )
        result = adapter.parse(response)

        assert result.outcome == ResponseOutcome.OK
        assert len(result.discovered) == 3

        first = result.discovered[0]
        assert first.retailer_product_code == "p38069"
        assert first.title_raw == "Abu Garcia Vengeance Series Casting Rod - Past Season Models"
        assert first.product_url == (
            "https://www.sportsmans.com/fishing-gear-supplies/casting-rods/"
            "abu-garcia-vengeance-series-casting-rod/p/p38069"
        )
        # MIN of the "$48.92 - $59.99" range.
        assert first.price_cents == 4892
        assert first.price_is_range is True
        assert first.claimed_reference_cents == 5999  # MIN of "$59.99 - $69.99"
        assert first.claimed_reference_kind == ClaimedReferenceKind.WAS

    def test_discovers_single_priced_item_with_sale_price_sr_only_label(
        self, adapter: SportsmansWarehouseAdapter
    ):
        response = _response(
            _load("clearance_fishing.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209",
        )
        result = adapter.parse(response)

        second = result.discovered[1]
        assert second.retailer_product_code == "p255459"
        assert second.price_cents == 6397
        assert second.price_is_range is False
        assert second.claimed_reference_cents == 6999
        assert second.claimed_reference_kind == ClaimedReferenceKind.WAS

    def test_range_item_with_no_was_price_has_none_claimed_reference(
        self, adapter: SportsmansWarehouseAdapter
    ):
        response = _response(
            _load("clearance_fishing.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209",
        )
        result = adapter.parse(response)

        third = result.discovered[2]
        assert third.retailer_product_code == "p273093"
        assert third.price_cents == 20477
        assert third.price_is_range is True
        assert third.claimed_reference_cents is None
        assert third.claimed_reference_kind is None

    def test_next_page_url_follows_rel_next_anchor(self, adapter: SportsmansWarehouseAdapter):
        response = _response(
            _load("clearance_fishing.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209",
        )
        result = adapter.parse(response)
        assert result.next_page_url == (
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209?page=1"
        )

    def test_last_page_has_no_next_page_url(self, adapter: SportsmansWarehouseAdapter):
        response = _response(
            _load("clearance_last_page.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209?page=64",
        )
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.OK
        assert len(result.discovered) == 1
        assert result.next_page_url is None

    def test_category_hint_passed_through_from_params(self, adapter: SportsmansWarehouseAdapter):
        response = _response(
            _load("clearance_fishing.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209",
            params={"category_hint": "rod"},
        )
        result = adapter.parse(response)
        assert all(item.category_hint == "rod" for item in result.discovered)

    def test_defaults_category_hint_to_other(self, adapter: SportsmansWarehouseAdapter):
        response = _response(
            _load("clearance_fishing.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209",
        )
        result = adapter.parse(response)
        assert all(item.category_hint == "other" for item in result.discovered)


class TestUrlSanitizing:
    def test_product_urls_are_all_https_on_allowed_host(self, adapter: SportsmansWarehouseAdapter):
        response = _response(
            _load("clearance_fishing.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209",
        )
        result = adapter.parse(response)
        for item in result.discovered:
            assert item.product_url.startswith("https://www.sportsmans.com/")


class TestBlockAndEmptyDetection:
    def test_content_sentinels_present_on_real_page(self, adapter: SportsmansWarehouseAdapter):
        body = _load("clearance_fishing.html")
        sentinels = tuple(adapter.content_sentinels(PageType.CATALOG_LISTING))
        check = detect_block_or_empty(
            status=200, body=body, final_url="x", request_url="x", sentinels=sentinels
        )
        assert check.outcome == ResponseOutcome.OK

    def test_block_page_detected(self, adapter: SportsmansWarehouseAdapter):
        response = _response(_load("block_page.html"), "https://www.sportsmans.com/blocked")
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature in ("access denied", "captcha")

    def test_sentinel_present_but_zero_items_is_structure_changed(
        self, adapter: SportsmansWarehouseAdapter
    ):
        response = _response(
            _load("empty_no_items.html"),
            "https://www.sportsmans.com/deals-clearance/fishing-clearance/c/cat101209?page=99",
        )
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.discovered == ()

    def test_parse_never_raises_on_garbage_input(self, adapter: SportsmansWarehouseAdapter):
        response = _response(b"<html><body>nonsense</body></html>", "x")
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.EMPTY
        assert result.discovered == ()

    def test_404_is_not_found(self, adapter: SportsmansWarehouseAdapter):
        response = _response(b"<html><body>gone</body></html>", "x")
        response = FetchResponse(
            request=response.request,
            status=404,
            final_url="x",
            headers={},
            body=response.body,
            elapsed_ms=1,
            fetched_at=response.fetched_at,
            egress_mode="DIRECT",
            snapshot_ref="",
        )
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.NOT_FOUND
