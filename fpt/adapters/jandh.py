"""J&H Tackle (jandh.com) adapter.

Architecture doc references: section 3.1 (adapter contract, MVP adapter
matrix -- `jandh` row: "Reference anchor for cross-retailer verification",
no claimed reference, no used offers), section 8 (product matching -- J&H's
per-variant UPC-A is "the matching anchor").

Shopify storefront. Two page shapes used here (verified live, 2026-09-12):

- PRODUCT: the page embeds BOTH a `Product` JSON-LD block (with an
  `AggregateOffer` giving only a min/max price range across all variants,
  plus per-model barcodes under `additionalProperty[].valueReference`) AND
  a `<script id="ProductJson-...">` blob -- Shopify's own per-variant data
  (`variants[]`: sku, price in *cents already*, barcode, title, available).
  `ProductJson` is authoritative for price/availability per variant; the
  JSON-LD `additionalProperty` barcodes are used only to fill in a barcode
  when a variant's own `ProductJson` barcode is empty (observed to happen
  on some SKUs).
- CATALOG_LISTING: a `.product-card-wrapper` grid, no JSON-LD `ItemList`
  found on the sampled collection page. Each card carries one visible price
  and a `<span class="fromtag">From</span>` marker when the card price is a
  "starting at" price for a multi-variant product (as opposed to the
  product's only price) -- mapped to `DiscoveredItem.price_is_range`.

Per-piece vs "FINAL PRICE" (mentioned in
knowledge/research/fishing-gear-retailer-coverage-2026-09-12.md): on
multi-pack terminal-tackle pages, J&H's theme shows a per-piece-styled
`.jh-main-price` next to literal text "/piece" and a separate sticky
`.jh-final-price` ("FINAL PRICE"). Both were observed to equal the SAME
number as the selected variant's `ProductJson` price (i.e. "/piece" here
means "per selected variant", not "per physical item in the pack") -- see
handoff uncertainty. This adapter does not parse those two DOM fields at
all: `ProductJson.variants[].price` is already the correct, authoritative
per-variant price, and `unit_count` is derived from the variant's own
title/option text (e.g. "5 Pack", "25 Pack") via `parse_pack_count`.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from fpt.adapters._shared import (
    extract_jsonld_objects,
    extract_product_json,
    extract_title,
    find_barcodes,
    is_blocked,
    load_allowed_hosts,
    parse_pack_count,
    sanitize_offer_url,
)
from fpt.core.models import (
    Availability,
    AdapterCapabilities,
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

SELLER_KEY = "jandh"
SELLER_NAME = "J&H Tackle"

# H1: allowed hosts for this retailer's listing/discovered-item URLs.
ALLOWED_HOSTS = load_allowed_hosts("jandh")

_CARD_START_RE = re.compile(r'<div class="product-card-wrapper"')
_CARD_HREF_RE = re.compile(r'<a class="product-card" href="([^"]+)"')
_CARD_TITLE_RE = re.compile(
    r'<h2 class="product-title">\s*<span>(.*?)</span>', re.DOTALL
)
_CARD_PRICE_RE = re.compile(
    r'<span class="current-price[^"]*">\s*\$([0-9,]+\.\d{2})\s*</span>', re.DOTALL
)
_CARD_FROMTAG_RE = re.compile(r'<span class="fromtag">')


def _price_str_to_cents(price_str: str) -> int | None:
    try:
        return round(float(price_str.replace(",", "")) * 100)
    except ValueError:
        return None


def _identifier_barcode_map(objects: list[Any]) -> dict[str, str]:
    """Map JSON-LD `additionalProperty[].identifier` -> barcode value.

    J&H's `Product` JSON-LD ties a barcode to a model identifier (e.g.
    `DMSJB62MLS`) via `additionalProperty[].valueReference`, which lines up
    with `ProductJson.variants[].sku` on every sample fetched.
    """
    mapping: dict[str, str] = {}
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("@type") != "Product":
            continue
        for prop in obj.get("additionalProperty") or []:
            if not isinstance(prop, dict):
                continue
            identifier = prop.get("identifier")
            ref = prop.get("valueReference")
            if not identifier or not isinstance(ref, dict):
                continue
            value = ref.get("value")
            if isinstance(value, str) and value.strip():
                mapping[identifier] = value.strip()
    return mapping


def _jsonld_brand(objects: list[Any]) -> str | None:
    for obj in objects:
        if isinstance(obj, dict) and obj.get("@type") == "Product":
            brand = obj.get("brand")
            if isinstance(brand, dict) and brand.get("name"):
                return brand["name"]
    return None


def _build_offer(
    variant: dict[str, Any], unit_count: int | None
) -> ParsedOffer:
    price = variant.get("price")
    price_cents = int(price) if isinstance(price, (int, float)) else None
    available = bool(variant.get("available"))
    return ParsedOffer(
        offer_key=f"{SELLER_KEY}|{Condition.NEW.value}",
        condition=Condition.NEW,
        condition_raw=None,
        seller_key=SELLER_KEY,
        seller_name=SELLER_NAME,
        seller_type=SellerType.FIRST_PARTY,
        price_cents=price_cents,
        shipping_cents=None,
        # J&H is a reference anchor only -- capabilities.exposes_claimed_
        # reference is False; never populate a claimed reference even
        # though Shopify's schema natively supports compare_at_price.
        claimed_reference_cents=None,
        claimed_reference_kind=None,
        on_clearance=False,
        availability=Availability.IN_STOCK if available else Availability.OUT_OF_STOCK,
        stock_qty=None,
        stock_qty_is_floor=False,
        restock_date=None,
        unit_count=unit_count,
    )


def _attributes_from_options(
    product_json: dict[str, Any], variant: dict[str, Any]
) -> dict[str, str]:
    option_names = [
        name for name in (product_json.get("options") or []) if isinstance(name, str)
    ]
    attrs: dict[str, str] = {}
    for i, name in enumerate(option_names, start=1):
        value = variant.get(f"option{i}")
        if value:
            attrs[name.lower()] = value
    return attrs


def _parse_product_page(body: bytes, fallback_url: str | None = None) -> list[ParsedListing]:
    product_json = extract_product_json(body)
    jsonld_objects = extract_jsonld_objects(body)

    if product_json is None:
        # Fall back to JSON-LD only: one listing per barcode-bearing
        # additionalProperty entry, priced at the AggregateOffer's
        # lowPrice (best available signal when ProductJson is missing).
        listings: list[ParsedListing] = []
        for obj in jsonld_objects:
            if not isinstance(obj, dict) or obj.get("@type") != "Product":
                continue
            offers = obj.get("offers") or {}
            price = offers.get("lowPrice") if isinstance(offers, dict) else offers.get("price")
            price_cents = _price_str_to_cents(str(price)) if price is not None else None
            name = obj.get("name", "")
            brand = (obj.get("brand") or {}).get("name")
            props = obj.get("additionalProperty") or []
            product_url = sanitize_offer_url(obj.get("url"), allowed_hosts=ALLOWED_HOSTS, fallback_url=fallback_url) or ""
            if not props:
                # Single-SKU product with no variant breakdown at all.
                listings.append(
                    ParsedListing(
                        retailer_sku=str(obj.get("sku") or ""),
                        retailer_product_code=None,
                        url=product_url,
                        title_raw=name,
                        brand_raw=brand,
                        model_raw=None,
                        variant_label_raw=name,
                        attributes_raw={},
                        gtin_raw=find_barcodes(obj),
                        mpn_raw=None,
                        offers=[
                            ParsedOffer(
                                offer_key=f"{SELLER_KEY}|{Condition.NEW.value}",
                                condition=Condition.NEW,
                                condition_raw=None,
                                seller_key=SELLER_KEY,
                                seller_name=SELLER_NAME,
                                seller_type=SellerType.FIRST_PARTY,
                                price_cents=price_cents,
                                shipping_cents=None,
                                claimed_reference_cents=None,
                                claimed_reference_kind=None,
                                on_clearance=False,
                                availability=Availability.UNKNOWN,
                                stock_qty=None,
                                stock_qty_is_floor=False,
                                restock_date=None,
                                unit_count=parse_pack_count(name),
                            )
                        ],
                        source="jsonld",
                    )
                )
                continue
            for prop in props:
                if not isinstance(prop, dict):
                    continue
                identifier = str(prop.get("identifier") or "")
                ref = prop.get("valueReference") or {}
                barcode = ref.get("value") if isinstance(ref, dict) else None
                label = prop.get("value") or prop.get("name") or ""
                listings.append(
                    ParsedListing(
                        retailer_sku=identifier,
                        retailer_product_code=str(obj.get("sku") or "") or None,
                        url=product_url,
                        title_raw=name,
                        brand_raw=brand,
                        model_raw=None,
                        variant_label_raw=label,
                        attributes_raw={},
                        gtin_raw=[barcode] if isinstance(barcode, str) and barcode else [],
                        mpn_raw=None,
                        offers=[
                            ParsedOffer(
                                offer_key=f"{SELLER_KEY}|{Condition.NEW.value}",
                                condition=Condition.NEW,
                                condition_raw=None,
                                seller_key=SELLER_KEY,
                                seller_name=SELLER_NAME,
                                seller_type=SellerType.FIRST_PARTY,
                                price_cents=price_cents,
                                shipping_cents=None,
                                claimed_reference_cents=None,
                                claimed_reference_kind=None,
                                on_clearance=False,
                                availability=Availability.UNKNOWN,
                                stock_qty=None,
                                stock_qty_is_floor=False,
                                restock_date=None,
                                unit_count=parse_pack_count(label),
                            )
                        ],
                        source="jsonld",
                    )
                )
        return listings

    barcode_map = _identifier_barcode_map(jsonld_objects)
    brand = product_json.get("vendor") or _jsonld_brand(jsonld_objects)
    product_code = str(product_json.get("id") or "") or None
    product_title = product_json.get("title", "")

    listings = []
    for variant in product_json.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        sku = str(variant.get("sku") or "")
        variant_title = variant.get("title") or variant.get("public_title") or ""
        barcode = variant.get("barcode")
        gtin_raw: list[str] = []
        if isinstance(barcode, str) and barcode.strip():
            gtin_raw.append(barcode.strip())
        elif sku in barcode_map:
            gtin_raw.append(barcode_map[sku])
        unit_count = parse_pack_count(variant_title) or parse_pack_count(product_title)
        # config/retailers.yaml's `allowed_hosts` for jandh is
        # `www.jandh.com` only (matching its `base_url` and the host our
        # own crawler actually requests) -- bare `jandh.com` is not on the
        # allowlist, so build the canonical URL with `www.` to match.
        built_url = f"https://www.jandh.com/products/{product_json.get('handle', '')}?variant={variant.get('id', '')}"
        # Built from our own domain literal, but the handle/variant id are
        # still untrusted page content -- route through the same guard
        # rather than assuming string interpolation alone is safe (H1).
        url = sanitize_offer_url(built_url, allowed_hosts=ALLOWED_HOSTS, fallback_url=fallback_url) or ""
        listings.append(
            ParsedListing(
                retailer_sku=sku,
                retailer_product_code=product_code,
                url=url,
                title_raw=product_title,
                brand_raw=brand,
                model_raw=None,
                variant_label_raw=variant_title,
                attributes_raw=_attributes_from_options(product_json, variant),
                gtin_raw=gtin_raw,
                mpn_raw=None,
                offers=[_build_offer(variant, unit_count)],
                source="jsonld",
            )
        )
    return listings


def _parse_catalog_listing(
    body_text: str, category_hint: str, fallback_url: str | None = None
) -> list[DiscoveredItem]:
    starts = [m.start() for m in _CARD_START_RE.finditer(body_text)]
    discovered: list[DiscoveredItem] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(body_text)
        block = body_text[start:end]

        href_match = _CARD_HREF_RE.search(block)
        title_match = _CARD_TITLE_RE.search(block)
        price_match = _CARD_PRICE_RE.search(block)
        if not href_match or not title_match:
            continue

        price_cents = _price_str_to_cents(price_match.group(1)) if price_match else None
        is_range = bool(_CARD_FROMTAG_RE.search(block))
        href = href_match.group(1)
        candidate_url = href if href.startswith("http") else f"https://www.jandh.com{href}"
        # `href` is untrusted card markup: could be `javascript:`, `data:`,
        # `//evil.com`, `http://`, or an absolute link to another host
        # entirely -- sanitize before treating it as a product URL (H1).
        url = sanitize_offer_url(candidate_url, allowed_hosts=ALLOWED_HOSTS, fallback_url=fallback_url) or ""

        discovered.append(
            DiscoveredItem(
                product_url=url,
                retailer_product_code=None,
                title_raw=re.sub(r"\s+", " ", title_match.group(1)).strip(),
                price_cents=price_cents,
                price_is_range=is_range,
                # Capabilities: exposes_claimed_reference=False for jandh.
                claimed_reference_cents=None,
                claimed_reference_kind=None,
                condition_hint=None,
                category_hint=category_hint,
            )
        )
    return discovered


class JandhAdapter:
    """RetailerAdapter for jandh.com (page_scraper, Shopify JSON + JSON-LD)."""

    slug = "jandh"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        adapter_kind="page_scraper",
        page_types=frozenset({PageType.PRODUCT, PageType.CATALOG_LISTING}),
        structured_source="jsonld",
        exposes_gtin=True,
        exposes_stock_qty=False,
        exposes_claimed_reference=False,
        exposes_used_offers=False,
        exposes_multiple_sellers=False,
        requires_js=False,
    )

    def build_request(self, task: CrawlTask) -> FetchRequest:
        return FetchRequest(
            task_id=task.id,
            page_type=task.page_type,
            url=task.url,
            params=dict(task.params),
            render_js=False,
        )

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]:
        if page_type == PageType.CATALOG_LISTING:
            return (b'product-card-wrapper', b'class="product-price"')
        return (b'id="ProductJson', b'"@type":"Product"', b'"@type": "Product"')

    def parse(self, response: FetchResponse) -> ParseResult:
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
            title = extract_title(body)
            outcome = ResponseOutcome.EMPTY if not title else ResponseOutcome.STRUCTURE_CHANGED
            return ParseResult(
                outcome=outcome,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("no recognizable content sentinels found on page",),
                block_signature=None,
            )

        if page_type == PageType.CATALOG_LISTING:
            body_text = body.decode("utf-8", errors="replace")
            category_hint = response.request.params.get("category_hint", "other")
            discovered = _parse_catalog_listing(body_text, category_hint, fallback_url=response.request.url)
            if not discovered:
                return ParseResult(
                    outcome=ResponseOutcome.STRUCTURE_CHANGED,
                    listings=(),
                    discovered=(),
                    next_page_url=None,
                    warnings=("product-card markup found but yielded no items",),
                    block_signature=None,
                )
            return ParseResult(
                outcome=ResponseOutcome.OK,
                listings=(),
                discovered=tuple(discovered),
                # No pagination control observed on the sampled collection
                # page within this fetch budget; see backend-coder
                # handoff, uncertainty HIGH.
                next_page_url=None,
                warnings=(),
                block_signature=None,
            )

        # PRODUCT page
        listings = _parse_product_page(body, fallback_url=response.request.url)
        if not listings:
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("Product JSON-LD/ProductJson found but yielded no listings",),
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
