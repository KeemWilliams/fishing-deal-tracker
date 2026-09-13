"""Shared Coveo search-JSON parsing helper for retailers whose listing/search
data loads client-side from a Coveo search API (`/rest/search/v2`) rather
than from the rendered page HTML.

Extracted from `fpt/adapters/basspro.py` while adding the Cabela's adapter
(2026-09-13): Cabela's is confirmed (via a live captured response, see
`tests/fixtures/_capture/cabelas_coveo_rods.json`) to run the exact same
Coveo backend/org as Bass Pro Shops -- identical top-level `results` array
shape, identical `raw` object field names (`offerprice`, `listprice`,
`minofferprice`/`maxofferprice`, `minlistprice`/`maxlistprice`,
`clbofferpriceusd`, `title`, `sku`, `producturlkeyword`). Rather than
copy-pasting `basspro.py`'s parsing function verbatim into a second module,
the shared logic below is parameterized by seller slug/name/base_url/
allowed_hosts and both `basspro.py` and `cabelas.py` now call it. This
carries zero risk to Bass Pro's existing tests: `basspro.py` still exposes
its own `parse_coveo_search_json` name (now a thin wrapper) so
`test_adapter_basspro.py` did not need to change at all.

Everything here is pure (no I/O) and never raises on malformed input, per
the adapter contract ("`parse` never raises for a malformed page").
"""

from __future__ import annotations

import json
from typing import Any

from fpt.adapters._shared import sanitize_offer_url
from fpt.core.models import ClaimedReferenceKind, DiscoveredItem

# Coveo's search-response envelope. Deliberately a byte-substring check
# (matching the rest of this package's `is_blocked`/`content_sentinels`
# convention) rather than a speculative json.loads -- a truncated or
# block-page body should hit `is_blocked`/EMPTY/STRUCTURE_CHANGED via the
# normal parse path, not raise here.
RESULTS_SENTINELS: tuple[bytes, ...] = (b'"results":[', b'"results": [')


def dollars_to_cents(value: Any) -> int | None:
    """Coveo prices are USD dollar amounts (float or int), e.g. 49.99 or 50.
    Never guesses -- a missing/non-numeric/non-positive value is None, per
    the adapter contract ("a missing price is None, not 0")."""
    if isinstance(value, bool):  # bool is an int subclass; exclude explicitly
        return None
    if not isinstance(value, (int, float)):
        return None
    cents = round(value * 100)
    return cents if cents > 0 else None


def parse_coveo_search_json(
    body: bytes,
    *,
    category_hint: str,
    allowed_hosts: frozenset[str],
    base_url: str,
    product_path_prefix: str = "/p/",
) -> list[DiscoveredItem] | None:
    """Parse a Coveo `/rest/search/v2` response body into discovery rows.

    Returns None when the body cannot be interpreted as this endpoint's
    JSON shape at all (caller maps that to STRUCTURE_CHANGED); returns an
    empty list for a validly-shaped but empty result set (a legitimate "no
    matches" response, mapped to outcome OK with zero discovered items --
    matching `_shopify_collection.parse_collection_products_json`'s
    empty-vs-unparseable distinction).

    `base_url`/`product_path_prefix` let each retailer using this Coveo
    backend build its own canonical product URL from the shared
    `producturlkeyword` field (Bass Pro and Cabela's both serve product
    pages under `/p/<keyword>`, confirmed live for both).
    """
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None

    discovered: list[DiscoveredItem] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        raw = result.get("raw")
        if not isinstance(raw, dict):
            continue

        url_keyword = raw.get("producturlkeyword")
        if not isinstance(url_keyword, str) or not url_keyword:
            continue

        price_cents = dollars_to_cents(raw.get("offerprice"))
        if price_cents is None:
            continue
        list_cents = dollars_to_cents(raw.get("listprice"))

        # Never treat offerprice == listprice as a discount, even when both
        # are present -- only a strictly higher listprice is a real "was"
        # claim (matches _shopify_collection.py's compare_at_price > price
        # guard and jandh.py's "never fabricate a discount" rule).
        claimed_cents = list_cents if list_cents is not None and list_cents > price_cents else None
        claimed_kind = ClaimedReferenceKind.LIST if claimed_cents is not None else None

        min_offer = dollars_to_cents(raw.get("minofferprice"))
        max_offer = dollars_to_cents(raw.get("maxofferprice"))
        # price_is_range: the grid/search-row price is a product-level
        # figure, not confirmed per-variant, whenever this product's
        # variants (childcount) don't all share one offer price -- same
        # "grid shows one price, real per-variant prices are confirmed on
        # the product page" convention as academy.py/_shopify_collection.py.
        price_is_range = min_offer is not None and max_offer is not None and min_offer != max_offer

        # Untrusted page content (the URL keyword) -- route through the
        # same sanitizing guard rather than assuming string interpolation
        # alone is safe (security review H1), matching jandh.py/
        # _shopify_collection.py/basspro.py.
        candidate_url = f"{base_url}{product_path_prefix}{url_keyword}"
        url = sanitize_offer_url(candidate_url, allowed_hosts=allowed_hosts)
        if not url:
            continue

        sku = raw.get("sku")
        discovered.append(
            DiscoveredItem(
                product_url=url,
                retailer_product_code=str(sku) if sku is not None else None,
                title_raw=str(raw.get("title") or ""),
                price_cents=price_cents,
                price_is_range=price_is_range,
                claimed_reference_cents=claimed_cents,
                claimed_reference_kind=claimed_kind,
                condition_hint=None,
                category_hint=category_hint,
            )
        )
    return discovered
