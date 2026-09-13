"""alltackle.com adapter.

Architecture doc references: section 3.1 (adapter contract), section 6.2
(block/quality signatures).

Live structure captured 2026-09-12 via 2 polite, robots.txt-allowed
scrapling `make_request` fetches (well under the 10-request budget).
robots.txt's ClaudeBot block only disallows narrow account/cart/checkout/
admin/search paths -- product pages are allowed, no crawl-delay directive
found for this specific domain's robots.txt (unlike fishusa.com's explicit
`Crawl-delay: 10`); this adapter's retailer policy config nonetheless uses
the same conservative `min_delay_s: 10` as the other two new retailers,
since alltackle.com is confirmed to be the same BigCommerce-network/
storefront family.

alltackle.com is, like TackleDirect, a BigCommerce Stencil storefront on the
**default** `productView` template (see `_shared_stencil.py` -- shared
parsing helpers, byte-identical markup for the fields this adapter reads,
confirmed live across both retailers). One SKU per product URL; variants
(if any) are separate pages entirely, matching the research doc's finding.

Confirms the research doc's UPC finding, with one correction: UPC is
present as a `dd[data-product-upc]` DOM attribute value (same Stencil field
TackleDirect exposes), not merely "plain page text" requiring a bespoke
"UPC: 843022006090"-style text regex -- it lives in the same structured
`dl.productView-info` list as the SKU. Not every product has one on file
(one of the two sampled fixtures has no UPC at all), so `exposes_gtin=True`
means "present when the retailer has it on file", matching the pattern used
by tackle_warehouse's checksum-validated `gtin_raw` list (empty list, not an
error, when absent).

Confirms a "was" price IS observable here (the research doc found no
was/now pair on either site sampled that pass, but did not sample a
discounted item): the same `rrp`/`non-sale` Stencil price spans used by
TackleDirect are populated with a real "Was: $43.99" -> "Now: $39.99" pair
on a sampled clearance-priced lure -- `exposes_claimed_reference=True`.

`og:availability` values differ in spelling from TackleDirect's ("oos" here
vs. "instock"/presumably "outofstock" there) -- both are handled by the
shared `map_og_availability` lookup table in `_shared_stencil.py`.
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

SELLER_KEY = "alltackle"
SELLER_NAME = "alltackle.com"

_PRODUCT_SENTINELS = (b"data-product-sku", b"productView-title")


class AlltackleAdapter:
    """RetailerAdapter for alltackle.com (page_scraper, plain HTML)."""

    slug = "alltackle"
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

    BASE_URL = "https://alltackle.com"

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
            # Same multi-variant/JS-only shape as TackleDirect (not sampled
            # live for this retailer, but the two sites share the identical
            # Stencil template -- handled identically rather than assumed
            # not to occur; see tackledirect.py module docstring).
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
