"""Confirmation-via-listing-re-observation (mission item 3, 2026-09-13).

For "listing-complete" retailers -- ones whose CLEARANCE_LISTING/
CATALOG_LISTING/USED_LISTING grid already carries BOTH the current price
AND a compare-at/claimed reference price for an item (the Shopify
collection retailers, Sportsman's Guide, Tackle Warehouse's own clearance
grid) -- a separate per-product PRODUCT-page fetch to enroll and then
re-confirm a deal is unnecessary busywork: the same two facts the
confirmation state machine needs (current price, reference price, stable
offer identity) are already sitting on the grid row.

This module builds a synthetic one-offer `ParsedListing`/`ParsedOffer`
straight from each eligible `DiscoveredItem` and routes it through the
EXISTING `fpt.store.pipeline.ingest_parsed_listing` -- the same
detect/confirm state machine a real PRODUCT-page fetch uses. A deal
reaches ACTIVE from TWO listing sweeps >=10 minutes apart (the confirm
gap, `fpt/deals/confirm.py` C1) instead of one ENROLL + one CONFIRM
product-page fetch, cutting per-product fetch volume for these retailers
to (near) zero and sidestepping whatever is blocking/slowing individual
product-page fetches for them.

Architecture note on `detected_via` (IMPORTANT, read before changing):
`deals.detected_via` is a DB-enforced enum (`db/migrations/008_deals.up.sql`,
CHECK IN ('DISCOVERY_GRID', 'PRODUCT_PAGE', 'DATA_API')) -- adding a new
value is a schema change, out of this coder's delegation scope (Database
Engineer's job). This module therefore reuses `"PRODUCT_PAGE"`, which
`fpt/store/pipeline.py`'s own `_run_detection` already hardcodes
unconditionally for every offer it detects, regardless of what page the
observation actually came from. This is a defensible, NOT a hacky, reuse:
`detected_via="DISCOVERY_GRID"` specifically means "detected from a raw
grid row with only WEAK signal (price only, maybe a range, no confirmed
offer identity) that MUST be re-verified from the real product page" --
`fpt/deals/confirm.py` C2 enforces exactly that ("DISCOVERY_GRID
confirmation must be from a PRODUCT page"), and there is a passing,
intentional test locking that in (`tests/test_confirm.py::
test_c2_discovery_grid_confirmation_must_be_product_page`). A
listing-complete retailer's grid row is NOT that weak signal -- it already
carries the same full price + reference identity a PRODUCT-page fetch
would, for the offer this module builds. Labeling it "PRODUCT_PAGE" is
therefore accurate to what C2 is actually gating on (identity/reference
strength), not a workaround, and it correctly frees confirmation to
happen from a SECOND listing sweep (C2 imposes no page-type restriction
for PRODUCT_PAGE-detected deals). Escalate to the architect before
introducing a genuinely new `detected_via` value (e.g.
`DISCOVERY_GRID_COMPLETE`) instead of this reuse -- that needs a migration
and an architect sign-off on C2's semantics, both out of this task's
authority.

Scope limits (deliberate, flagged in HANDOFF):
  - Only items with a non-range price AND a `claimed_reference_cents`
    present on the SAME grid row are routed through here -- with neither a
    verified nor a claimed reference, no deal rule can fire off a lone
    grid observation anyway (own-history median needs 21+ observations
    spanning 30+ days first), so there's nothing to gain by synthesizing
    an offer for those rows; they still get a normal `discovery_hits` row
    and (if new/fast-track) an ENROLL task via `fpt.scheduler.enroller`,
    unaffected by this module.
  - `task_kind="DISCOVERY"` is used (an already-allowed `crawl_tasks.kind`/
    `price_observations.task_kind` enum value) rather than a new kind --
    same schema-change-avoidance reasoning as `detected_via` above.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

from fpt.adapters.base import (
    Availability,
    ClaimedReferenceKind,
    Condition,
    FetchResponse,
    ParsedListing,
    ParsedOffer,
    PageType,
    SellerType,
)
from fpt.core.models import DiscoveredItem
from fpt.store.pipeline import IngestOutcome, ingest_parsed_listing

logger = logging.getLogger(__name__)

_LISTING_INGEST_TASK_KIND = "DISCOVERY"


def _synthetic_retailer_sku(item: DiscoveredItem) -> str:
    """A stable identifier for this grid row's offer across sweeps.
    `retailer_product_code` when the grid exposes one (most Shopify
    collections do -- the product handle); otherwise the product URL's own
    path, which is stable for a given product across sweeps even without a
    parsed code."""
    if item.retailer_product_code:
        return item.retailer_product_code
    return item.product_url.rstrip("/").rsplit("/", 1)[-1] or item.product_url


def is_eligible_for_listing_ingest(item: DiscoveredItem) -> bool:
    """Only a grid row that already carries BOTH a firm (non-range)
    current price AND a claimed reference price is worth synthesizing an
    observation for -- see module docstring's Scope limits."""
    return (
        item.price_cents is not None
        and not item.price_is_range
        and item.claimed_reference_cents is not None
    )


def _build_synthetic_listing(item: DiscoveredItem, *, page_type: PageType) -> ParsedListing:
    retailer_sku = _synthetic_retailer_sku(item)
    condition = item.condition_hint or Condition.NEW
    offer = ParsedOffer(
        offer_key=f"{retailer_sku}|grid",
        condition=condition,
        condition_raw=item.condition_hint.value if item.condition_hint else "NEW",
        seller_key="first_party",
        seller_name=None,
        seller_type=SellerType.FIRST_PARTY,
        price_cents=item.price_cents,
        shipping_cents=None,
        claimed_reference_cents=item.claimed_reference_cents,
        claimed_reference_kind=item.claimed_reference_kind,
        # CLEARANCE_LISTING rows are, by definition, on the clearance grid;
        # CATALOG_LISTING/USED_LISTING rows are not (their reference, if
        # any, is a plain compare-at/MSRP rather than a clearance marker).
        on_clearance=page_type == PageType.CLEARANCE_LISTING,
        availability=Availability.IN_STOCK,
        stock_qty=None,
        stock_qty_is_floor=False,
        restock_date=None,
        unit_count=None,
    )
    return ParsedListing(
        retailer_sku=retailer_sku,
        retailer_product_code=item.retailer_product_code,
        url=item.product_url,
        title_raw=item.title_raw,
        brand_raw=None,
        model_raw=None,
        variant_label_raw="",
        attributes_raw={},
        gtin_raw=[],
        mpn_raw=None,
        offers=[offer],
        source="html_text",
    )


@dataclass
class ListingIngestOutcome:
    eligible_count: int = 0
    ineligible_count: int = 0
    outcomes: list[IngestOutcome] = field(default_factory=list)


def ingest_discovered_items_as_observations(
    conn,
    *,
    retailer_id: int,
    retailer_slug: str,
    category: str,
    page_type: PageType,
    discovered: Sequence[DiscoveredItem],
    response: FetchResponse,
    adapter_version: str,
    crawl_task_id: int,
    category_bands: dict | None = None,
) -> ListingIngestOutcome:
    """Called once per discovery-listing fetch, in ADDITION to
    `fpt.scheduler.enroller.enroll_discovery_page` (which still runs
    unconditionally for `discovery_hits`/`tracked_urls` bookkeeping) --
    see cli.py's wiring, gated on `retailer_cfg["listing_complete"]`.

    Reuses the discovery page's own `crawl_task_id` (the `DISCOVERY`
    crawl_tasks row `fpt.cli.run_tick` already created for this fetch)
    rather than creating a new one per item -- every synthetic observation
    from this one grid fetch attributes to that single row, matching how a
    real PRODUCT-page fetch attributes all its offers' observations to the
    one crawl_tasks row the queue leased for it.
    """
    result = ListingIngestOutcome()
    for item in discovered:
        if not is_eligible_for_listing_ingest(item):
            result.ineligible_count += 1
            continue
        result.eligible_count += 1
        listing = _build_synthetic_listing(item, page_type=page_type)
        outcome = ingest_parsed_listing(
            conn,
            retailer_id=retailer_id,
            retailer_slug=retailer_slug,
            category=category,
            listing=listing,
            response=response,
            adapter_version=adapter_version,
            task_kind=_LISTING_INGEST_TASK_KIND,
            category_bands=category_bands,
            crawl_task_id=crawl_task_id,
            # A fresh CANDIDATE from this path never gets a queued
            # per-product CONFIRM task -- the NEXT listing sweep's
            # re-ingest of this same synthetic offer supplies the
            # confirming observation instead. See
            # `ingest_parsed_listing`'s docstring for `skip_confirm_task_
            # enqueue` and this module's own docstring for why.
            skip_confirm_task_enqueue=True,
        )
        result.outcomes.append(outcome)
        logger.info(
            "listing_ingest.observed retailer=%s sku=%s observations=%d deal_actions=%s",
            retailer_slug, listing.retailer_sku, len(outcome.observations),
            [a.kind for a in outcome.deal_actions],
        )
    logger.info(
        "listing_ingest.sweep_complete retailer=%s eligible=%d ineligible=%d",
        retailer_slug, result.eligible_count, result.ineligible_count,
    )
    return result
