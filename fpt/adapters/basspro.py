"""Bass Pro Shops (basspro.com) adapter.

Bass Pro is a Next.js storefront whose listing/search data does NOT come
from the rendered page HTML: category and search results load client-side
from a Coveo search API response (`platform.cloud.coveo.com/rest/search/v2`)
containing a top-level `results` array, each element carrying a `raw` object
with the actual product fields. Verified live 2026-09-13 against a real
captured response (see `tests/fixtures/_capture/basspro_coveo_rods.json`
and its README) -- confirmed field names below are exactly what that
response contains, not assumed from other retailers' shapes.

PURE PARSER, NO FETCHING (deliberate scope boundary): this module only
turns an already-fetched Coveo JSON response (`FetchResponse.body`) into a
`ParseResult`. Bass Pro's listing pages are Akamai-fronted and were only
observed to render for a real browser (see the `_capture` README) -- the
actual FETCH mechanism (a real-browser/Playwright-backed fetcher, run
outside the normal `HttpFetcher` path) is a separate, out-of-scope concern
handled elsewhere. Any caller that can produce a `FetchResponse` whose body
is this Coveo JSON shape -- stealth browser fetch, replay of a captured
response, etc. -- can drive this adapter; `parse()` never issues a network
request itself and never assumes anything about how the bytes arrived.

Contract mapping decision (see architecture doc 3.1's adapter contract,
`fpt/core/models.py`'s module docstring: "A grid row from a listing page is
a `DiscoveredItem`, never a `ParsedListing`/`ParsedOffer` observation
directly"): a Coveo search response is a listing/search-result page -- each
`raw` object is one grid row covering a whole product (potentially many
priced variants via `childcount`/`minofferprice`..`maxofferprice`), not one
already-confirmed SKU observation. This adapter therefore emits
`DiscoveredItem` rows (`PageType.CATALOG_LISTING` -- an ordinary category
search, not a clearance/sale-specific grid) rather than `ParsedListing`/
`ParsedOffer`, matching `academy.py`'s clearance listing and
`_shopify_collection.py`'s collection-JSON handling. Each real product's
`offerprice`/`listprice` fields were confirmed (across every sampled
result) to already equal that product's `minofferprice`/`minlistprice` --
i.e. the "selected/default variant" pricing IS the cheapest-variant
pricing -- so `offerprice`/`listprice` are used directly as the
representative price/reference rather than re-deriving them from the
min/max fields.

Fields confirmed present on every sampled `raw` object (2026-09-13 capture):
`title` (display name), `sku` (retailer's numeric product id -- confirmed
stable per product across sampled results), `offerprice` (current price,
USD dollars as a float/int, e.g. `49.99`), `listprice` (retailer's own
"list"/reference price, same shape), `minofferprice`/`maxofferprice`/
`minlistprice`/`maxlistprice` (variant price range across the product's
`childcount` variants), `producturlkeyword` (URL slug), `brand`,
`clbofferpriceusd` (a club/loyalty-tier price -- confirmed present but
NEVER used for deal math per the mission: it is a membership-gated price,
not this product's public retail price). No GTIN/UPC/EAN-shaped field was
found anywhere in the sampled `raw` objects (checked every key against the
same barcode-key pattern `fpt/adapters/_shared.py.find_barcodes` uses) --
`gtin_raw` is therefore always empty for this adapter; see HANDOFF
uncertainty.
"""

from __future__ import annotations

from typing import Sequence

from fpt.adapters._coveo import RESULTS_SENTINELS as _RESULTS_SENTINELS
from fpt.adapters._coveo import parse_coveo_search_json as _parse_coveo_search_json
from fpt.adapters._shared import is_blocked, load_allowed_hosts
from fpt.core.models import (
    AdapterCapabilities,
    CrawlTask,
    DiscoveredItem,
    FetchRequest,
    FetchResponse,
    PageType,
    ParseResult,
    ResponseOutcome,
)

SELLER_KEY = "basspro"
SELLER_NAME = "Bass Pro Shops"
BASE_URL = "https://www.basspro.com"

# H1: allowed hosts for this retailer's discovered-item URLs. Loaded once at
# import time -- config/retailers.yaml is a static file checked into the
# image, not something that changes mid-process (matches academy.py/
# jandh.py's module-level load).
ALLOWED_HOSTS = load_allowed_hosts(SELLER_KEY)


def parse_coveo_search_json(
    body: bytes,
    *,
    category_hint: str,
    allowed_hosts: frozenset[str],
) -> list[DiscoveredItem] | None:
    """Thin wrapper over `fpt.adapters._coveo.parse_coveo_search_json` bound
    to Bass Pro's own `base_url`/`/p/` product path.

    Extracted into the shared `_coveo` module while adding the Cabela's
    adapter (2026-09-13), which runs the identical Coveo backend/org (see
    `_coveo.py`'s module docstring). This wrapper keeps the module-level
    function name and signature `test_adapter_basspro.py` was already
    written against, so that suite required zero changes -- refactoring the
    shared logic out carried no risk to Bass Pro's existing coverage.
    """
    return _parse_coveo_search_json(
        body,
        category_hint=category_hint,
        allowed_hosts=allowed_hosts,
        base_url=BASE_URL,
        product_path_prefix="/p/",
    )


class BassProAdapter:
    """RetailerAdapter for basspro.com (page_scraper, Coveo search JSON).

    See module docstring: this adapter only parses an already-fetched Coveo
    response body -- it never fetches anything itself. `build_request`
    below is a normal `FetchRequest` builder to satisfy the
    `RetailerAdapter` protocol; the actual Akamai-aware fetch mechanism for
    this retailer is out of scope here and is expected to be supplied by a
    separate fetcher.
    """

    slug = SELLER_KEY
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        adapter_kind="page_scraper",
        page_types=frozenset({PageType.CATALOG_LISTING}),
        structured_source="api_json",
        exposes_gtin=False,
        exposes_stock_qty=False,
        exposes_claimed_reference=True,
        exposes_used_offers=False,
        exposes_multiple_sellers=False,
        requires_js=True,
    )

    def build_request(self, task: CrawlTask) -> FetchRequest:
        return FetchRequest(
            task_id=task.id,
            page_type=task.page_type,
            url=task.url,
            params=dict(task.params),
            render_js=True,
        )

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]:
        return _RESULTS_SENTINELS

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

        has_sentinel = any(sentinel in body for sentinel in _RESULTS_SENTINELS)
        if not has_sentinel:
            stripped = body.strip()
            outcome = ResponseOutcome.EMPTY if not stripped else ResponseOutcome.STRUCTURE_CHANGED
            return ParseResult(
                outcome=outcome,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("no recognizable content sentinels found on page",),
                block_signature=None,
            )

        category_hint = response.request.params.get("category_hint", "other")
        discovered = parse_coveo_search_json(
            body,
            category_hint=category_hint,
            allowed_hosts=ALLOWED_HOSTS,
        )
        if discovered is None:
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("results sentinel found but body was not the expected shape",),
                block_signature=None,
            )
        if not discovered:
            # A validly-shaped but genuinely empty result set (e.g.
            # "totalCount": 0) is EMPTY, not OK -- there is nothing to
            # enroll, and a search that legitimately returns zero rows
            # should be distinguishable from one that returned real rows.
            return ParseResult(
                outcome=ResponseOutcome.EMPTY,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("results array present but empty",),
                block_signature=None,
            )
        return ParseResult(
            outcome=ResponseOutcome.OK,
            listings=(),
            discovered=tuple(discovered),
            # Coveo search responses support offset-based pagination
            # (`firstResult`/`numberOfResults`), but this adapter does not
            # construct a next_page_url from those fields -- not observed/
            # exercised against a live paginated request this session. See
            # HANDOFF uncertainty MEDIUM.
            next_page_url=None,
            warnings=(),
            block_signature=None,
        )
