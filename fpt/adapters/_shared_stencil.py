"""Shared parsing helpers for BigCommerce Stencil `productView` product pages.

TackleDirect (tackledirect.com) and alltackle.com are both BigCommerce Stencil
storefronts using the same default "productView" template (verified live,
2026-09-12, single-SKU product pages on each site): a
`dl.productView-info` block carrying `dd[data-product-sku]` and, when the
product has a barcode on file, `dd[data-product-upc]`; a
`span[data-product-price-without-tax]` current price; `rrp`/`non-sale` price
spans for MSRP/"was" pricing; a hidden `product_id` form input; and a
`meta[property="og:availability"]` tag. Field/section markup is identical
byte-for-byte between the two retailers -- only the domain and a few brand
labels differ.

Kept adapter-scoped (leading underscore) like `_shared.py` (the jsonld/
Academy+J&H helpers) -- this is NOT the core fetch/normalizer layer, it only
avoids duplicating the same regex-based scraping logic between
`tackledirect.py` and `alltackle.py`.

Everything here is pure (no I/O) and never raises on malformed input, per the
adapter contract ("`parse` never raises for a malformed page").
"""

from __future__ import annotations

import re

from fpt.core.models import Availability, ClaimedReferenceKind
from fpt.core.money import parse_price_cents

# Values observed live in `<meta property="og:availability" content="...">`:
# TackleDirect uses "instock"; alltackle.com uses "oos" for an out-of-stock
# item (verified live, 2026-09-12) -- both retailers share the same Stencil
# theme family but the exact string BigCommerce renders here is themeable
# per store, so this map is intentionally generous with likely variants
# rather than trusting a single spelling.
_OG_AVAILABILITY_MAP: dict[str, Availability] = {
    "instock": Availability.IN_STOCK,
    "in-stock": Availability.IN_STOCK,
    "in stock": Availability.IN_STOCK,
    "oos": Availability.OUT_OF_STOCK,
    "outofstock": Availability.OUT_OF_STOCK,
    "out-of-stock": Availability.OUT_OF_STOCK,
    "out of stock": Availability.OUT_OF_STOCK,
    "preorder": Availability.BACKORDER,
    "pre-order": Availability.BACKORDER,
    "backorder": Availability.BACKORDER,
    "back-order": Availability.BACKORDER,
}

# `data-product-price-without-tax` is a boolean-style custom attribute (no
# `="..."` value) immediately followed by `class="price price--withoutTax"`
# on both retailers -- confirmed on every sampled page, single-SKU and
# multi-variant alike.
_PRICE_SPAN_RE = re.compile(
    r'data-product-price-without-tax\s+class="price price--withoutTax"[^>]*>([^<]*)<'
)
_RRP_SPAN_RE = re.compile(
    r'data-product-rrp-price-without-tax\s+class="price price--rrp">\s*([^<]*)<'
)
_NON_SALE_SPAN_RE = re.compile(
    r'data-product-non-sale-price-without-tax\s+class="price price--non-sale">\s*([^<]*)<'
)
_SKU_RE = re.compile(r"data-product-sku>([^<]*)<")
_UPC_RE = re.compile(r"data-product-upc>([^<]*)<")
_PRODUCT_ID_RE = re.compile(r'name="product_id" value="(\d+)"')
_OG_AVAILABILITY_META_RE = re.compile(r'og:availability"\s+content="([^"]*)"')
_TITLE_RE = re.compile(r'<h1[^>]*class="productView-title"[^>]*>(.*?)</h1>', re.DOTALL)
_BRAND_RE = re.compile(
    r'class="productView-brand"[^>]*>\s*<a[^>]*>\s*<span>(.*?)</span>', re.DOTALL
)


def map_og_availability(raw: str | None) -> Availability:
    if not raw:
        return Availability.UNKNOWN
    return _OG_AVAILABILITY_MAP.get(raw.strip().lower(), Availability.UNKNOWN)


def extract_current_price_cents(html_text: str) -> int | None:
    """The product's current/selling price, or None if it isn't populated in
    the static HTML -- the reliable signal that a multi-variant "series" page
    needs a JS variant selection before a real price exists (see
    `tackledirect.py` module docstring)."""
    match = _PRICE_SPAN_RE.search(html_text)
    if not match:
        return None
    return parse_price_cents(match.group(1))


def extract_claimed_reference(
    html_text: str,
) -> tuple[int | None, ClaimedReferenceKind | None]:
    """MSRP takes priority over a retailer "was" price when both happen to be
    populated (not observed live, but MSRP is the stronger/more specific
    claim of the two `ClaimedReferenceKind` values available here)."""
    rrp_match = _RRP_SPAN_RE.search(html_text)
    rrp_cents = parse_price_cents(rrp_match.group(1)) if rrp_match else None
    if rrp_cents is not None:
        return rrp_cents, ClaimedReferenceKind.MSRP
    was_match = _NON_SALE_SPAN_RE.search(html_text)
    was_cents = parse_price_cents(was_match.group(1)) if was_match else None
    if was_cents is not None:
        return was_cents, ClaimedReferenceKind.WAS
    return None, None


def extract_sku(html_text: str) -> str | None:
    match = _SKU_RE.search(html_text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def extract_upc(html_text: str) -> str | None:
    match = _UPC_RE.search(html_text)
    if not match:
        return None
    value = match.group(1).strip()
    return value or None


def extract_product_id(html_text: str) -> str | None:
    match = _PRODUCT_ID_RE.search(html_text)
    return match.group(1) if match else None


def extract_og_availability_raw(html_text: str) -> str | None:
    match = _OG_AVAILABILITY_META_RE.search(html_text)
    return match.group(1) if match else None


def extract_title(html_text: str) -> str:
    match = _TITLE_RE.search(html_text)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def extract_brand(html_text: str) -> str | None:
    match = _BRAND_RE.search(html_text)
    if not match:
        return None
    return match.group(1).strip() or None
