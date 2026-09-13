"""Sportsman's Guide adapter tests, against fixtures trimmed from a live
page fetched 2026-09-13 (see fpt/adapters/sportsmans_guide.py module
docstring, including the badge-based clearance/on-sale filtering this
retailer requires).
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
from fpt.adapters.sportsmans_guide import SportsmansGuideAdapter
from fpt.fetch.blocks import detect_block_or_empty

FIXTURES = Path(__file__).parent / "fixtures" / "sportsmans_guide"


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
def adapter() -> SportsmansGuideAdapter:
    return SportsmansGuideAdapter()


def test_build_request_uses_task_fields(adapter: SportsmansGuideAdapter):
    class FakeTask:
        id = 9
        url = "https://www.sportsmansguide.com/productlist?collection=fishdeals"
        page_type = PageType.CATALOG_LISTING
        params: dict = {"category_hint": "other"}

    request = adapter.build_request(FakeTask())
    assert request.task_id == 9
    assert request.page_type == PageType.CATALOG_LISTING
    assert request.url == FakeTask.url


def test_capabilities_declare_catalog_listing_only(adapter: SportsmansGuideAdapter):
    assert adapter.capabilities.page_types == frozenset({PageType.CATALOG_LISTING})
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.exposes_gtin is False
    assert adapter.capabilities.requires_js is False


class TestBadgeFiltering:
    def test_only_clearance_and_on_sale_badges_are_discovered(self, adapter: SportsmansGuideAdapter):
        response = _response(
            _load("fishdeals.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals",
        )
        result = adapter.parse(response)

        assert result.outcome == ResponseOutcome.OK
        # Fixture has 4 tiles: On Sale, Members Only Deal, Members Only
        # Save 25%, Clearance -- only the first and last qualify.
        assert len(result.discovered) == 2
        codes = {item.retailer_product_code for item in result.discovered}
        assert codes == {"746141000000", "740903000LK0"}

    def test_members_only_deal_badge_is_skipped(self, adapter: SportsmansGuideAdapter):
        response = _response(
            _load("fishdeals.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals",
        )
        result = adapter.parse(response)
        assert "746729000000" not in {item.retailer_product_code for item in result.discovered}
        assert any("skipped_non_clearance_badge:Members Only Deal" in w for w in result.warnings)

    def test_members_only_save_percent_badge_is_skipped(self, adapter: SportsmansGuideAdapter):
        response = _response(
            _load("fishdeals.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals",
        )
        result = adapter.parse(response)
        assert "746736000000" not in {item.retailer_product_code for item in result.discovered}
        assert any(
            "skipped_non_clearance_badge:Members Only Save 25%" in w for w in result.warnings
        )


class TestPriceExtraction:
    def test_on_sale_item_current_and_was_price(self, adapter: SportsmansGuideAdapter):
        response = _response(
            _load("fishdeals.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals",
        )
        result = adapter.parse(response)
        by_code = {item.retailer_product_code: item for item in result.discovered}

        on_sale = by_code["746141000000"]
        assert on_sale.title_raw == (
            "Zebco Omega Pro Spincast Fishing Reel, Size 30 Reel, Left/Right Retrieve, Black"
        )
        assert on_sale.price_cents == 5999
        assert on_sale.price_is_range is False
        assert on_sale.claimed_reference_cents == 8999
        assert on_sale.claimed_reference_kind == ClaimedReferenceKind.WAS
        assert on_sale.product_url == (
            "https://www.sportsmansguide.com/product/index/"
            "zebco-omega-pro-spincast-fishing-reel-size-30-reel-left-right-retrieve-black"
            "?a=3031260"
        )

    def test_clearance_item_with_map_locked_club_price_still_uses_was_price(
        self, adapter: SportsmansGuideAdapter
    ):
        response = _response(
            _load("fishdeals.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals",
        )
        result = adapter.parse(response)
        by_code = {item.retailer_product_code: item for item in result.discovered}

        clearance = by_code["740903000LK0"]
        assert clearance.title_raw == "Daiwa Zillion Bass Spinning Rods"
        assert clearance.price_cents == 29997
        assert clearance.claimed_reference_cents == 39999
        assert clearance.claimed_reference_kind == ClaimedReferenceKind.WAS


class TestPagination:
    def test_next_page_url_follows_paging_item_next(self, adapter: SportsmansGuideAdapter):
        response = _response(
            _load("fishdeals.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals",
        )
        result = adapter.parse(response)
        assert result.next_page_url == (
            "https://www.sportsmansguide.com/productlist?collection=fishdeals&pg=2"
        )

    def test_last_page_has_no_next_page_url(self, adapter: SportsmansGuideAdapter):
        response = _response(
            _load("fishdeals_last_page.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals&pg=3",
        )
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.OK
        assert len(result.discovered) == 1
        assert result.next_page_url is None


class TestBlockAndEmptyDetection:
    def test_content_sentinels_present_on_real_page(self, adapter: SportsmansGuideAdapter):
        body = _load("fishdeals.html")
        sentinels = tuple(adapter.content_sentinels(PageType.CATALOG_LISTING))
        check = detect_block_or_empty(
            status=200, body=body, final_url="x", request_url="x", sentinels=sentinels
        )
        assert check.outcome == ResponseOutcome.OK

    def test_block_page_detected(self, adapter: SportsmansGuideAdapter):
        response = _response(_load("block_page.html"), "https://www.sportsmansguide.com/blocked")
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature in ("access denied", "captcha")

    def test_sentinel_present_but_zero_tiles_is_structure_changed(
        self, adapter: SportsmansGuideAdapter
    ):
        response = _response(
            _load("empty_no_items.html"),
            "https://www.sportsmansguide.com/productlist?collection=fishdeals&pg=99",
        )
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.discovered == ()

    def test_parse_never_raises_on_garbage_input(self, adapter: SportsmansGuideAdapter):
        response = _response(b"<html><body>nonsense</body></html>", "x")
        result = adapter.parse(response)
        assert result.outcome == ResponseOutcome.EMPTY
        assert result.discovered == ()

    def test_404_is_not_found(self, adapter: SportsmansGuideAdapter):
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
