"""Academy Sports + Outdoors adapter.

Architecture doc references: section 3.1 (adapter contract, MVP adapter
matrix -- `academy` row), section 6.2 (block/quality signatures), section
4.2 (GTIN is field-name agnostic).

Structured source: JSON-LD only, no JS rendering required (verified live,
2026-09-12 -- see knowledge/research/fishing-gear-price-tracker-scrape-
feasibility-2026-09-12.md). Two page shapes:

- PRODUCT: a single `Product` (one SKU, e.g. a reel) or a `ProductGroup`
  with `hasVariant: [Product, ...]` (color variants, e.g. soft baits). Each
  variant/SKU becomes its own `ParsedListing` -- one retailer SKU = one
  variant, per the data model (listings table).
- CLEARANCE_LISTING: an `ItemList` of `ListItem -> Product` rows, each with
  one product-level `Offer`. Per capabilities, Academy's clearance grid
  carries NO claimed reference (no was/MSRP shown on the grid itself) and
  the price shown is at the product level, not per-variant -- so every row
  is emitted as `price_is_range=True` (the variant-mismatch guard in
  section 3.1: "a clearance grid often shows a product-level ... price").

Capabilities note vs. capabilities matrix (section 3.1): `exposes_stock_qty`
is False here -- Academy's JSON-LD only ever exposes a binary
`availability` (InStock / InStoreOnly / OutOfStock), never a numeric count,
confirmed on both sampled fixtures.
"""

from __future__ import annotations

from typing import Any, Sequence

from fpt.adapters._shared import (
    extract_jsonld_objects,
    extract_title,
    find_barcodes,
    is_blocked,
    parse_pack_count,
)
from fpt.core.models import (
    Availability,
    AdapterCapabilities,
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

SELLER_KEY = "academy"
SELLER_NAME = "Academy Sports + Outdoors"

_AVAILABILITY_MAP = {
    "instock": Availability.IN_STOCK,
    "instoreonly": Availability.STORE_ONLY,
    "outofstock": Availability.OUT_OF_STOCK,
    "preorder": Availability.BACKORDER,
    "backorder": Availability.BACKORDER,
    "soldout": Availability.OUT_OF_STOCK,
    "discontinued": Availability.OUT_OF_STOCK,
}


def _map_availability(raw: str | None) -> Availability:
    if not raw:
        return Availability.UNKNOWN
    key = raw.rsplit("/", 1)[-1].strip().lower()
    return _AVAILABILITY_MAP.get(key, Availability.UNKNOWN)


def _map_condition(raw: str | None) -> tuple[Condition, str | None]:
    if raw and raw.rsplit("/", 1)[-1].strip().lower() == "usedcondition":
        return Condition.USED_UNGRADED, raw
    return Condition.NEW, raw


def _price_cents(offer: dict[str, Any]) -> int | None:
    price = offer.get("price")
    if price is None:
        return None
    try:
        return round(float(price) * 100)
    except (TypeError, ValueError):
        return None


def _build_offer(offer_obj: dict[str, Any], unit_count: int | None) -> ParsedOffer:
    condition, condition_raw = _map_condition(offer_obj.get("itemCondition"))
    return ParsedOffer(
        offer_key=f"{SELLER_KEY}|{condition.value}",
        condition=condition,
        condition_raw=condition_raw,
        seller_key=SELLER_KEY,
        seller_name=SELLER_NAME,
        seller_type=SellerType.FIRST_PARTY,
        price_cents=_price_cents(offer_obj),
        shipping_cents=None,
        # Academy's product-page JSON-LD never carries a was/MSRP claim
        # (capabilities: exposes_claimed_reference=False) -- confirmed on
        # both sampled fixtures.
        claimed_reference_cents=None,
        claimed_reference_kind=None,
        on_clearance=False,
        availability=_map_availability(offer_obj.get("availability")),
        stock_qty=None,
        stock_qty_is_floor=False,
        restock_date=None,
        unit_count=unit_count,
    )


def _parse_single_product(product: dict[str, Any]) -> ParsedListing:
    name = product.get("name", "")
    brand = (product.get("brand") or {}).get("name")
    sku = str(product.get("sku") or "")
    offers_raw = product.get("offers")
    offer_objs = offers_raw if isinstance(offers_raw, list) else [offers_raw] if offers_raw else []
    unit_count = parse_pack_count(name)
    offers = [_build_offer(o, unit_count) for o in offer_objs if isinstance(o, dict)]
    return ParsedListing(
        retailer_sku=sku,
        retailer_product_code=sku or None,
        url=product.get("url") or (offer_objs[0].get("url") if offer_objs else "") or "",
        title_raw=name,
        brand_raw=brand,
        model_raw=None,
        variant_label_raw=name,
        attributes_raw={},
        gtin_raw=find_barcodes(product),
        mpn_raw=product.get("mpn"),
        offers=offers,
        source="jsonld",
    )


def _parse_product_group(group: dict[str, Any]) -> list[ParsedListing]:
    name = group.get("name", "")
    brand = (group.get("brand") or {}).get("name")
    unit_count = parse_pack_count(name)
    listings: list[ParsedListing] = []
    for variant in group.get("hasVariant") or []:
        if not isinstance(variant, dict):
            continue
        offer_obj = variant.get("offers")
        offer_objs = [offer_obj] if isinstance(offer_obj, dict) else []
        color = variant.get("color")
        listings.append(
            ParsedListing(
                retailer_sku=str(variant.get("sku") or ""),
                retailer_product_code=str(group.get("productGroupID") or "") or None,
                url=(offer_obj or {}).get("url") or group.get("url") or "",
                title_raw=variant.get("name") or name,
                brand_raw=brand,
                model_raw=None,
                variant_label_raw=color or variant.get("name") or "",
                attributes_raw={"color": color} if color else {},
                gtin_raw=find_barcodes(variant),
                mpn_raw=None,
                offers=[_build_offer(o, unit_count) for o in offer_objs],
                source="jsonld",
            )
        )
    return listings


def _parse_product_page(objects: list[Any]) -> list[ParsedListing]:
    listings: list[ParsedListing] = []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj_type = obj.get("@type")
        if obj_type == "ProductGroup":
            listings.extend(_parse_product_group(obj))
        elif obj_type == "Product":
            listings.append(_parse_single_product(obj))
    return listings


def _parse_clearance_listing(
    objects: list[Any], category_hint: str
) -> list[DiscoveredItem]:
    discovered: list[DiscoveredItem] = []
    for obj in objects:
        if not isinstance(obj, dict) or obj.get("@type") != "ItemList":
            continue
        for entry in obj.get("itemListElement") or []:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            if not isinstance(item, dict):
                continue
            offer = item.get("offers") if isinstance(item.get("offers"), dict) else {}
            discovered.append(
                DiscoveredItem(
                    product_url=item.get("url") or "",
                    retailer_product_code=item.get("mpn"),
                    title_raw=item.get("name") or "",
                    price_cents=_price_cents(offer),
                    # ItemList carries one Offer per product, not per
                    # variant -- see module docstring.
                    price_is_range=True,
                    claimed_reference_cents=None,
                    claimed_reference_kind=None,
                    condition_hint=None,
                    category_hint=category_hint,
                )
            )
    return discovered


class AcademyAdapter:
    """RetailerAdapter for academy.com (page_scraper, JSON-LD)."""

    slug = "academy"
    adapter_version = "1"
    capabilities = AdapterCapabilities(
        adapter_kind="page_scraper",
        page_types=frozenset({PageType.PRODUCT, PageType.CLEARANCE_LISTING}),
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
        if page_type == PageType.CLEARANCE_LISTING:
            return (b'"@type":"ItemList"', b'"@type": "ItemList"')
        return (
            b'"@type":"Product"',
            b'"@type": "Product"',
            b'"@type":"ProductGroup"',
            b'"@type": "ProductGroup"',
        )

    def parse(self, response: FetchResponse) -> ParseResult:
        body = response.body
        warnings: list[str] = []

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

        objects = extract_jsonld_objects(body)
        sentinels = self.content_sentinels(response.request.page_type)
        has_sentinel = any(sentinel in body for sentinel in sentinels)

        if not objects and not has_sentinel:
            title = extract_title(body)
            outcome = ResponseOutcome.EMPTY if not title else ResponseOutcome.STRUCTURE_CHANGED
            return ParseResult(
                outcome=outcome,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=("no JSON-LD objects found on page",),
                block_signature=None,
            )

        if response.request.page_type == PageType.CLEARANCE_LISTING:
            category_hint = response.request.params.get("category_hint", "other")
            discovered = _parse_clearance_listing(objects, category_hint)
            if not discovered:
                warnings.append("ItemList JSON-LD found but yielded no items")
                return ParseResult(
                    outcome=ResponseOutcome.STRUCTURE_CHANGED,
                    listings=(),
                    discovered=(),
                    next_page_url=None,
                    warnings=tuple(warnings),
                    block_signature=None,
                )
            return ParseResult(
                outcome=ResponseOutcome.OK,
                listings=(),
                discovered=tuple(discovered),
                # Pagination link was not observed on the sampled clearance
                # page (single page, no rel="next"); see backend-coder
                # handoff, uncertainty HIGH.
                next_page_url=None,
                warnings=tuple(warnings),
                block_signature=None,
            )

        # PRODUCT page
        listings = _parse_product_page(objects)
        if not listings:
            warnings.append("Product/ProductGroup JSON-LD found but yielded no listings")
            return ParseResult(
                outcome=ResponseOutcome.STRUCTURE_CHANGED,
                listings=(),
                discovered=(),
                next_page_url=None,
                warnings=tuple(warnings),
                block_signature=None,
            )
        return ParseResult(
            outcome=ResponseOutcome.OK,
            listings=tuple(listings),
            discovered=(),
            next_page_url=None,
            warnings=tuple(warnings),
            block_signature=None,
        )
