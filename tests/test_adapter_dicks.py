"""Unit tests for the Dick's Sporting Goods (dickssportinggoods.com)
adapter, against a real (fetched 2026-09-13, trimmed) `v2/search`
prod-catalog-product-api response.

Live source the fixture was captured from:
- search_sale.json: a Dick's `/f/sale` search response (see
  tests/fixtures/_capture/README.md and fpt/adapters/dicks.py's module
  docstring). Trimmed from the captured
  `tests/fixtures/_capture/dicks_search_sale.json` (51 real products, left
  untouched on disk -- see HANDOFF) down to 3 real rows:
  - partnumber 26802117 (HOKA Women's Arahi 8 Running Shoes): a genuine
    on-sale row, offer $119.99 < list $149.99, `priceIndicator: 0`.
  - partnumber 27485785 (Nike Vapor Pro 1 Football Cleats): a MAP-restricted
    "See Price in Cart" decoy row, `priceIndicator: 1` -- offer == list ==
    map price ($144.99), must be SKIPPED entirely (no DiscoveredItem
    emitted).
  - partnumber 26623335 (Nike Men's Club Fleece Hoodie): a plain
    not-on-sale row, offer == list ($70.00), `priceIndicator: 0`.
  All three are real captured field values, unmodified except for the
  file being trimmed to these three array entries.
- search_empty.json: the same shape with `productVOs` emptied.
- blocked_page.html: a synthetic Akamai-style "Access Denied" block page
  (this adapter's `is_blocked` check is a byte-substring scan that applies
  equally to JSON and HTML bodies -- see fpt/adapters/_shared.py).
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fpt.adapters.dicks import DicksAdapter
from fpt.core.models import (
    ClaimedReferenceKind,
    FetchRequest,
    FetchResponse,
    PageType,
    ResponseOutcome,
)

FIXTURES = Path(__file__).parent / "fixtures" / "dicks"


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
    url: str = "https://www.dickssportinggoods.com/f/sale",
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
def adapter() -> DicksAdapter:
    return DicksAdapter()


def _by_code(discovered, code: str):
    matches = [d for d in discovered if d.retailer_product_code == code]
    assert len(matches) == 1, f"expected exactly one row for code={code!r}, got {len(matches)}"
    return matches[0]


def test_capabilities_match_adapter_matrix(adapter: DicksAdapter) -> None:
    assert adapter.slug == "dicks"
    assert adapter.capabilities.exposes_gtin is False
    assert adapter.capabilities.exposes_claimed_reference is True
    assert adapter.capabilities.exposes_used_offers is False
    assert adapter.capabilities.requires_js is True
    assert PageType.CATALOG_LISTING in adapter.capabilities.page_types


def test_build_request_carries_task_fields(adapter: DicksAdapter) -> None:
    task = _FakeTask(
        id=42,
        url="https://www.dickssportinggoods.com/f/sale",
        page_type=PageType.CATALOG_LISTING,
        params={"category_hint": "other"},
    )
    request = adapter.build_request(task)
    assert request.task_id == 42
    assert request.url == task.url
    assert request.page_type == PageType.CATALOG_LISTING
    assert request.params == {"category_hint": "other"}
    assert request.render_js is True


class TestNormalParse:
    """Real v2/search response, trimmed to 3 real rows: on-sale, MAP-restricted
    (skipped), and plain not-on-sale."""

    def test_parses_ok_with_only_non_restricted_rows(self, adapter: DicksAdapter) -> None:
        body = _load("search_sale.json")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.OK
        assert result.block_signature is None
        assert result.listings == ()
        # 3 rows in the fixture, 1 is MAP-restricted and must be skipped.
        assert len(result.discovered) == 2

    def test_on_sale_item_has_claimed_reference(self, adapter: DicksAdapter) -> None:
        body = _load("search_sale.json")
        result = adapter.parse(_response(body))

        item = _by_code(result.discovered, "25FHQWRH8WHTWHTXXFTW")
        assert item.title_raw == "HOKA Women's Arahi 8 Running Shoes"
        assert item.price_cents == 11999
        assert item.claimed_reference_cents == 14999
        assert item.claimed_reference_kind == ClaimedReferenceKind.LIST
        assert item.price_is_range is False
        assert item.product_url == (
            "https://www.dickssportinggoods.com/p/"
            "hoka-womens-arahi-8-running-shoes-25fhqwrh8whtwhtxxftw/25fhqwrh8whtwhtxxftw"
        )

    def test_map_restricted_price_in_cart_item_is_skipped(self, adapter: DicksAdapter) -> None:
        body = _load("search_sale.json")
        result = adapter.parse(_response(body))

        codes = [d.retailer_product_code for d in result.discovered]
        assert "25NIKAZMRVPRPR1GRCLT" not in codes

    def test_plain_item_not_on_sale(self, adapter: DicksAdapter) -> None:
        body = _load("search_sale.json")
        result = adapter.parse(_response(body))

        item = _by_code(result.discovered, "25NIKMMNKCLBBBPHDNFT")
        assert item.price_cents == 7000
        # offerprice == listprice -- never a fabricated discount.
        assert item.claimed_reference_cents is None
        assert item.claimed_reference_kind is None

    def test_no_gtin_field_present_never_fabricated(self, adapter: DicksAdapter) -> None:
        body = _load("search_sale.json")
        import json

        payload = json.loads(body)
        for product in payload["productVOs"]:
            for key in product:
                assert not key.lower().startswith(("gtin", "upc", "ean")), key
                assert key.lower() != "barcode"


class TestEmptyResponse:
    def test_empty_product_vos_array_is_empty_outcome(self, adapter: DicksAdapter) -> None:
        body = _load("search_empty.json")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.discovered == ()
        assert result.listings == ()
        assert result.block_signature is None

    def test_all_rows_restricted_is_empty_outcome(self, adapter: DicksAdapter) -> None:
        # A response whose only row is MAP-restricted has nothing usable to
        # enroll -- must be classified EMPTY, not OK with zero rows.
        import json

        payload = json.loads(_load("search_sale.json"))
        restricted_only = dict(payload)
        restricted_only["productVOs"] = [
            p for p in payload["productVOs"] if p["partnumber"] == "27485785"
        ]
        body = json.dumps(restricted_only).encode("utf-8")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.EMPTY
        assert result.discovered == ()


class TestBlockedResponse:
    def test_block_page_detected(self, adapter: DicksAdapter) -> None:
        body = _load("blocked_page.html")
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.BLOCKED
        assert result.block_signature is not None
        assert result.discovered == ()
        assert result.listings == ()


class TestMalformedResponse:
    def test_garbage_body_is_structure_changed(self, adapter: DicksAdapter) -> None:
        body = b"<html><body>Not a product-catalog response</body></html>"
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.discovered == ()

    def test_product_vos_present_but_not_a_list_is_structure_changed(
        self, adapter: DicksAdapter
    ) -> None:
        body = b'{"productVOs":[}malformed'
        result = adapter.parse(_response(body))

        assert result.outcome == ResponseOutcome.STRUCTURE_CHANGED
        assert result.discovered == ()

    def test_404_status_is_not_found(self, adapter: DicksAdapter) -> None:
        body = _load("search_sale.json")
        result = adapter.parse(_response(body, status=404))

        assert result.outcome == ResponseOutcome.NOT_FOUND
