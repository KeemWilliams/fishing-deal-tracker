"""Shared adapter for Shopify storefronts whose sale/clearance discovery and
per-product confirm data are read from Shopify's own public JSON endpoints
rather than scraped out of rendered HTML.

Backs three retailers added 2026-09-12 (see knowledge/research/fishing-
additional-retailers-2026-09-12.md): Fishing Online (fishingonline.com),
Discount Tackle (discounttackle.com), and Rod Locker (rodlocker.com --
American Legacy Fishing Co. now 301-redirects here and is treated as ONE
retailer, never tracked separately). All three run the same Shopify theme
family already seen in jandh.py, but unlike jandh (which scrapes an
embedded `ProductJson` `<script>` blob plus JSON-LD out of rendered HTML),
this shared adapter fetches two of Shopify's own machine-readable
endpoints directly -- verified live 2026-09-12 via <=3 polite,
robots.txt-allowed requests per host, >=10s apart, well under the 10-
request budget:

- DISCOVERY (CLEARANCE_LISTING): `GET /collections/<handle>/products.json`.
  The sale/clearance collection HANDLE differs per retailer and is NOT
  always "clearance" -- confirmed live: Fishing Online's real handle is
  `sale` (its own `/collections/clearance` is empty), Rod Locker's is also
  `sale` (same gotcha), Discount Tackle's is `clearance` (verified
  non-empty, active `SummerMegaSale`/`LaborDay2023` tags on returned
  products). Each retailer module below passes its own verified handle to
  this adapter's constructor -- never assume "clearance" is universal for
  a new Shopify retailer.

  Prices on this endpoint are decimal STRINGS (e.g. `"6.39"`), confirmed
  across all three hosts -- `fpt.core.money.parse_price_cents` (built for
  exactly this "$X.YY"/"X.YY" shape) is reused rather than re-implemented.
  A `DiscoveredItem` is one grid row, not one variant (per the adapter
  contract in `fpt/core/models.py`): each Shopify "product" in the JSON
  response can carry many priced variants, so this adapter reduces them to
  a single representative price (cheapest *available* variant, falling
  back to cheapest overall if none are available) and sets
  `price_is_range=True` whenever the product's variants do not all share
  that one price -- the same "grid shows one price, real per-variant
  prices are confirmed on the product page" rule jandh's catalog listing
  and academy's clearance listing already follow.

- CONFIRM (PRODUCT): `GET /products/<handle>.js`. Returns one product's
  full variant list with `price`/`compare_at_price` as integer CENTS (a
  DIFFERENT shape than the collection endpoint above -- confirmed by
  fetching the same product/variant through both endpoints and comparing)
  and a per-variant `barcode` field always present in the response shape
  (verified 2026-09-12, all 3 hosts), unlike jandh.com's on-page
  `ProductJson`, which sometimes needs a JSON-LD fallback for barcodes.
  `.js` is not disallowed by any of the three hosts' robots.txt (only
  `/cart.js` and `/recommendations/products` are JS-endpoint disallows;
  the general `Allow: /` plus `Allow: /products/*` cover
  `/products/<handle>.js`). `build_request` derives this fetch URL from
  `task.url` (the canonical `/products/<handle>` page) by appending
  `.js` -- the tracked/enrolled URL stored elsewhere in the system stays
  the human-visible canonical product page for `allowed_hosts`/display
  purposes; only the outbound fetch target differs, and `parse()`
  recovers the canonical URL by stripping the same suffix back off
  `response.request.url` before building `ParsedListing.url`.

Unlike jandh (a deliberate reference-anchor-only exception --
`capabilities.exposes_claimed_reference=False`), all three retailers here
DO expose a `compare_at_price` on clearance-tagged variants (confirmed
live on every sampled product) -- `capabilities.exposes_claimed_reference
=True`, mapped to `ClaimedReferenceKind.COMPARE_AT`. A `compare_at_price`
equal to (not greater than) `price` is treated as "no claimed reference"
(observed live: some non-discounted variants on a sale collection page
carry an identical compare_at_price/price pair), never surfaced as a
false discount.
"""

from __future__ import annotations

import json
from typing import Any, Sequence
from urllib.parse import urlsplit

from fpt.adapters._shared import (
    find_barcodes,
    is_blocked,
    load_allowed_hosts,
    parse_pack_count,
    sanitize_offer_url,
)
from fpt.core.models import (
    AdapterCapabilities,
    Availability,
    ClaimedReferenceKind,
    Condition,
    CrawlTask,
    DiscoveredItem,
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

# Sentinels for the two JSON shapes this adapter ever sees. Deliberately
# byte-substring checks (matching the rest of this package's `is_blocked`/
# `content_sentinels` convention) rather than a speculative json.loads --
# a truncated or block-page body should hit `is_blocked`/EMPTY/STRUCTURE_
# CHANGED via the normal parse path, not raise here.
_COLLECTION_SENTINELS: tuple[bytes, ...] = (b'"products":[', b'"products": [')
_PRODUCT_SENTINELS: tuple[bytes, ...] = (b'"variants":[', b'"variants": [')


def _price_string_to_cents(raw: Any) -> int | None:
    """Collection endpoint prices: decimal strings, e.g. "6.39"."""
    if raw is None:
        return None
    return parse_price_cents(str(raw))


def _cents_field_to_int(raw: Any) -> int | None:
    """Confirm endpoint prices: already integer cents, e.g. 639. Never
    guesses -- a non-numeric or missing value is None, matching the
    adapter contract ("a missing price is None, not 0")."""
    if isinstance(raw, bool):  # bool is an int subclass; exclude explicitly
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        return int(raw) if raw > 0 else None
    return None


def parse_collection_products_json(
    body: bytes,
    *,
    category_hint: str,
    canonical_host: str,
    allowed_hosts: frozenset[str],
) -> list[DiscoveredItem] | None:
    """Parse a `/collections/<handle>/products.json` response into
    discovery rows. Returns None on a body that cannot be interpreted as
    this endpoint's JSON shape at all (caller maps that to STRUCTURE_
    CHANGED); returns an empty list for a validly-shaped but empty
    collection (a legitimate "nothing on sale right now" state, mapped to
    outcome OK with zero discovered items).
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    products = payload.get("products")
    if not isinstance(products, list):
        return None

    discovered: list[DiscoveredItem] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        handle = product.get("handle")
        if not isinstance(handle, str) or not handle:
            continue
        variants = [v for v in (product.get("variants") or []) if isinstance(v, dict)]
        priced: list[tuple[int, int | None, bool]] = []
        for variant in variants:
            price_cents = _price_string_to_cents(variant.get("price"))
            if price_cents is None:
                continue
            compare_cents = _price_string_to_cents(variant.get("compare_at_price"))
            priced.append((price_cents, compare_cents, bool(variant.get("available"))))
        if not priced:
            continue

        # Representative price for this grid row: cheapest AVAILABLE
        # variant when any exist, else cheapest overall -- confirming the
        # real per-variant price/availability happens on the product page.
        available_priced = [p for p in priced if p[2]] or priced
        price_cents, compare_cents, _ = min(available_priced, key=lambda p: p[0])
        price_is_range = len({p[0] for p in priced}) > 1

        claimed_cents = (
            compare_cents if compare_cents is not None and compare_cents > price_cents else None
        )
        claimed_kind = ClaimedReferenceKind.COMPARE_AT if claimed_cents is not None else None

        # Built from our own domain literal (canonical_host, a constructor
        # constant), but the handle is still untrusted page content --
        # route through the same sanitizing guard rather than assuming
        # string interpolation alone is safe (H1), matching jandh.py.
        candidate_url = f"https://{canonical_host}/products/{handle}"
        url = sanitize_offer_url(candidate_url, allowed_hosts=allowed_hosts)
        if not url:
            continue

        product_id = product.get("id")
        discovered.append(
            DiscoveredItem(
                product_url=url,
                retailer_product_code=str(product_id) if product_id is not None else None,
                title_raw=str(product.get("title") or ""),
                price_cents=price_cents,
                price_is_range=price_is_range,
                claimed_reference_cents=claimed_cents,
                claimed_reference_kind=claimed_kind,
                condition_hint=None,
                category_hint=category_hint,
            )
        )
    return discovered


def _option_names(product: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for option in product.get("options") or []:
        if isinstance(option, dict) and isinstance(option.get("name"), str):
            names.append(option["name"])
    return names


def _attributes_from_options(option_names: Sequence[str], variant: dict[str, Any]) -> dict[str, str]:
    attrs: dict[str, str] = {}
    for i, name in enumerate(option_names, start=1):
        value = variant.get(f"option{i}")
        if value:
            attrs[name.lower()] = value
    return attrs


def parse_product_js(
    body: bytes,
    *,
    product_url: str,
    seller_key: str,
    seller_name: str,
) -> list[ParsedListing] | None:
    """Parse a `/products/<handle>.js` response into one `ParsedListing`
    per variant. Returns None when the body cannot be interpreted as this
    endpoint's JSON shape at all, or has no variants -- caller maps that
    to STRUCTURE_CHANGED/EMPTY."""
    try:
        product = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(product, dict):
        return None
    variants = [v for v in (product.get("variants") or []) if isinstance(v, dict)]
    if not variants:
        return None

    vendor = product.get("vendor")
    brand_raw = vendor.strip() if isinstance(vendor, str) and vendor.strip() else None
    title_raw = str(product.get("title") or "")
    product_id = product.get("id")
    product_code = str(product_id) if product_id is not None else None
    option_names = _option_names(product)

    listings: list[ParsedListing] = []
    for variant in variants:
        sku = str(variant.get("sku") or "")
        price_cents = _cents_field_to_int(variant.get("price"))
        compare_cents = _cents_field_to_int(variant.get("compare_at_price"))
        claimed_cents = (
            compare_cents
            if price_cents is not None and compare_cents is not None and compare_cents > price_cents
            else None
        )
        claimed_kind = ClaimedReferenceKind.COMPARE_AT if claimed_cents is not None else None
        available = bool(variant.get("available"))
        variant_title = str(variant.get("public_title") or variant.get("title") or "")
        gtin_raw = find_barcodes(variant)
        unit_count = parse_pack_count(variant_title) or parse_pack_count(title_raw)

        offer = ParsedOffer(
            offer_key=f"{seller_key}|{Condition.NEW.value}",
            condition=Condition.NEW,
            condition_raw=None,
            seller_key=seller_key,
            seller_name=seller_name,
            seller_type=SellerType.FIRST_PARTY,
            price_cents=price_cents,
            shipping_cents=None,
            claimed_reference_cents=claimed_cents,
            claimed_reference_kind=claimed_kind,
            on_clearance=claimed_cents is not None,
            availability=Availability.IN_STOCK if available else Availability.OUT_OF_STOCK,
            stock_qty=None,
            stock_qty_is_floor=False,
            restock_date=None,
            unit_count=unit_count,
        )
        listings.append(
            ParsedListing(
                retailer_sku=sku,
                retailer_product_code=product_code,
                url=product_url,
                title_raw=title_raw,
                brand_raw=brand_raw,
                model_raw=None,
                variant_label_raw=variant_title,
                attributes_raw=_attributes_from_options(option_names, variant),
                gtin_raw=gtin_raw,
                mpn_raw=None,
                offers=(offer,),
                source="api_json",
            )
        )
    return listings


class ShopifyCollectionAdapter:
    """RetailerAdapter for a Shopify storefront read via its own public
    JSON endpoints. One instance per retailer -- see fishingonline.py,
    discounttackle.py, rodlocker.py for the thin per-retailer modules that
    construct this with their own slug/name/base_url/sale collection
    handle."""

    adapter_version = "1"

    def __init__(
        self,
        *,
        slug: str,
        name: str,
        base_url: str,
        sale_collection_handle: str,
    ) -> None:
        self.slug = slug
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.sale_collection_handle = sale_collection_handle
        self.canonical_host = (urlsplit(self.base_url).hostname or "").lower()
        # H1: allowed hosts for this retailer's listing/discovered-item
        # URLs, loaded once at construction time -- config/retailers.yaml
        # is a static file checked into the image, not something that
        # changes mid-process (matches academy.py/jandh.py's module-level
        # load; here it's per-instance since slug varies by retailer).
        self.allowed_hosts = load_allowed_hosts(slug)
        self.capabilities = AdapterCapabilities(
            adapter_kind="page_scraper",
            page_types=frozenset({PageType.PRODUCT, PageType.CLEARANCE_LISTING}),
            structured_source="api_json",
            exposes_gtin=True,
            exposes_stock_qty=False,
            exposes_claimed_reference=True,
            exposes_used_offers=False,
            exposes_multiple_sellers=False,
            requires_js=False,
        )

    def discovery_url(self) -> str:
        """The `/collections/<handle>/products.json` URL this retailer's
        `discovery_pages` row should point at (used by the seed migration
        and available here so it is never hand-typed twice)."""
        return f"{self.base_url}/collections/{self.sale_collection_handle}/products.json"

    def build_request(self, task: CrawlTask) -> FetchRequest:
        url = task.url
        if task.page_type == PageType.PRODUCT:
            # See module docstring: the tracked/enrolled URL is the
            # canonical human-visible product page; the actual fetch goes
            # to Shopify's own `.js` variant of that same URL.
            url = f"{task.url.rstrip('/')}.js"
        return FetchRequest(
            task_id=task.id,
            page_type=task.page_type,
            url=url,
            params=dict(task.params),
            render_js=False,
        )

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]:
        if page_type == PageType.CLEARANCE_LISTING:
            return _COLLECTION_SENTINELS
        return _PRODUCT_SENTINELS

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

        page_type = response.request.page_type
        sentinels = self.content_sentinels(page_type)
        has_sentinel = any(sentinel in body for sentinel in sentinels)
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

        if page_type == PageType.CLEARANCE_LISTING:
            category_hint = response.request.params.get("category_hint", "other")
            discovered = parse_collection_products_json(
                body,
                category_hint=category_hint,
                canonical_host=self.canonical_host,
                allowed_hosts=self.allowed_hosts,
            )
            if discovered is None:
                return ParseResult(
                    outcome=ResponseOutcome.STRUCTURE_CHANGED,
                    listings=(),
                    discovered=(),
                    next_page_url=None,
                    warnings=("products.json sentinel found but body was not the expected shape",),
                    block_signature=None,
                )
            return ParseResult(
                outcome=ResponseOutcome.OK,
                listings=(),
                discovered=tuple(discovered),
                # Shopify's `products.json` supports `?page=N` pagination,
                # but no discovery page configured for these retailers
                # requests more than the default page -- see backend-coder
                # HANDOFF, uncertainty MEDIUM.
                next_page_url=None,
                warnings=(),
                block_signature=None,
            )

        # PRODUCT (confirm) page: response.request.url is the `.js`
        # fetch target built in build_request; recover the canonical
        # product page URL by stripping that same suffix back off before
        # it is ever persisted/published (H1) -- see module docstring.
        fetched_url = response.final_url or response.request.url
        canonical_url = fetched_url[: -len(".js")] if fetched_url.endswith(".js") else fetched_url
        product_url = sanitize_offer_url(canonical_url, allowed_hosts=self.allowed_hosts) or ""

        listings = parse_product_js(
            body,
            product_url=product_url,
            seller_key=self.slug,
            seller_name=self.name,
        )
        if not listings:
            outcome = (
                ResponseOutcome.STRUCTURE_CHANGED if listings is None else ResponseOutcome.EMPTY
            )
            return ParseResult(
                outcome=outcome,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("product .js sentinel found but yielded no listings",),
                block_signature=None,
            )
        return ParseResult(
            outcome=ResponseOutcome.OK,
            listings=tuple(listings),
            discovered=(),
            next_page_url=None,
            warnings=(),
            block_signature=None,
        )
