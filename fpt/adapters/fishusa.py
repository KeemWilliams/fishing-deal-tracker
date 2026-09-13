"""FishUSA (fishusa.com) adapter.

Architecture doc references: section 3.1 (adapter contract), section 4.2
(GTIN field-name agnostic -- not applicable here, see capabilities note
below), section 6.2 (block/quality signatures).

Live structure captured 2026-09-12 via 2 polite, robots.txt-allowed, plain
scrapling `make_request` fetches against fishusa.com (well under the
10-request budget), spaced several seconds apart, identified UA (scrapling's
default `stealthy_headers`, no impersonation flag), matching the existing
adapters' fetch style. robots.txt names ClaudeBot/Claude-Web/anthropic-ai
explicitly with `Crawl-delay: 10` but only disallows narrow account/cart/
search paths -- both the product page and the clearance-category path used
here are allowed.

Page shapes:

- PRODUCT: a BigCommerce Stencil storefront on a *custom* "specialty-pdp"
  rod/reel theme (NOT the default `productView` template used by
  TackleDirect/alltackle -- see `_shared_stencil.py` for that one). One SKU
  per model/length/power/action combination, each rendered as its own
  `div.variant-container` "rod card" with a `table.rod-spec-table-mobile`
  label:value spec table, a numeric internal SKU (`div.rod-sku`, e.g.
  "#133465"), a live inventory count (`span.selected[data-inventory]`), and
  its own price (`span.regular-price-tag`). This "one product page, N
  server-rendered variant rows" shape is confirmed on the sampled rod page
  (Daiwa Spinmatic D, 9 length variants).
- CLEARANCE_LISTING: robots.txt allows
  `/Clearance/Clearance-Rods/` and confirms Academy's "clearance grid
  didn't render" finding from the research doc -- FishUSA's category grid is
  a Searchspring-powered `#athos-content` div populated entirely by
  client-side JS (confirmed live: the fetched page's grid container is
  empty, no product cards, no price text, in the plain-HTTP response).
  **Not included in this adapter's `capabilities.page_types`** -- see
  HANDOFF uncertainty. `parse()` still handles this page type defensively
  (never raises) by returning STRUCTURE_CHANGED if ever routed here anyway.

Capabilities notes vs. the general adapter matrix:
- `exposes_gtin=False`: no barcode/UPC/GTIN field was found anywhere on the
  sampled product page (confirmed by the research doc and by this session's
  own fetch) -- FishUSA is a price/availability source only, not a
  cross-retailer matching anchor.
- `exposes_claimed_reference=False`: the rod-card markup has no was/MSRP
  price element at all (unlike TackleDirect/alltackle's shared Stencil
  template, which has `rrp`/`non-sale` price spans even when empty). No
  clearance/discount signal was observed anywhere in this adapter's scope.
- `exposes_stock_qty=True`: unlike Academy (binary availability only),
  FishUSA's rod cards expose a real `data-inventory` integer per variant.
"""

from __future__ import annotations

from typing import Sequence

from scrapling.parser import Selector

from fpt.adapters._shared import is_blocked
from fpt.core.models import (
    AdapterCapabilities,
    Availability,
    Condition,
    CrawlTask,
    FetchRequest,
    FetchResponse,
    ParsedListing,
    ParsedOffer,
    ParseResult,
    PageType,
    ResponseOutcome,
    SellerType,
)
from fpt.core.money import parse_price_cents

SELLER_KEY = "fishusa"
SELLER_NAME = "FishUSA"

_PRODUCT_SENTINELS = (b"variant-container", b"rod-spec-table")
_LISTING_SENTINELS = (b"category-title", b"product-listing-container")


def _parse_spec_dict(variant_el) -> dict[str, str]:
    """Label:value pairs from a rod card's mobile spec table -- e.g.
    {"Model": "SMD401ULFS", "Length": "4' 0\"", "Power": "Ultralight", ...}.
    Uses the *-mobile table because it renders each attribute as its own
    `<tr>` (title cell + value cell), unlike the desktop table's separate
    header row / value row layout -- simpler and equally authoritative
    (same source data, confirmed identical on every sampled variant).
    """
    spec: dict[str, str] = {}
    for row in variant_el.css("table.rod-spec-table-mobile tr.rod-spec"):
        label = (row.css("td.rod-spec-title-mobile::text").get() or "").strip()
        value = (row.css("td.rod-spec-value-mobile::text").get() or "").strip()
        if label:
            spec[label] = value
    return spec


def _parse_variant(variant_el, brand_raw: str | None, title_raw: str) -> ParsedListing | None:
    retailer_product_code = variant_el.attrib.get("data-product-id")

    sku_raw = variant_el.css("div.rod-sku::text").get()
    retailer_sku = (sku_raw or "").strip().lstrip("#")
    if not retailer_sku:
        return None

    spec = _parse_spec_dict(variant_el)
    model_code = spec.pop("Model", None)
    variant_label_raw = model_code or ""

    price_raw = variant_el.css(".rod-price-container .regular-price-tag::text").get()
    price_cents = parse_price_cents(price_raw)

    inventory_raw = variant_el.css(
        ".rod-stock-message span.selected::attr(data-inventory)"
    ).get()
    stock_qty: int | None = None
    if inventory_raw is not None and inventory_raw.strip().isdigit():
        stock_qty = int(inventory_raw.strip())
    availability = (
        Availability.IN_STOCK
        if stock_qty is not None and stock_qty > 0
        else Availability.OUT_OF_STOCK
        if stock_qty is not None
        else Availability.UNKNOWN
    )

    offer = ParsedOffer(
        offer_key=retailer_sku,
        condition=Condition.NEW,
        condition_raw=None,
        seller_key=SELLER_KEY,
        seller_name=SELLER_NAME,
        seller_type=SellerType.FIRST_PARTY,
        price_cents=price_cents,
        shipping_cents=None,
        # capabilities.exposes_claimed_reference=False -- see module
        # docstring; no was/MSRP element exists in this theme's markup.
        claimed_reference_cents=None,
        claimed_reference_kind=None,
        on_clearance=False,
        availability=availability,
        stock_qty=stock_qty,
        stock_qty_is_floor=False,
        restock_date=None,
        unit_count=None,
    )

    return ParsedListing(
        retailer_sku=retailer_sku,
        retailer_product_code=retailer_product_code,
        url="",  # filled in by caller (product URL is the same for every variant)
        title_raw=title_raw,
        brand_raw=brand_raw,
        model_raw=title_raw,
        variant_label_raw=variant_label_raw,
        attributes_raw=spec,
        gtin_raw=[],  # capabilities.exposes_gtin=False -- see module docstring
        mpn_raw=None,
        offers=(offer,),
        source="html_text",
    )


class FishUSAAdapter:
    """RetailerAdapter for fishusa.com (page_scraper, plain HTML)."""

    slug = "fishusa"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        adapter_kind="page_scraper",
        page_types=frozenset({PageType.PRODUCT}),
        structured_source="html_text",
        exposes_gtin=False,
        exposes_stock_qty=True,
        exposes_claimed_reference=False,
        exposes_used_offers=False,
        exposes_multiple_sellers=False,
        requires_js=False,
    )

    BASE_URL = "https://www.fishusa.com"

    def build_request(self, task: CrawlTask) -> FetchRequest:
        return FetchRequest(
            task_id=task.id,
            page_type=task.page_type,
            url=task.url,
            params=dict(task.params),
            render_js=False,
        )

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]:
        if page_type == PageType.CLEARANCE_LISTING:
            return _LISTING_SENTINELS
        return _PRODUCT_SENTINELS

    def parse(self, response: FetchResponse) -> ParseResult:
        body = response.body
        try:
            return self._parse(response, body)
        except Exception as exc:  # noqa: BLE001 - parse() must never raise
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=[f"unhandled_parse_error:{exc.__class__.__name__}"],
                block_signature=None,
            )

    def _parse(self, response: FetchResponse, body: bytes) -> ParseResult:
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

        page_type = response.request.page_type

        if page_type != PageType.PRODUCT:
            # Not in this adapter's capabilities.page_types (see module
            # docstring: the clearance grid is Searchspring/JS-rendered and
            # never populates via plain HTTP) -- handled defensively rather
            # than raising, per the adapter contract.
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=[f"unsupported_page_type_requires_js:{page_type}"],
                block_signature=None,
            )

        sel = Selector(content=body)
        variant_els = sel.css("div.variant-container")
        if not variant_els:
            has_sentinel = any(sentinel in body for sentinel in _PRODUCT_SENTINELS)
            outcome = ResponseOutcome.STRUCTURE_CHANGED if has_sentinel else ResponseOutcome.EMPTY
            return ParseResult(
                outcome=outcome,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=["no_variant_containers_found"],
                block_signature=None,
            )

        title_raw = (sel.css("h1::text").get() or "").strip()
        brand_raw = sel.css(".productView-brand a span::text").get()
        brand_raw = brand_raw.strip() if brand_raw else None
        url = response.final_url or response.request.url

        listings: list[ParsedListing] = []
        warnings: list[str] = []
        for variant_el in variant_els:
            listing = _parse_variant(variant_el, brand_raw, title_raw)
            if listing is None:
                warnings.append("skipped_variant_missing_sku")
                continue
            listings.append(
                ParsedListing(
                    retailer_sku=listing.retailer_sku,
                    retailer_product_code=listing.retailer_product_code,
                    url=url,
                    title_raw=listing.title_raw,
                    brand_raw=listing.brand_raw,
                    model_raw=listing.model_raw,
                    variant_label_raw=listing.variant_label_raw,
                    attributes_raw=listing.attributes_raw,
                    gtin_raw=listing.gtin_raw,
                    mpn_raw=listing.mpn_raw,
                    offers=listing.offers,
                    source=listing.source,
                )
            )

        outcome = ResponseOutcome.OK if listings else ResponseOutcome.EMPTY
        return ParseResult(
            outcome=outcome,
            listings=tuple(listings),
            discovered=(),
            next_page_url=None,
            warnings=tuple(warnings),
            block_signature=None,
        )
