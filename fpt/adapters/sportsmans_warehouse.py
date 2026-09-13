"""Sportsman's Warehouse (sportsmans.com) adapter.

SAP Commerce Cloud (Hybris) storefront. Live structure captured 2026-09-13
via two polite, robots.txt-allowed, plain-HTTP fetches against
sportsmans.com (`/robots.txt` and the fishing clearance listing itself),
spaced several seconds apart, no stealth -- see
`knowledge/research/fishing-big-outdoor-retailers-2026-09-13.md` for the
scouting pass this was built on and
`tests/fixtures/_capture/sw_sg_scratch/` (not committed) for the raw
capture this fixture was trimmed from.

Page type covered: CATALOG_LISTING only --
`/deals-clearance/fishing-clearance/c/cat101209`. Each grid row is a
`<div class="product-item js-product-item" data-product-code="..."
data-product-url="...">`. This is Sportsman's Warehouse's own dedicated
fishing-clearance category (confirmed live: `data-cnstrc-num-results`
reported 2960 items across 65 pages, matching the scout report), so every
row on this page is a real clearance item -- unlike Sportsman's Guide's
mixed `fishdeals` collection (see sportsmans_guide.py), no badge-based
filtering is needed here.

`CATALOG_LISTING` (not `CLEARANCE_LISTING`) per the mission brief and
matching the basspro.py/cabelas.py/dicks.py precedent: this page is an
ordinary retailer category grid, not a retailer-native "clearance flag"
data source the way Tackle Warehouse's MSRP-asterisk convention is.

Price shape (confirmed live, both single-price and per-variant-range
rows exist on the same page):
- Single: `<div class="smw-sale-price displayed-price">$63.97<span
  class="price-before price-del currency"><span class="sr-only">Original
  price:</span>$<span class="price-strikethrough">69.99</span></span></div>`
- Range (multi-variant product, e.g. a rod sold in several lengths):
  `$48.92 - $59.99` ... `<span class="price-strikethrough">$59.99 -
  $69.99</span>`. Per the adapter contract note in `base.py` ("a
  clearance grid often shows a product-level ... price"), the MIN of each
  range is recorded as `price_cents`/`claimed_reference_cents` and
  `price_is_range` is set True -- matching academy.py's product-level-
  range handling, not a per-variant confirmation (which would require a
  product-page fetch, out of scope here per the mission brief's
  instruction to prefer the listing-level was price).
- No was price at all is also observed live (a range-priced clearance row
  with an empty `.price-strikethrough`) -- `claimed_reference_cents` is
  `None` in that case, never guessed.

Not captured (contract/data limitations, see HANDOFF):
- Star rating: present on the grid (`.rating-stars` / `data-rating` JSON)
  but `DiscoveredItem` (architecture doc 3.1, `fpt/adapters/base.py`) has
  no rating field -- there is nowhere to put it without a contract change,
  which is out of this adapter's scope.
- GTIN/UPC: confirmed absent from this listing page (scout report) and,
  independently, `DiscoveredItem` has no GTIN field at all (that is a
  `PRODUCT`-page/`ParsedListing` concept) -- so this would need a
  per-product-page fetch regardless, which the mission brief says to
  avoid when the listing already carries a was price (it does here).
"""

from __future__ import annotations

import re
from typing import Sequence
from urllib.parse import urljoin

from scrapling.parser import Selector

from fpt.adapters._shared import is_blocked, load_allowed_hosts, sanitize_offer_url
from fpt.core.models import (
    AdapterCapabilities,
    ClaimedReferenceKind,
    CrawlTask,
    DiscoveredItem,
    FetchRequest,
    FetchResponse,
    PageType,
    ParseResult,
    ResponseOutcome,
)
from fpt.core.money import parse_price_cents

SELLER_KEY = "sportsmans_warehouse"
SELLER_NAME = "Sportsman's Warehouse"
BASE_URL = "https://www.sportsmans.com"

# H1: allowed hosts for this retailer's discovered-item URLs. Loaded once at
# import time, matching academy.py/basspro.py's module-level load.
ALLOWED_HOSTS = load_allowed_hosts(SELLER_KEY)

# Presence of either substring is sufficient evidence the page rendered a
# real Sportsman's Warehouse product grid (matches the rest of this
# package's content_sentinels/block-detection convention).
_LISTING_SENTINELS: tuple[bytes, ...] = (b"product__listing", b"js-product-item")

_CELL_CSS = "div.product-item.js-product-item"


def _joined_direct_text(node) -> str:
    """Join a node's DIRECT text-child fragments (parsel/scrapling's
    `::text` on an element selects direct text children, not deeper
    descendant text -- this is what lets us read the current-price text
    of `.smw-sale-price.displayed-price` without also picking up the
    nested `.price-before`/`.price-strikethrough` "was" markup, and
    without picking up the leading `<span class="sr-only">Sale price:
    </span>` sibling either, since that text belongs to the SPAN, not to
    this div)."""
    fragments = node.css("::text").getall()
    return " ".join(f.strip() for f in fragments if f and f.strip())


def _price_and_range(text: str) -> tuple[int | None, bool]:
    """Parses a price cell that is either a single price ("$63.97") or a
    per-variant range ("$48.92 - $59.99"). The MIN of a range is used --
    see module docstring. Returns (price_cents, price_is_range)."""
    if not text:
        return None, False
    is_range = "-" in text
    first_segment = text.split("-", 1)[0]
    return parse_price_cents(first_segment), is_range


class SportsmansWarehouseAdapter:
    """RetailerAdapter for sportsmans.com (page_scraper, HTML text)."""

    slug = SELLER_KEY
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        adapter_kind="page_scraper",
        page_types=frozenset({PageType.CATALOG_LISTING}),
        structured_source="html_text",
        exposes_gtin=False,
        exposes_stock_qty=False,
        exposes_claimed_reference=True,
        exposes_used_offers=False,
        exposes_multiple_sellers=False,
        requires_js=False,
    )

    def build_request(self, task: CrawlTask) -> FetchRequest:
        return FetchRequest(
            task_id=task.id,
            page_type=task.page_type,
            url=task.url,
            params=dict(task.params),
            render_js=False,
        )

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]:
        return _LISTING_SENTINELS

    def parse(self, response: FetchResponse) -> ParseResult:
        try:
            return self._parse(response)
        except Exception as exc:  # noqa: BLE001 - parse() must never raise
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=(f"unhandled_parse_error:{exc.__class__.__name__}",),
                block_signature=None,
            )

    def _parse(self, response: FetchResponse) -> ParseResult:
        body = response.body

        block_sig = is_blocked(body)
        if block_sig is not None:
            return ParseResult(
                outcome=ResponseOutcome.BLOCKED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=(),
                block_signature=block_sig,
            )

        if response.status == 404:
            return ParseResult(
                outcome=ResponseOutcome.NOT_FOUND,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=(),
                block_signature=None,
            )

        sel = Selector(content=body)
        cells = sel.css(_CELL_CSS)
        if not cells:
            has_sentinel = any(sentinel in body for sentinel in _LISTING_SENTINELS)
            outcome = ResponseOutcome.STRUCTURE_CHANGED if has_sentinel else ResponseOutcome.EMPTY
            return ParseResult(
                outcome=outcome,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("no product-item cells found on page",),
                block_signature=None,
            )

        category_hint = response.request.params.get("category_hint", "other")
        warnings: list[str] = []
        discovered: list[DiscoveredItem] = []

        for cell in cells:
            code = cell.attrib.get("data-product-code")
            href = cell.attrib.get("data-product-url")
            candidate_url = urljoin(BASE_URL, href) if href else None
            product_url = sanitize_offer_url(
                candidate_url, allowed_hosts=ALLOWED_HOSTS, fallback_url=response.request.url
            )
            title = (cell.css("a.name::text").get() or "").strip()
            if not product_url or not title:
                warnings.append(f"skipped_cell_missing_url_or_title:{code}")
                continue

            price_node = cell.css("div.smw-sale-price.displayed-price")
            current_text = _joined_direct_text(price_node) if price_node else ""
            price_cents, price_is_range = _price_and_range(current_text)

            was_raw = cell.css(".price-strikethrough::text").get()
            claimed_cents, _ = _price_and_range(was_raw or "")
            claimed_kind = ClaimedReferenceKind.WAS if claimed_cents is not None else None

            discovered.append(
                DiscoveredItem(
                    product_url=product_url,
                    retailer_product_code=code,
                    title_raw=title,
                    price_cents=price_cents,
                    price_is_range=price_is_range,
                    claimed_reference_cents=claimed_cents,
                    claimed_reference_kind=claimed_kind,
                    condition_hint=None,
                    category_hint=category_hint,
                )
            )

        outcome = ResponseOutcome.OK if discovered else ResponseOutcome.EMPTY
        return ParseResult(
            outcome=outcome,
            listings=(),
            discovered=tuple(discovered),
            next_page_url=self._next_page_url(sel, response.final_url),
            warnings=tuple(warnings),
            block_signature=None,
        )

    def _next_page_url(self, sel: Selector, final_url: str | None) -> str | None:
        # Confirmed live: a conventional `rel="next"` anchor
        # (`<li class="pagination-next"><a ... rel="next"></a></li>`) is
        # present through page 64 of 65 (`?page=0`..`?page=64`, i.e. 0
        # indexed even though the UI displays "Page 1".."Page 65"). Using
        # the `rel="next"` anchor rather than hand-building the `page=`
        # query param means this adapter never needs to know the total
        # page count itself.
        href = sel.css('a[rel="next"]::attr(href)').get()
        if not href:
            return None
        return urljoin(final_url or BASE_URL, href)
