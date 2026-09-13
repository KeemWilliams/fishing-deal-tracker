"""Sportsman's Guide (sportsmansguide.com) adapter.

Legacy commerce platform (`/productlist?collection=...` URL pattern,
`OSC_WX2_google_sitemap_indexV2.xml` sitemap name). Live structure
captured 2026-09-13 via two polite, robots.txt-allowed, plain-HTTP fetches
against sportsmansguide.com (`/robots.txt` and the `fishdeals` listing
itself), spaced several seconds apart, no stealth -- see
`knowledge/research/fishing-big-outdoor-retailers-2026-09-13.md` for the
scouting pass this was built on and
`tests/fixtures/_capture/sw_sg_scratch/` (not committed) for the raw
capture this fixture was trimmed from.

Page type covered: CATALOG_LISTING only --
`/productlist?collection=fishdeals`. Each grid row is a
`<div id="product-tile-N" class="product-tile">`.

`CATALOG_LISTING` (not `CLEARANCE_LISTING`) per the mission brief and
matching basspro.py/cabelas.py/dicks.py's precedent: `fishdeals` is a
mixed sale collection, not a retailer-native "clearance flag" data
source.

Badge-based clearance/markdown filtering (mission brief, confirmed live
2026-09-13 -- every distinct badge text sampled on the `fishdeals`
collection, with a real `.was-price` present or absent per row):

    Badge text                | `.was-price` present? | Treated as a deal?
    ---------------------------|------------------------|---------------------
    "Clearance"                | Yes                    | Yes
    "On Sale"                  | Yes                    | Yes
    "Members Only Deal"        | No (sampled: 0/9)      | No
    "Members Only Save NN%"    | No (sampled: 0/10)     | No

The `fishdeals` collection mixes true markdowns ("Clearance"/"On Sale",
both of which reliably carried a real crossed-out "Was $X.XX" price in
every sampled row) with member-exclusive-discount badges that show no
comparable "was" price at all -- the actual discount is only revealed
behind a Buyers Club paywall/checkout (`<span class="club-price
apply-checkout"><span>See Member Price in Checkout</span></span>`, the
same "decoy price" shape as Dick's `priceIndicator` MAP suppression in
dicks.py). This adapter therefore applies a two-factor gate before
emitting a `DiscoveredItem`:

1. The tile's `.image-banner` badge text must case-insensitively equal
   "clearance" or "on sale" (regex `_QUALIFYING_BADGE_RE`) -- a
   percentage-off or generic "Members Only" badge is skipped outright,
   regardless of price data.
2. The mission's own backstop still applies independently: a parsed
   `claimed_reference_cents` (was) must be strictly greater than
   `price_cents` (current), or the row is skipped even if the badge
   qualified (defensive; no sampled row hit this case, but a badge
   surviving without a comparable was price should never emit a "deal").

Not captured (contract/data limitations, see HANDOFF): the Buyers Club
price (`.club-price`, e.g. "$53.99 Club") and the star rating -- both
appear in the source HTML, but `DiscoveredItem` (architecture doc 3.1,
`fpt/adapters/base.py`) has neither a club/member-price field nor a
rating field. There is nowhere to put either without a contract change,
which is out of this adapter's scope (matches sportsmans_warehouse.py's
identical rating limitation). GTIN/UPC: not present on this listing page
(confirmed live) and `DiscoveredItem` has no GTIN field regardless (a
`PRODUCT`-page/`ParsedListing` concept) -- out of scope per the mission
brief's instruction to prefer the listing-level was price.
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

SELLER_KEY = "sportsmans_guide"
SELLER_NAME = "Sportsman's Guide"
BASE_URL = "https://www.sportsmansguide.com"

# H1: allowed hosts for this retailer's discovered-item URLs. Loaded once at
# import time, matching the rest of this package's module-level load.
ALLOWED_HOSTS = load_allowed_hosts(SELLER_KEY)

# Presence of either substring is sufficient evidence the page rendered a
# real Sportsman's Guide product grid.
_LISTING_SENTINELS: tuple[bytes, ...] = (b"product-tile", b"plp-grid-items")

_CELL_CSS = "div.product-tile"

# See module docstring's badge table -- only these two badge texts carry a
# reliable "was" price on this collection.
_QUALIFYING_BADGE_RE = re.compile(r"^(clearance|on sale)$", re.IGNORECASE)


def _joined_direct_text(node) -> str:
    """Join a node's DIRECT text-child fragments (parsel/scrapling's
    `::text` on an element selects direct text children, not deeper
    descendant text) -- this reads e.g. `.regular-price`'s own "$59.99"
    text without also picking up the nested `<span class="slash"> /
    </span>` sibling, and `.club-price`'s "$53.99" without its nested
    `<span class="member-span">Club</span>`."""
    fragments = node.css("::text").getall()
    return " ".join(f.strip() for f in fragments if f and f.strip())


class SportsmansGuideAdapter:
    """RetailerAdapter for sportsmansguide.com (page_scraper, HTML text)."""

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
                warnings=("no product-tile cells found on page",),
                block_signature=None,
            )

        category_hint = response.request.params.get("category_hint", "other")
        warnings: list[str] = []
        discovered: list[DiscoveredItem] = []

        for cell in cells:
            badge_text = (cell.css(".image-banner::text").get() or "").strip()
            if not _QUALIFYING_BADGE_RE.match(badge_text):
                # Not a real clearance/on-sale markdown -- see module
                # docstring's badge table (Members Only Deal / Members
                # Only Save NN% never carry a comparable was price).
                warnings.append(f"skipped_non_clearance_badge:{badge_text or 'none'}")
                continue

            href = cell.css("a.anchor-container::attr(href)").get()
            code = cell.css("a.anchor-container::attr(pid)").get()
            candidate_url = urljoin(BASE_URL, href) if href else None
            product_url = sanitize_offer_url(
                candidate_url, allowed_hosts=ALLOWED_HOSTS, fallback_url=response.request.url
            )
            title = (cell.css(".product-name span::text").get() or "").strip()
            if not product_url or not title:
                warnings.append(f"skipped_cell_missing_url_or_title:{code}")
                continue

            price_node = cell.css("span.regular-price")
            price_cents = parse_price_cents(_joined_direct_text(price_node) if price_node else "")

            was_node = cell.css(".was-price .price.strike")
            claimed_cents = parse_price_cents(_joined_direct_text(was_node) if was_node else "")

            if price_cents is None or claimed_cents is None or claimed_cents <= price_cents:
                # Backstop guard (module docstring, factor 2): a qualifying
                # badge with no usable was-price > current-price pair is
                # never treated as a deal, even though the badge alone
                # matched -- no sampled row hit this branch, but the guard
                # must be structural, not merely observed.
                warnings.append(f"skipped_no_valid_was_price:{code}")
                continue

            discovered.append(
                DiscoveredItem(
                    product_url=product_url,
                    retailer_product_code=code,
                    title_raw=title,
                    price_cents=price_cents,
                    price_is_range=False,
                    claimed_reference_cents=claimed_cents,
                    claimed_reference_kind=ClaimedReferenceKind.WAS,
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
        # Confirmed live: `<div class="paging-item next"><a
        # href="/productlist?collection=fishdeals&pg=2">...</a></div>` --
        # no `rel="next"` attribute on this platform (unlike Sportsman's
        # Warehouse), so this selects on the wrapping `.next` div instead.
        href = sel.css("div.paging-item.next a::attr(href)").get()
        if not href:
            return None
        return urljoin(final_url or BASE_URL, href)
