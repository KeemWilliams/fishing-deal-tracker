"""Cabela's (cabelas.com) adapter.

Cabela's and Bass Pro Shops are the same corporate parent (Bass Pro Group)
and run the IDENTICAL Coveo search backend/org -- confirmed live 2026-09-13
against a real captured response
(`tests/fixtures/_capture/cabelas_coveo_rods.json`): same top-level
`results` array, same `raw` object field names (`offerprice`, `listprice`,
`minofferprice`/`maxofferprice`, `minlistprice`/`maxlistprice`,
`clbofferpriceusd`, `title`, `sku`, `producturlkeyword`). See
`fpt/adapters/_coveo.py`'s module docstring for the shared parsing helper
this adapter delegates to, and `fpt/adapters/basspro.py`'s module docstring
for the full field-by-field rationale (unchanged for Cabela's -- same
backend, same fields, same "no GTIN field anywhere" finding).

PURE PARSER, NO FETCHING (deliberate scope boundary, same as basspro.py):
this module only turns an already-fetched Coveo JSON response
(`FetchResponse.body`) into a `ParseResult`. Cabela's listing pages are
Akamai-fronted like Bass Pro's and were only observed to render for a real
browser -- the actual FETCH mechanism is a separate, out-of-scope concern
handled elsewhere.

Contract mapping decision: identical to basspro.py -- a Coveo search
response is a listing/search-result page, so this adapter emits
`DiscoveredItem` rows (`PageType.CATALOG_LISTING`) rather than
`ParsedListing`/`ParsedOffer`.

Cabela's product pages live under the site's `/p/` path, same as Bass Pro's
(confirmed against `producturlkeyword` + the live captured response's own
result URIs -- see HANDOFF).
"""

from __future__ import annotations

from typing import Sequence

from fpt.adapters._coveo import RESULTS_SENTINELS as _RESULTS_SENTINELS
from fpt.adapters._coveo import parse_coveo_search_json
from fpt.adapters._shared import is_blocked, load_allowed_hosts
from fpt.core.models import (
    AdapterCapabilities,
    CrawlTask,
    FetchRequest,
    FetchResponse,
    PageType,
    ParseResult,
    ResponseOutcome,
)

SELLER_KEY = "cabelas"
SELLER_NAME = "Cabela's"
BASE_URL = "https://www.cabelas.com"

# H1: allowed hosts for this retailer's discovered-item URLs. Loaded once at
# import time, matching basspro.py's module-level load.
ALLOWED_HOSTS = load_allowed_hosts(SELLER_KEY)


class CabelasAdapter:
    """RetailerAdapter for cabelas.com (page_scraper, Coveo search JSON).

    See module docstring: this adapter only parses an already-fetched Coveo
    response body -- it never fetches anything itself. `build_request`
    below is a normal `FetchRequest` builder to satisfy the
    `RetailerAdapter` protocol; the actual Akamai-aware fetch mechanism for
    this retailer is out of scope here (same as basspro.py).
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
            base_url=BASE_URL,
            product_path_prefix="/p/",
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
            # "totalCount": 0) is EMPTY, not OK -- matches basspro.py.
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
            # exercised against a live paginated request this session,
            # matching basspro.py's same open uncertainty.
            next_page_url=None,
            warnings=(),
            block_signature=None,
        )
