"""Core domain enums and dataclasses.

This module is the adapter contract (architecture doc section 3.1). It is
intentionally the first file committed in this package so that other
retailer adapters (Academy, J&H Tackle) can be built against a stable
interface without waiting on the rest of the pipeline.

Rules for adapter authors (see architecture doc 3.1):
- `RetailerAdapter.parse` never raises for a malformed page and never invents
  a value (a missing price is `None`, not 0).
- A grid row from a listing page is a `DiscoveredItem`, never a
  `ParsedListing`/`ParsedOffer` observation directly — it must be confirmed
  on the product page before it can back a deal.
- Pagination is expressed via `ParseResult.next_page_url`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Mapping, Protocol, Sequence


class Condition(StrEnum):
    NEW = "NEW"
    NEW_OPEN_BOX = "NEW_OPEN_BOX"
    REFURBISHED = "REFURBISHED"
    USED_LIKE_NEW = "USED_LIKE_NEW"
    USED_VERY_GOOD = "USED_VERY_GOOD"
    USED_GOOD = "USED_GOOD"
    USED_ACCEPTABLE = "USED_ACCEPTABLE"
    USED_UNGRADED = "USED_UNGRADED"  # used, retailer gives no grade


CONDITION_GROUP: dict[str, str] = {  # baselines never cross groups
    "NEW": "NEW",
    "NEW_OPEN_BOX": "OPEN_BOX",
    "REFURBISHED": "REFURB",
    "USED_LIKE_NEW": "USED",
    "USED_VERY_GOOD": "USED",
    "USED_GOOD": "USED",
    "USED_ACCEPTABLE": "USED",
    "USED_UNGRADED": "USED",
}


class SellerType(StrEnum):
    FIRST_PARTY = "FIRST_PARTY"  # the retailer itself
    RETAILER_RESALE = "RETAILER_RESALE"  # e.g. Amazon Resale, TW used-gear program
    MARKETPLACE_3P = "MARKETPLACE_3P"  # third-party seller on a marketplace


class Availability(StrEnum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    STORE_ONLY = "STORE_ONLY"
    BACKORDER = "BACKORDER"
    UNKNOWN = "UNKNOWN"


class PageType(StrEnum):
    PRODUCT = "PRODUCT"  # one product, all its variants and offers
    CLEARANCE_LISTING = "CLEARANCE_LISTING"  # grid of discounted items (discovery)
    USED_LISTING = "USED_LISTING"  # grid of used items (discovery)
    CATALOG_LISTING = "CATALOG_LISTING"  # ordinary category grid (catalog breadth)
    API_BATCH = "API_BATCH"  # licensed data API request (Phase 2)


class ResponseOutcome(StrEnum):
    OK = "OK"
    BLOCKED = "BLOCKED"
    EMPTY = "EMPTY"
    STRUCTURE_CHANGED = "STRUCTURE_CHANGED"
    NOT_FOUND = "NOT_FOUND"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"  # data API only


class ClaimedReferenceKind(StrEnum):
    WAS = "WAS"
    MSRP = "MSRP"
    LIST = "LIST"
    COMPARE_AT = "COMPARE_AT"


@dataclass(frozen=True)
class AdapterCapabilities:
    adapter_kind: Literal["page_scraper", "data_api"]
    page_types: frozenset[PageType]
    structured_source: Literal["jsonld", "html_text", "api_json"]
    exposes_gtin: bool
    exposes_stock_qty: bool
    exposes_claimed_reference: bool  # TW clearance True; Academy clearance False
    exposes_used_offers: bool
    exposes_multiple_sellers: bool  # Amazon True
    requires_js: bool


@dataclass(frozen=True)
class FetchRequest:
    task_id: int
    page_type: PageType
    url: str  # for API_BATCH: endpoint URL without credentials
    params: Mapping[str, str] = field(default_factory=dict)
    render_js: bool = False
    api_cost_units: int = 0  # estimated quota units (data API only)


@dataclass(frozen=True)
class FetchResponse:
    request: FetchRequest
    status: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes
    elapsed_ms: int
    fetched_at: datetime
    egress_mode: Literal["DIRECT", "PROXY", "API"]
    snapshot_ref: str
    api_units_remaining: int | None = None


@dataclass(frozen=True)
class ParsedOffer:
    offer_key: str  # source offer id, else f"{seller_key}|{condition}"
    condition: Condition
    condition_raw: str | None  # retailer's own wording, kept for audit
    seller_key: str  # "tackle_warehouse", "amazon", "amazon_resale", 3P id
    seller_name: str | None
    seller_type: SellerType
    price_cents: int | None  # item price; None = parse failed
    shipping_cents: int | None  # None = unknown; 0 = free
    claimed_reference_cents: int | None  # retailer "was"/MSRP for THIS offer row
    claimed_reference_kind: ClaimedReferenceKind | None
    on_clearance: bool
    availability: Availability
    stock_qty: int | None
    stock_qty_is_floor: bool  # TW "10+"
    restock_date: date | None
    unit_count: int | None  # pack count the price covers; J&H exposes per-piece AND final


@dataclass(frozen=True)
class ParsedListing:  # one retailer SKU = one variant at that retailer
    retailer_sku: str  # Academy sku, TW Stock #, J&H variant sku, ASIN (Phase 2)
    retailer_product_code: str | None  # TW descpage code, Shopify handle, parent ASIN
    url: str
    title_raw: str
    brand_raw: str | None
    model_raw: str | None
    variant_label_raw: str
    attributes_raw: Mapping[str, str]
    gtin_raw: Sequence[str]  # all barcodes found, field-name agnostic
    mpn_raw: str | None
    offers: Sequence[ParsedOffer]  # at least one when outcome OK
    source: Literal["jsonld", "html_text", "api_json"]


@dataclass(frozen=True)
class DiscoveredItem:  # a row on a clearance/used/catalog grid
    product_url: str  # canonical product page to enroll
    retailer_product_code: str | None
    title_raw: str
    price_cents: int | None
    price_is_range: bool  # "from $X" or grid shows a product-level price
    claimed_reference_cents: int | None
    claimed_reference_kind: ClaimedReferenceKind | None
    condition_hint: Condition | None  # USED_LISTING rows
    category_hint: str


@dataclass(frozen=True)
class ParseResult:
    outcome: ResponseOutcome
    listings: Sequence[ParsedListing]  # PRODUCT and API_BATCH pages
    discovered: Sequence[DiscoveredItem]  # *_LISTING pages
    next_page_url: str | None  # pagination for listing pages, same page_type
    warnings: Sequence[str]
    block_signature: str | None


class CrawlTask(Protocol):
    """Minimal shape adapters need from the scheduler's crawl_tasks row.

    Kept intentionally small (duck-typed) so this module has zero dependency
    on the scheduler/DB layer -- adapters only need enough to build a
    request.
    """

    id: int
    url: str
    page_type: PageType
    params: Mapping[str, str]


class RetailerAdapter(Protocol):
    slug: str
    adapter_version: str  # bump on any parse change; stored on every observation
    capabilities: AdapterCapabilities

    def build_request(self, task: CrawlTask) -> FetchRequest: ...

    def content_sentinels(self, page_type: PageType) -> Sequence[bytes]: ...

    def parse(self, response: FetchResponse) -> ParseResult: ...
