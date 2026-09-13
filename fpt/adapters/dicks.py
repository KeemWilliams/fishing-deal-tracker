"""Dick's Sporting Goods (dickssportinggoods.com) adapter.

Dick's listing/search data does NOT come from the rendered page HTML: sale
and category grids load client-side from a `prod-catalog-product-api`
`v2/search` JSON response containing a top-level `productVOs` array, each
element carrying the retailer's own price fields plus a `dsgPriceIndicators`
object flagging MAP-restricted rows. Verified live 2026-09-13 against a real
captured response (see `tests/fixtures/_capture/dicks_search_sale.json` and
its README) -- confirmed field names below are exactly what that response
contains, not assumed from other retailers' shapes.

PURE PARSER, NO FETCHING (deliberate scope boundary, same as basspro.py/
cabelas.py): this module only turns an already-fetched `v2/search` JSON
response (`FetchResponse.body`) into a `ParseResult`. Dick's `v2/search`
endpoint is a JSON API a real browser calls, not something a plain HTTP
client is confirmed to receive directly -- the actual FETCH mechanism (a
real-browser/Playwright-backed fetcher) is a separate, out-of-scope concern
handled elsewhere, matching the basspro/cabelas split.

Contract mapping decision (architecture doc 3.1's adapter contract): each
`productVOs` entry is one grid row on a listing/search page, not an
already-confirmed SKU observation -- this adapter therefore emits
`DiscoveredItem` rows (`PageType.CATALOG_LISTING`; the `/f/sale` page this
adapter targets is a sale grid, but it is not retailer-native "clearance"
metadata the way Tackle Warehouse's clearance flag is, so CATALOG_LISTING
matches basspro.py/cabelas.py's ordinary-category-grid treatment rather
than CLEARANCE_LISTING).

Fields confirmed present on every sampled `productVOs` entry (2026-09-13
capture):
- `name` (display title), `partnumber` (this specific color/size variant's
  numeric catalog id), `parentPartnumber` (the style-level code shared
  across colors of the same product -- also the id segment embedded in
  `assetSeoUrl`, e.g. style `25FHQWRH8WHTWHTXXFTW` -> url
  `/p/hoka-womens-arahi-8-running-shoes-25fhqwrh8whtwhtxxftw/
  25fhqwrh8whtwhtxxftw`), `assetSeoUrl`/`dsgSeoUrl` (identical relative
  product-page path in every sampled row, `/p/<slug>/<style-id>`).
- `dsgPriceIndicators`: `{"priceIndicator": 0|1, "mapPriceIndicator": 0|1,
  "dealsPercentage": <float>}`. `priceIndicator == 1` is Dick's own
  "See Price in Cart" MAP-enforcement signal -- in every sampled row where
  it is 1, the row's own price fields still contain non-zero, non-discounted
  numbers (`offerprice == listprice == mapprice`, or a 0.0 mapprice on one
  sampled row), i.e. this is retailer-suppressed/decoy pricing, not a real
  sellable price, and this adapter never treats it as a deal candidate --
  see the mission's "skip MAP/'price in cart' items" instruction and
  HANDOFF uncertainty for the reasoning distinguishing this from
  `mapPriceIndicator` (see below).
- `floatFacets`: a flat array of `{"identifier": <str>, "value": <float>,
  "partnumber": <str>, ...}` objects. The SAME array structure carries price
  facets for OTHER brands under this API (`publiclands*`, `golfgalaxy*`,
  `fieldandstream*` prefixes were all observed in the sampled response
  alongside `dickssportinggoods*`) -- this adapter only ever reads the
  `dickssportinggoods`-prefixed identifiers
  (`dickssportinggoodsofferprice`/`dickssportinggoodslistprice`/
  `dickssportinggoodsmapprice`), matching the mission's dicks.com scope.
  No min/max variant-range price field was found anywhere in the sampled
  shape (unlike the Coveo-backed adapters), so `price_is_range` is always
  False for this adapter -- each row's price is a specific,
  already-selected color/size variant's price, not a "starting at" figure.
- No GTIN/UPC/EAN-shaped field was found anywhere in the sampled entries
  (checked every top-level key against the same barcode-key pattern
  `fpt/adapters/_shared.py.find_barcodes` uses; `attributes` is present but
  is itself a JSON-encoded string of brand/category facets, not a barcode)
  -- `gtin_raw` is therefore always empty for this adapter.

`mapPriceIndicator` vs `priceIndicator` (HANDOFF uncertainty, HIGH): the
mission brief names "mapPriceIndicator" as one of the possible MAP-restriction
signals. Empirically, in the sampled response, rows with `mapPriceIndicator
== 1` still carried a real, usable `dickssportinggoodsofferprice` value below
`dickssportinggoodslistprice` (e.g. sku 25437277: offer 119.99 < list
149.99) -- these look like ordinary MAP-compliant discounted rows (the
"map" price sets a strikethrough floor, but Dick's is still allowed to show
the sale price). Only `priceIndicator == 1` rows showed the "decoy" pattern
(offer == list == map, or a suspicious 0.0). This adapter therefore treats
`priceIndicator` (not `mapPriceIndicator`) as the skip signal, plus an
independent "missing/non-positive offer price" guard for the "zero-price"
case named in the same brief sentence. This split was NOT independently
confirmed against Dick's live site UI (e.g. actually loading a
`priceIndicator == 1` product page to see "See Price in Cart" rendered) --
see HANDOFF.
"""

from __future__ import annotations

import json
from typing import Any, Sequence

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

SELLER_KEY = "dicks"
SELLER_NAME = "Dick's Sporting Goods"
BASE_URL = "https://www.dickssportinggoods.com"

# H1: allowed hosts for this retailer's discovered-item URLs. Loaded once at
# import time, matching basspro.py/cabelas.py's module-level load.
ALLOWED_HOSTS = load_allowed_hosts(SELLER_KEY)

# `prod-catalog-product-api` v2/search response envelope. Byte-substring
# sentinel check, matching the rest of this package's `is_blocked`/
# `content_sentinels` convention -- a truncated or block-page body should
# hit `is_blocked`/EMPTY/STRUCTURE_CHANGED via the normal parse path, not
# raise here.
_PRODUCT_VOS_SENTINELS: tuple[bytes, ...] = (b'"productVOs":[', b'"productVOs": [')

# Only this retailer's own price facets -- see module docstring for why
# other brands' facets (publiclands*, golfgalaxy*, fieldandstream*) share
# this same array and must never be read here.
_OFFER_PRICE_KEY = "dickssportinggoodsofferprice"
_LIST_PRICE_KEY = "dickssportinggoodslistprice"


def _dollars_to_cents(value: Any) -> int | None:
    """Dick's prices are USD dollar amounts (float), e.g. 119.99. Never
    guesses -- a missing/non-numeric/non-positive value is None, per the
    adapter contract ("a missing price is None, not 0")."""
    if isinstance(value, bool):  # bool is an int subclass; exclude explicitly
        return None
    if not isinstance(value, (int, float)):
        return None
    cents = round(value * 100)
    return cents if cents > 0 else None


def _own_price_facets(product: dict[str, Any]) -> dict[str, Any]:
    """Extract this retailer's own `dickssportinggoods*` price facets from a
    product's `floatFacets` array, keyed by their bare identifier suffix
    (i.e. `_OFFER_PRICE_KEY`/`_LIST_PRICE_KEY` as returned)."""
    facets = product.get("floatFacets")
    if not isinstance(facets, list):
        return {}
    found: dict[str, Any] = {}
    for facet in facets:
        if not isinstance(facet, dict):
            continue
        identifier = facet.get("identifier")
        if identifier in (_OFFER_PRICE_KEY, _LIST_PRICE_KEY):
            found[identifier] = facet.get("value")
    return found


def parse_product_catalog_search_json(
    body: bytes,
    *,
    category_hint: str,
    allowed_hosts: frozenset[str],
) -> list[DiscoveredItem] | None:
    """Parse a `prod-catalog-product-api` `v2/search` response body into
    discovery rows.

    Returns None when the body cannot be interpreted as this endpoint's JSON
    shape at all (caller maps that to STRUCTURE_CHANGED); returns an empty
    list for a validly-shaped but empty result set (matching
    basspro.py/cabelas.py's empty-vs-unparseable distinction).
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    products = payload.get("productVOs")
    if not isinstance(products, list):
        return None

    discovered: list[DiscoveredItem] = []
    for product in products:
        if not isinstance(product, dict):
            continue

        indicators = product.get("dsgPriceIndicators")
        if isinstance(indicators, dict) and indicators.get("priceIndicator"):
            # "See Price in Cart" MAP-enforcement decoy row -- never a real
            # sellable price. See module docstring for why this is
            # `priceIndicator`, not `mapPriceIndicator`.
            continue

        facets = _own_price_facets(product)
        price_cents = _dollars_to_cents(facets.get(_OFFER_PRICE_KEY))
        if price_cents is None:
            # Covers both "no dickssportinggoods facet at all" and the
            # "zero/absent displayed price" case named in the mission brief.
            continue
        list_cents = _dollars_to_cents(facets.get(_LIST_PRICE_KEY))
        claimed_cents = list_cents if list_cents is not None and list_cents > price_cents else None
        claimed_kind = ClaimedReferenceKind.LIST if claimed_cents is not None else None

        seo_path = product.get("assetSeoUrl") or product.get("dsgSeoUrl")
        if not isinstance(seo_path, str) or not seo_path:
            continue
        # `assetSeoUrl`/`dsgSeoUrl` are relative paths (e.g.
        # "/p/<slug>/<id>") -- untrusted page content, so route through the
        # same sanitizing guard as every other adapter (security review H1)
        # rather than assuming string interpolation alone is safe.
        candidate_url = f"{BASE_URL}{seo_path}" if seo_path.startswith("/") else seo_path
        url = sanitize_offer_url(candidate_url, allowed_hosts=allowed_hosts)
        if not url:
            continue

        code = product.get("parentPartnumber") or product.get("partnumber")
        discovered.append(
            DiscoveredItem(
                product_url=url,
                retailer_product_code=str(code) if code is not None else None,
                title_raw=str(product.get("name") or ""),
                price_cents=price_cents,
                # No min/max variant-range price field exists in this
                # shape -- each row is a specific already-selected
                # color/size variant's price. See module docstring.
                price_is_range=False,
                claimed_reference_cents=claimed_cents,
                claimed_reference_kind=claimed_kind,
                condition_hint=None,
                category_hint=category_hint,
            )
        )
    return discovered


class DicksAdapter:
    """RetailerAdapter for dickssportinggoods.com (page_scraper, JSON API).

    See module docstring: this adapter only parses an already-fetched
    `v2/search` response body -- it never fetches anything itself.
    `build_request` below is a normal `FetchRequest` builder to satisfy the
    `RetailerAdapter` protocol; the actual fetch mechanism for this
    retailer's JSON API is out of scope here (same as basspro.py/cabelas.py).
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
        return _PRODUCT_VOS_SENTINELS

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

        has_sentinel = any(sentinel in body for sentinel in _PRODUCT_VOS_SENTINELS)
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
        discovered = parse_product_catalog_search_json(
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
                warnings=("productVOs sentinel found but body was not the expected shape",),
                block_signature=None,
            )
        if not discovered:
            # A validly-shaped but genuinely empty result set (e.g. every
            # row was MAP-restricted/priceless, or "productVOs": []) is
            # EMPTY, not OK -- matches basspro.py/cabelas.py.
            return ParseResult(
                outcome=ResponseOutcome.EMPTY,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("productVOs array present but empty",),
                block_signature=None,
            )
        return ParseResult(
            outcome=ResponseOutcome.OK,
            listings=(),
            discovered=tuple(discovered),
            # Dick's search supports page-number pagination
            # (`searchVO.pageNumber`/`pageSize`/`totalCount`), but this
            # adapter does not construct a next_page_url from those fields
            # -- not observed/exercised against a live paginated request
            # this session. See HANDOFF uncertainty MEDIUM.
            next_page_url=None,
            warnings=(),
            block_signature=None,
        )
