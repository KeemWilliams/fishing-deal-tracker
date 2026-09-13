"""Unit tests for the Cabela's (cabelas.com) adapter, against a real
(fetched 2026-09-13, trimmed) Coveo search response plus one synthetic
on-sale row.

Live source the fixture was captured from:
- coveo_search_rods.json: a Cabela's Coveo `/rest/search/v2` product-search
  response for a fishing-rods listing (see
  tests/fixtures/_capture/README.md and fpt/adapters/cabelas.py's module
  docstring). Trimmed from the captured `tests/fixtures/_capture/
  cabelas_coveo_rods.json` (36 real results, left untouched on disk -- see
  HANDOFF) down to 2 real rows + ONE synthetic "on sale" row appended (sku
  "9999002"): every sampled real result had `offerprice == listprice`
  (`isproductdiscounted: 0`), matching the same finding basspro.py's test
  fixture already documented for this Coveo backend -- no real discounted
  row was available to trim down to. The synthetic row is a copy of the
  first real row with `offerprice`/`listprice` changed to 39.99/49.99 and
  `sku`/`producturlkeyword`/`title` changed to avoid colliding with the
  real row it was copied from -- every other field is the real captured
  shape. `searchUid`/`indexToken` (token-shaped fields) are redacted to
  "REDACTED_FOR_FIXTURE" per the commit secret gate.
- coveo_search_empty.json: the same real response with `results` emptied,
  for the empty-response case.
- blocked_page.html: a synthetic Akamai-style "Access Denied" block page
  (this adapter's `is_blocked` check is a byte-substring scan that applies
  equally to JSON and HTML bodies -- see fpt/adapters/_shared.py).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.cabelas import CabelasAdapter
from fpt.core.models import (
    ClaimedReferenceKind,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "cabelas"


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
    page_type: PageType = PageType.CATALOG_LISTING,
    url: str = "https://www.cabelas.com/c/fishing-rods",
    params: dict | None = None,
    status: int = 200,
) -> FetchResponse:
    request = FetchRequest(
        task_id=1,
        page_type=page_type,
        url=url,
        params=params or {},
        render_js=True,
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
def adapter() -> CabelasAdapter:
    return CabelasAdapter()


def _by_sku(discovered, sku: str):
    matches = [d for d in discovered if d.retailer_product_code == sku]
    assert len(matches) == 1, f"expected exactly one row for sku={sku!r}, got {len(matches)}"
    return matches[0]


def test_capabilities_match_adapter_matrix(adapter: CabelasAdapter) -> None:
    assert adapter.slug == "cabelas"
    assert adapter.capabilities.exposes_gtin is False
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.exposes_used_offers is False
    assert adapter.capabilities.requires_js is True
    assert PageType.CATALOG_LISTING in adapter.capabilities.page_types


def test_build_request_carries_task_fields(adapter: CabelasAdapter) -> None:
    task = _FakeTask(
        id=42,
        url="https://www.cabelas.com/c/fishing-rods",
        page_type=PageType.CATALOG_LISTING,
        params={"category_hint": "rods"},
    )
    request = adapter.build_request(task)
    assert request.task_id == 42
    assert request.url == task.url
    assert request.page_type == PageType.CATALOG_LISTING
    assert request.params == {"category_hint": "rods"}
    assert request.render_js is True


class TestNormalParse:
    """Real Coveo response, trimmed to 2 real rows + 1 synthetic on-sale row."""

    def test_parses_ok_with_discovered_rows(self, adapter: CabelasAdapter) -> None:
        body = _load("coveo_search_rods.json")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.OK
        assert result.block_signature is None
        assert result.listings == ()
        assert len(result.discovered) == 3

    def test_plain_item_not_on_sale_no_range(self, adapter: CabelasAdapter) -> None:
        body = _load("coveo_search_rods.json")
        result = adapter.parse(_response(body))

        item = _by_sku(result.discovered, "3234179")
        assert item.title_raw == "Bass Pro Shops Graphite Series Spinning Rod"
        assert item.price_cents == 4999
        assert item.price_is_range is False
        # offerprice == listprice -- never a fabricated discount.
        assert item.claimed_reference_cents is None
        assert item.claimed_reference_kind is None
        assert item.product_url == (
            "https://www.cabelas.com/p/bass-pro-shops-graphite-series-spinning-rod-101048321"
        )

    def test_synthetic_on_sale_item_has_claimed_reference(self, adapter: CabelasAdapter) -> None:
        body = _load("coveo_search_rods.json")
        result = adapter.parse(_response(body))

        item = _by_sku(result.discovered, "9999002")
        assert item.price_cents == 3999
        assert item.claimed_reference_cents == 4999
        assert item.claimed_reference_kind == ClaimedReferenceKind.LIST
        assert item.price_is_range is False

    def test_variant_price_range_item_sets_price_is_range(self, adapter: CabelasAdapter) -> None:
        body = _load("coveo_search_rods.json")
        result = adapter.parse(_response(body))

        # minofferprice=129.99, maxofferprice=159.99 -- variants disagree.
        item = _by_sku(result.discovered, "3810338")
        assert item.price_is_range is True
        # offerprice (== minofferprice here) is the representative price.
        assert item.price_cents == 13999

    def test_no_gtin_field_present_never_fabricated(self, adapter: CabelasAdapter) -> None:
        # DiscoveredItem carries no gtin field at all (confirmation-page
        # concern) -- this test documents that this fixture has no
        # barcode-shaped field anywhere, matching capabilities.exposes_gtin
        # is False for this adapter.
        body = _load("coveo_search_rods.json")
        import json

        payload = json.loads(body)
        for result in payload["results"]:
            raw = result["raw"]
            for key in raw:
                assert not key.lower().startswith(("gtin", "upc", "ean")), key
                assert key.lower() != "barcode"


class TestEmptyResponse:
    def test_empty_results_array_is_empty_outcome(self, adapter: CabelasAdapter) -> None:
        body = _load("coveo_search_empty.json")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.discovered == ()
        assert result.listings == ()
        assert result.block_signature is None


class TestBlockedResponse:
    def test_block_page_detected(self, adapter: CabelasAdapter) -> None:
        body = _load("blocked_page.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.discovered == ()
        assert result.listings == ()


class TestMalformedResponse:
    def test_garbage_body_is_structure_changed(self, adapter: CabelasAdapter) -> None:
        # No `"results":[` sentinel at all -- neither JSON-LD nor Coveo shape.
        body = b"<html><body>Not a Coveo response</body></html>"
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.discovered == ()

    def test_results_present_but_not_a_list_is_structure_changed(
        self, adapter: CabelasAdapter
    ) -> None:
        # Sentinel substring matches, but the JSON does not actually parse
        # into the expected {"results": [...]} shape.
        body = b'{"results":[}malformed'
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.discovered == ()

    def test_404_status_is_not_found(self, adapter: CabelasAdapter) -> None:
        body = _load("coveo_search_rods.json")
        result = adapter.parse(_response(body, status=404))

        assert result.outcome == ResponseOutcome.NOT_FOUND
