"""TackleDirect (tackledirect.com) adapter.

Architecture doc references: section 3.1 (adapter contract), section 6.2
(block/quality signatures).

Live structure captured 2026-09-12 via 2 polite, robots.txt-allowed
scrapling `make_request` fetches (well under the 10-request budget). A bare
`curl` with a generic UA hit a transient "large amount of traffic, please
try again later" interstitial on this domain during this session; scrapling's
default browser-fingerprint request succeeded with a normal 200 on both
fetches, no stealth/impersonation flag needed. robots.txt for the ClaudeBot
block only disallows narrow account/cart/checkout/admin paths -- product
pages are allowed.

TackleDirect is a BigCommerce Stencil storefront on the **default**
`productView` template (see `_shared_stencil.py`, shared with alltackle.com
-- same theme family, byte-identical markup for the fields this adapter
reads).

Two PRODUCT page shapes exist at this URL pattern, and only one is
supported:

- **Single-SKU pages** (e.g. `tsunami-tsshdii4000-shield-ii-spinning-reel
  .html`): the current price, SKU, and (when the product has one on file) a
  12-digit UPC-A are all present as static HTML in `dl.productView-info` /
  `span[data-product-price-without-tax]`. Fully supported.
- **Multi-variant "series" pages** (e.g.
  `tsunami-evict-ii-spinning-reels.html`, one URL for an entire product line
  with a length/size dropdown): confirmed live that the price span renders
  `$0.00` with `style="display:none"` and the `data-product-option-change`
  container is completely empty in the raw HTTP response -- the entire
  variant picker and per-variant price are injected by client-side JS with
  no fallback data in the static markup (no JSON blob, no `<select>`
  options, nothing to enumerate variants from). Per the CODE-phase task
  instructions, this shape is **not half-parsed**: `parse()` detects the
  missing/zero price on an otherwise-well-formed product page and returns
  `STRUCTURE_CHANGED` with an explicit `multi_variant_series_requires_js`
  warning rather than emitting a fake $0 offer or guessing a variant.

Capabilities notes vs. the general research-doc verdict ("None found -- no
Product JSON-LD, no barcode field anywhere"): that finding was about
structured data (JSON-LD/microdata) specifically. This adapter's *plain
HTML* parse of single-SKU pages found a `dd[data-product-upc]` element
carrying a real 12-digit UPC-A on every single-SKU page sampled --
`exposes_gtin=True` is correct once you read the DOM rather than only
JSON-LD, mirroring the tackle_warehouse adapter's own correction of the
architecture matrix for a similar reason (see that adapter's docstring).
`exposes_stock_qty=False`: the `span[data-product-stock]` element is always
empty in the static HTML on both sampled pages; only a binary
`meta[property="og:availability"]` availability signal is reliably present.
"""

from __future__ import annotations

from typing import Sequence

from fpt.adapters._shared import is_blocked
from fpt.adapters._shared_stencil import (
    extract_brand,
    extract_claimed_reference,
    extract_current_price_cents,
    extract_og_availability_raw,
    extract_product_id,
    extract_sku,
    extract_title,
    extract_upc,
    map_og_availability,
)
from fpt.core.gtin import normalize_all
from fpt.core.models import (
    AdapterCapabilities,
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

SELLER_KEY = "tackledirect"
SELLER_NAME = "TackleDirect"

_PRODUCT_SENTINELS = (b"data-product-sku", b"productView-title")


class TackleDirectAdapter:
    """RetailerAdapter for tackledirect.com (page_scraper, plain HTML)."""

    slug = "tackledirect"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        adapter_kind="page_scraper",
        page_types=frozenset({PageType.PRODUCT}),
        structured_source="html_text",
        exposes_gtin=True,
        exposes_stock_qty=False,
        exposes_claimed_reference=True,
        exposes_used_offers=False,
        exposes_multiple_sellers=False,
        requires_js=False,
    )

    BASE_URL = "https://www.tackledirect.com"

    def build_request(self, task: CrawlTask) -> FetchRequest:
        return FetchRequest(
            task_id=task.id,
            page_type=task.page_type,
            url=task.url,
            params=dict(task.params),
            render_js=False,
        )

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]:
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

        if response.request.page_type != PageType.PRODUCT:
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=[f"unsupported_page_type:{response.request.page_type}"],
                block_signature=None,
            )

        html_text = body.decode("utf-8", errors="replace")
        has_sentinel = any(sentinel in body for sentinel in _PRODUCT_SENTINELS)
        title_raw = extract_title(html_text)

        if not has_sentinel:
            outcome = ResponseOutcome.EMPTY if not title_raw else ResponseOutcome.STRUCTURE_CHANGED
            return ParseResult(
                outcome=outcome,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("no recognizable content sentinels found on page",),
                block_signature=None,
            )

        price_cents = extract_current_price_cents(html_text)
        if price_cents is None:
            # Multi-variant "series" page -- see module docstring. Reported
            # distinctly from a generic structure change so downstream
            # tooling can filter this known, documented limitation.
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("multi_variant_series_requires_js",),
                block_signature=None,
            )

        retailer_sku = extract_sku(html_text)
        if not retailer_sku:
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("no sku found on product page",),
                block_signature=None,
            )

        upc = extract_upc(html_text)
        gtin_raw = normalize_all([upc]) if upc else []

        claimed_cents, claimed_kind = extract_claimed_reference(html_text)
        availability = map_og_availability(extract_og_availability_raw(html_text))
        brand_raw = extract_brand(html_text)
        product_id = extract_product_id(html_text)

        offer = ParsedOffer(
            offer_key=retailer_sku,
            condition=Condition.NEW,
            condition_raw=None,
            seller_key=SELLER_KEY,
            seller_name=SELLER_NAME,
            seller_type=SellerType.FIRST_PARTY,
            price_cents=price_cents,
            shipping_cents=None,
            claimed_reference_cents=claimed_cents,
            claimed_reference_kind=claimed_kind,
            on_clearance=claimed_cents is not None,
            availability=availability,
            stock_qty=None,  # capabilities.exposes_stock_qty=False -- see module docstring
            stock_qty_is_floor=False,
            restock_date=None,
            unit_count=None,
        )

        listing = ParsedListing(
            retailer_sku=retailer_sku,
            retailer_product_code=product_id,
            url=response.final_url or response.request.url,
            title_raw=title_raw,
            brand_raw=brand_raw,
            model_raw=None,
            variant_label_raw=title_raw,
            attributes_raw={},
            gtin_raw=gtin_raw,
            mpn_raw=None,
            offers=(offer,),
            source="html_text",
        )

        return ParseResult(
            outcome=ResponseOutcome.OK,
            listings=(listing,),
            discovered=(),
            next_page_url=None,
            warnings=(),
            block_signature=None,
        )
