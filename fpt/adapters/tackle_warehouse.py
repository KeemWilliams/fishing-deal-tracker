"""Tackle Warehouse adapter.

Page types covered (architecture doc 3.1 MVP adapter matrix):
- CLEARANCE_LISTING: `catpage-CLEAR*.html` grids, was/now prices with an
  MSRP-asterisk convention.
- USED_LISTING: `catpage-USED.html` grid (same markup as clearance grids).
- PRODUCT: `descpage-*.html`, server-rendered per-variant rows (no
  JSON-LD). Confirms discovery-grid candidates and backs BASELINE/HOT/
  CONFIRM observations.

Live structure was captured 2026-09-12 via a small number (5) of polite,
robots.txt-allowed, plain-HTTP fetches against tacklewarehouse.com, spaced
several seconds apart, no stealth. Trimmed, representative fixtures derived
from those live pages live under `tests/fixtures/tackle_warehouse/`.

Notable finding not anticipated by the architecture doc's adapter matrix
(flagged in this adapter's HANDOFF as [MEDIUM]): TW product pages DO expose
a per-variant `gtin13` in a `<meta itemprop="gtin13">` tag, contradicting
the matrix's "GTIN: No" for tackle_warehouse. This adapter captures it
opportunistically -- extra correct data is harmless, and the checksum
validator in fpt.core.gtin drops the frequent `gtin13="0"` placeholder
(no barcode on file) automatically.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Sequence
from urllib.parse import urljoin

from scrapling.parser import Selector

from fpt.adapters.base import (
    AdapterCapabilities,
    Availability,
    ClaimedReferenceKind,
    Condition,
    DiscoveredItem,
    FetchRequest,
    ParsedListing,
    ParsedOffer,
    ParseResult,
    PageType,
    ResponseOutcome,
    SellerType,
)
from fpt.core.conditions import load_condition_map, map_condition
from fpt.core.gtin import normalize_all
from fpt.core.money import parse_price_cents

# Content sentinels used by the validator's block/silent-failure check
# (architecture doc 6.2 last row). Presence of ANY of these in a page's
# body is sufficient evidence the page rendered real TW markup.
_LISTING_SENTINELS = (b"cattable-wrap-cell", b"gtm_impression")
_PRODUCT_SENTINELS = (b"js-ordering-subproduct", b"js-ordering-table")

_STOCK_DATE_RE = re.compile(r"^\d{1,2}/\d{1,2}$")
_STOCK_LEAD_TIME_RE = re.compile(r"^\d+\s*Days?$", re.IGNORECASE)
_STOCK_FLOOR_RE = re.compile(r"^(\d+)\+$")
_MSRP_CONDITION_RE = re.compile(
    r"MSRP\s*\$([0-9][0-9,]*\.\d{2})\s*-\s*([A-Za-z][A-Za-z ]*?)\s+condition\b",
    re.IGNORECASE,
)


def _clean_product_url(href: str | None, base_url: str) -> str | None:
    """TW's grid links carry a stray trailing `%0D` (a literal carriage
    return, percent-encoded) on every product href observed live. Strip it
    rather than treat the URL as a different page from its %0D-free
    canonical form -- otherwise every discovery hit would double-enroll."""
    if not href:
        return None
    cleaned = href.strip()
    if cleaned.lower().endswith("%0d"):
        cleaned = cleaned[:-3]
    return urljoin(base_url, cleaned)


def _extract_retailer_product_code(url: str) -> str | None:
    match = re.search(r"descpage-([A-Za-z0-9]+)\.html", url)
    return match.group(1) if match else None


def _parse_stock(raw: str | None) -> tuple[int | None, bool, date | None]:
    """Returns (stock_qty, stock_qty_is_floor, restock_date).

    TW's "In Stock" cell takes one of: a plain integer ("4"), a floor
    ("10+"), a restock date ("09/20", no year -- assumed current/next
    occurrence, left as None here since year inference belongs to the
    pipeline layer which knows "now"), or a lead-time string ("14 Days",
    not a calendar date -- recorded as no stock_qty and no restock_date,
    availability falls back to BACKORDER via the caller).
    """
    if raw is None:
        return None, False, None
    text = raw.strip()
    if not text:
        return None, False, None
    floor_match = _STOCK_FLOOR_RE.match(text)
    if floor_match:
        return int(floor_match.group(1)), True, None
    if text.isdigit():
        return int(text), False, None
    if _STOCK_DATE_RE.match(text):
        # MM/DD with no year in the source text; year resolution (this
        # year vs next) is a pipeline-layer concern since it requires
        # "now", which this pure parser deliberately has no dependency on.
        return None, False, None
    if _STOCK_LEAD_TIME_RE.match(text):
        return None, False, None
    return None, False, None


def _availability_for(stock_qty: int | None, raw: str | None) -> Availability:
    if stock_qty is not None and stock_qty > 0:
        return Availability.IN_STOCK
    if raw and (_STOCK_DATE_RE.match(raw.strip()) or _STOCK_LEAD_TIME_RE.match(raw.strip())):
        return Availability.BACKORDER
    if raw == "0":
        return Availability.OUT_OF_STOCK
    return Availability.UNKNOWN


class TackleWarehouseAdapter:
    slug = "tackle_warehouse"
    adapter_version = "2026-09-12.1"
    capabilities = AdapterCapabilities(
        adapter_kind="page_scraper",
        page_types=frozenset(
            {PageType.PRODUCT, PageType.CLEARANCE_LISTING, PageType.USED_LISTING}
        ),
        structured_source="html_text",
        exposes_gtin=True,  # see module docstring -- corrects the architecture matrix
        exposes_stock_qty=True,
        exposes_claimed_reference=True,  # MSRP asterisk on clearance/used grids
        exposes_used_offers=True,
        exposes_multiple_sellers=False,
        requires_js=False,
    )

    BASE_URL = "https://www.tacklewarehouse.com"

    def __init__(self, condition_map: dict[str, dict[str, str]] | None = None):
        self._condition_map = condition_map if condition_map is not None else load_condition_map()

    def build_request(self, task) -> FetchRequest:
        return FetchRequest(task_id=task.id, page_type=task.page_type, url=task.url)

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]:
        if page_type == PageType.PRODUCT:
            return _PRODUCT_SENTINELS
        return _LISTING_SENTINELS

    # ------------------------------------------------------------------
    # parse() dispatch
    # ------------------------------------------------------------------

    def parse(self, response) -> ParseResult:
        page_type = response.request.page_type
        try:
            if page_type == PageType.PRODUCT:
                return self._parse_product(response)
            if page_type in (PageType.CLEARANCE_LISTING, PageType.USED_LISTING):
                return self._parse_listing(response, page_type)
        except Exception as exc:  # noqa: BLE001 - parse() must never raise
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=[f"unhandled_parse_error:{exc.__class__.__name__}"],
                block_signature=None,
            )
        return ParseResult(
            outcome=ResponseOutcome.STRUCTURE_CHANGED,
            listings=(),
            discovered=(),
            next_page_url=None,
            warnings=[f"unsupported_page_type:{page_type}"],
            block_signature=None,
        )

    # ------------------------------------------------------------------
    # CLEARANCE_LISTING / USED_LISTING
    # ------------------------------------------------------------------

    def _parse_listing(self, response, page_type: PageType) -> ParseResult:
        sel = Selector(content=response.body)
        cells = sel.css("div.cattable-wrap-cell")
        warnings: list[str] = []
        discovered: list[DiscoveredItem] = []

        is_used = page_type == PageType.USED_LISTING

        for cell in cells:
            retailer_product_code = cell.attrib.get("data-code")
            href = cell.css("a.cattable-wrap-cell-info::attr(href)").get()
            product_url = _clean_product_url(href, response.final_url or self.BASE_URL)
            title = (cell.css("h3.cattable-wrap-cell-info-name::text").get() or "").strip()
            if not product_url or not title:
                warnings.append(f"skipped_cell_missing_url_or_title:{retailer_product_code}")
                continue

            price_now_raw = cell.css("div.cattable-wrap-cell-info-price > span::text").get()
            price_cents = parse_price_cents(price_now_raw)

            claimed_raw = cell.css(
                ".cattable-wrap-cell-info-price-msrp .is-crossout::text"
            ).get()
            claimed_reference_cents = parse_price_cents(claimed_raw)
            claimed_reference_kind = None
            if claimed_reference_cents is not None:
                # The MSRP block's raw HTML carries a trailing "*" sibling
                # text node when TW is asserting MSRP specifically; a bare
                # crossed-out price with no asterisk is their own prior
                # "was" price instead (architecture doc: "Yes (MSRP
                # asterisk on grid)").
                msrp_block_html = cell.css(
                    ".cattable-wrap-cell-info-price-msrp"
                ).get() or ""
                claimed_reference_kind = (
                    ClaimedReferenceKind.MSRP
                    if "*" in msrp_block_html.split("</span>")[-1]
                    or msrp_block_html.rstrip().endswith("*</span>")
                    else ClaimedReferenceKind.WAS
                )

            condition_hint = Condition.USED_UNGRADED if is_used else None

            discovered.append(
                DiscoveredItem(
                    product_url=product_url,
                    retailer_product_code=retailer_product_code,
                    title_raw=title,
                    price_cents=price_cents,
                    price_is_range=False,
                    claimed_reference_cents=claimed_reference_cents,
                    claimed_reference_kind=claimed_reference_kind,
                    condition_hint=condition_hint,
                    category_hint="other",
                )
            )

        outcome = ResponseOutcome.OK if discovered else ResponseOutcome.EMPTY
        return ParseResult(
            outcome=outcome,
            listings=(),
            discovered=discovered,
            next_page_url=self._next_page_url(sel, response.final_url),
            warnings=warnings,
            block_signature=None,
        )

    def _next_page_url(self, sel: Selector, final_url: str | None) -> str | None:
        # No pagination link was present on any sampled TW catpage (a
        # single flat grid per category, confirmed against a 76-item
        # clearance-rods sample and a 64-item used-gear sample on
        # 2026-09-12). This checks for a conventional rel="next" anchor
        # so pagination is honored automatically if TW adds it later,
        # without requiring an adapter_version bump for that alone.
        href = sel.css('a[rel="next"]::attr(href)').get()
        if not href:
            return None
        return urljoin(final_url or self.BASE_URL, href)

    # ------------------------------------------------------------------
    # PRODUCT
    # ------------------------------------------------------------------

    def _parse_product(self, response) -> ParseResult:
        sel = Selector(content=response.body)
        rows = sel.css("tr.js-ordering-subproduct")
        if not rows:
            return ParseResult(
                outcome=ResponseOutcome.EMPTY,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=["no_subproduct_rows"],
                block_signature=None,
            )

        brand_raw = sel.css("meta[itemprop=brand]::attr(content)").get()
        model_raw = sel.css("h1::text").get()
        model_raw = model_raw.strip() if model_raw else None
        retailer_product_code = sel.css(".gtm_detail::attr(data-gtm_detail_id)").get()

        # Item-condition free text for used items, e.g.
        # "MSRP $299.99 - Excellent condition. Handle slightly dirty...".
        # Present only on used-gear product pages (see module docstring).
        description_paragraphs = sel.css(
            "#product_overview .product-description p::text"
        ).getall()
        used_msrp_cents: int | None = None
        used_condition_raw: str | None = None
        for paragraph in description_paragraphs:
            match = _MSRP_CONDITION_RE.search(paragraph)
            if match:
                used_msrp_cents = parse_price_cents(match.group(1))
                used_condition_raw = match.group(2).strip()
                break

        warnings: list[str] = []
        listings: list[ParsedListing] = []

        for row in rows:
            retailer_sku = row.attrib.get("data-code")
            if not retailer_sku:
                warnings.append("skipped_row_missing_data_code")
                continue

            variant_label_raw = (row.css(".js-ordering-name::text").get() or "").strip()
            price_raw = row.css(".js-ordering-price::text").get()
            price_cents = parse_price_cents(price_raw)
            stock_raw = row.css(".js-ordering-available::text").get()
            stock_qty, stock_qty_is_floor, restock_date = _parse_stock(stock_raw)
            availability = _availability_for(stock_qty, stock_raw)

            gtin_candidates = row.css("meta[itemprop=gtin13]::attr(content)").getall()
            gtin14 = normalize_all([g for g in gtin_candidates if g])

            is_closeout = "data-closeout-item" in row.attrib
            list_price_raw = row.attrib.get("data-list-price")
            list_price_cents = parse_price_cents(list_price_raw) if list_price_raw else None

            attributes_raw: dict[str, str] = {}
            for style in row.css("li.js-ordering-style"):
                style_name = (style.css(".js-ordering-style-name::text").get() or "").strip()
                style_value = (style.css(".styleitem::text").get() or "").strip()
                if style_name:
                    attributes_raw[style_name] = style_value

            is_used_product = bool(used_condition_raw) or retailer_sku.upper().startswith("USED")

            if is_used_product:
                condition, condition_raw = map_condition(
                    self.slug,
                    used_condition_raw,
                    condition_map=self._condition_map,
                    default_new=False,
                )
                if condition is None:
                    condition = Condition.USED_UNGRADED
                claimed_cents = used_msrp_cents or list_price_cents
                claimed_kind = ClaimedReferenceKind.MSRP if used_msrp_cents else (
                    ClaimedReferenceKind.WAS if claimed_cents else None
                )
                on_clearance = True
                seller_type = SellerType.RETAILER_RESALE
            else:
                condition = Condition.NEW
                condition_raw = None
                claimed_cents = list_price_cents if is_closeout else None
                claimed_kind = ClaimedReferenceKind.WAS if claimed_cents else None
                on_clearance = is_closeout
                seller_type = SellerType.FIRST_PARTY

            offer = ParsedOffer(
                offer_key=retailer_sku,
                condition=condition,
                condition_raw=condition_raw,
                seller_key=self.slug,
                seller_name="Tackle Warehouse",
                seller_type=seller_type,
                price_cents=price_cents,
                shipping_cents=None,
                claimed_reference_cents=claimed_cents,
                claimed_reference_kind=claimed_kind,
                on_clearance=on_clearance,
                availability=availability,
                stock_qty=stock_qty,
                stock_qty_is_floor=stock_qty_is_floor,
                restock_date=restock_date,
                unit_count=None,
            )

            listings.append(
                ParsedListing(
                    retailer_sku=retailer_sku,
                    retailer_product_code=retailer_product_code,
                    url=response.final_url or response.request.url,
                    title_raw=variant_label_raw or (model_raw or ""),
                    brand_raw=brand_raw,
                    model_raw=model_raw,
                    variant_label_raw=variant_label_raw,
                    attributes_raw=attributes_raw,
                    gtin_raw=gtin14,
                    mpn_raw=None,
                    offers=(offer,),
                    source="html_text",
                )
            )

        outcome = ResponseOutcome.OK if listings else ResponseOutcome.EMPTY
        return ParseResult(
            outcome=outcome,
            listings=listings,
            discovered=(),
            next_page_url=None,
            warnings=warnings,
            block_signature=None,
        )
