"""Single entrypoint wiring parse -> validate -> store -> detect -> confirm
for one `ParsedListing` (one retailer SKU) observed on one fetch.

This is what fpt/cli.py's tick path calls per listing/offer instead of the
old `_try_db_persist`, which never wrote a row (HANDOFF CRITICAL-1).

Scope boundary, deliberately: this module ingests `ParsedListing`s -- i.e.
PRODUCT-page (or API_BATCH) results that already carry real offers. A
`CLEARANCE_LISTING`/`USED_LISTING` grid's `DiscoveredItem`s are NEVER
routed through here (architecture doc 3.1: "A grid row from a listing page
is never an observation"); enrolling a `DiscoveredItem` into a tracked
product-page fetch is scheduler/enroller territory (architecture 2.2,
explicitly out of fpt/cli.py's stated scope) and is not built here --
flagged as a HIGH uncertainty in the HANDOFF, since it means today's
`config/discovery_pages.yaml` (CLEARANCE_LISTING/USED_LISTING only) cannot
by itself produce a persisted observation end to end without a caller that
also fetches the product page for a discovered item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from fpt.adapters.base import FetchResponse, PageType, ParsedListing, ParsedOffer
from fpt.core.gtin import normalize_all
from fpt.core.normalize.brands import canonical_brand
from fpt.core.normalize.keys import build_variant_key
from fpt.core.normalize.rods import normalize_rod_attributes
from fpt.deals.confirm import confirm_candidate
from fpt.deals.detect import (
    DealCandidate,
    detect_deep_discount_claimed,
    detect_deep_discount_new,
    detect_used_vs_current_new,
)
from fpt.deals.references import claimed_inflated as compute_claimed_inflated
from fpt.pipeline.validate import ObservationContext, ObservationInput, validate_observation
from fpt.store import catalog, deals as deals_store, listings as listings_store
from fpt.store.observations import (
    RecordedObservation,
    default_task_kind,
    get_or_create_crawl_task,
    record_observation,
)

# The confirm window (architecture 6.4, C1) -- an observation older than
# this relative to an open deal's detection can still be a confirming
# fetch; this module does not enforce a maximum age here, `confirm_
# candidate` (C1) is the single source of truth for the minimum gap.
_DEFAULT_MIN_DELAY_MINUTES = 10


def _normalize_attributes(category: str, attributes_raw: dict) -> dict:
    if category == "rod":
        # TW's style-item map is exactly what normalize_rod_attributes
        # expects; other adapters' attributes_raw shapes are not rod-style
        # maps, so this only fires for the one category with a real
        # normalizer implemented today (fpt/core/normalize/rods.py).
        return normalize_rod_attributes(attributes_raw)
    return dict(attributes_raw)


def _fallback_variant_key(retailer_slug: str, retailer_sku: str) -> str:
    """When required category attributes are missing (or no normalizer
    exists yet for that category -- only `rod` has one), the offer must
    still be tracked (architecture 4.2: "the offer is still tracked").
    This key is retailer-scoped and therefore never accidentally collides
    with, or auto-matches against, another retailer's listing -- it is a
    storage placeholder, not a claim of identity."""
    return f"raw:{retailer_slug}:{retailer_sku}"


@dataclass(frozen=True)
class DealAction:
    kind: str  # "none" | "candidate_created" | "confirmed"
    deal_id: int | None = None
    confirm_status: str | None = None


@dataclass(frozen=True)
class IngestOutcome:
    listing_id: int
    offer_ids: list[int] = field(default_factory=list)
    observations: list[RecordedObservation] = field(default_factory=list)
    deal_actions: list[DealAction] = field(default_factory=list)


def _last_ok_context(cur, offer_id: int) -> tuple[int | None, int | None]:
    cur.execute(
        "SELECT price_cents, unit_count FROM price_observations "
        "WHERE offer_id = %s AND quality = 'OK' ORDER BY observed_at DESC LIMIT 1",
        (offer_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


def _category_floor_cents(category: str, category_bands: dict) -> int | None:
    band = category_bands.get(category, {})
    return band.get("floor_cents")


def _run_detection(
    cur,
    *,
    offer_id: int,
    offer: ParsedOffer,
    observed_id: int,
    observed_at,
    detected_via: str,
    category: str,
) -> DealAction:
    condition_group = deals_store.get_offer_context(cur, offer_id)["condition_group"]

    if condition_group == "NEW":
        verified = deals_store.resolve_verified_reference_for_offer(cur, offer_id)
        candidate: DealCandidate | None = None
        reference_kind = None
        reference_detail: dict = {}
        inflated = compute_claimed_inflated(offer.claimed_reference_cents, verified.cents if verified else None)
        if verified is not None:
            candidate = detect_deep_discount_new(
                condition=offer.condition,
                availability=offer.availability,
                price_cents=offer.price_cents,
                verified_reference_cents=verified.cents,
            )
            reference_kind = verified.kind
            reference_detail = verified.detail
        if candidate is None:
            candidate = detect_deep_discount_claimed(
                condition=offer.condition,
                price_cents=offer.price_cents,
                claimed_reference_cents=offer.claimed_reference_cents,
                on_clearance=offer.on_clearance,
                has_verified_reference=verified is not None,
            )
            if candidate is not None:
                reference_kind = "RETAILER_CLAIMED"
                reference_detail = {}
        if candidate is None:
            return DealAction(kind="none")
        open_deal = deals_store.create_candidate_deal(
            cur, offer_id=offer_id, candidate=candidate, detected_observation_id=observed_id,
            detected_via=detected_via, reference_kind=reference_kind, reference_detail=reference_detail,
            detected_at=observed_at,
            claimed_reference_cents=offer.claimed_reference_cents,
            claimed_reference_kind=offer.claimed_reference_kind.value if offer.claimed_reference_kind else None,
            claimed_inflated=inflated,
        )
        return DealAction(kind="candidate_created", deal_id=open_deal.id)

    # USED/OPEN_BOX/REFURB path.
    ctx = deals_store.get_offer_context(cur, offer_id)
    reference_cents = deals_store.resolve_current_new_for_variant(
        cur, variant_id=ctx["variant_id"], exclude_offer_id=offer_id
    )
    landed_price = offer.price_cents + offer.shipping_cents if offer.shipping_cents else offer.price_cents
    candidate = detect_used_vs_current_new(
        condition=offer.condition, availability=offer.availability,
        landed_price_cents=landed_price, current_new_reference_cents=reference_cents,
    )
    if candidate is None:
        return DealAction(kind="none")
    open_deal = deals_store.create_candidate_deal(
        cur, offer_id=offer_id, candidate=candidate, detected_observation_id=observed_id,
        detected_via=detected_via, reference_kind="CURRENT_NEW", reference_detail={},
        detected_at=observed_at,
    )
    return DealAction(kind="candidate_created", deal_id=open_deal.id)


def _try_confirm(
    cur,
    *,
    offer_id: int,
    offer: ParsedOffer,
    listing: ParsedListing,
    listing_row_variant_key: str | None,
    listing_row_unit_count: int | None,
    observed_id: int,
    observed_at,
    page_type: PageType,
    validation_quality: str,
    validation_reasons: list[str],
    category_floor_cents: int | None,
) -> DealAction | None:
    for rule in ("DEEP_DISCOUNT_NEW", "DEEP_DISCOUNT_CLAIMED", "USED_VS_CURRENT_NEW"):
        open_deal = deals_store.find_open_deal(cur, offer_id=offer_id, rule=rule)
        if open_deal is None or open_deal.status not in ("CANDIDATE", "CONFIRMING"):
            continue
        if open_deal.detected_observation_id == observed_id:
            continue  # this IS the detecting observation, not a confirming one
        if observed_at - open_deal.detected_at < timedelta(minutes=_DEFAULT_MIN_DELAY_MINUTES):
            continue
        if offer.price_cents is None:
            continue
        ctx = deals_store.build_confirm_context(
            cur, open_deal=open_deal, offer_id=offer_id,
            confirming_observation_id=observed_id, confirming_observed_at=observed_at,
            confirming_page_type=page_type.value, confirming_price_cents=offer.price_cents,
            confirming_quality=validation_quality, confirming_quality_reasons=validation_reasons,
            detected_retailer_sku=listing.retailer_sku, confirming_retailer_sku=listing.retailer_sku,
            detected_offer_key=offer.offer_key, confirming_offer_key=offer.offer_key,
            detected_condition=offer.condition.value, confirming_condition=offer.condition.value,
            detected_seller_key=offer.seller_key, confirming_seller_key=offer.seller_key,
            detected_variant_key_observed=listing_row_variant_key,
            confirming_variant_key_observed=listing_row_variant_key,
            listing_variant_key_observed=listing_row_variant_key,
            detected_unit_count=listing_row_unit_count, confirming_unit_count=listing_row_unit_count,
            listing_unit_count=listing_row_unit_count, category_floor_cents=category_floor_cents,
        )
        result = confirm_candidate(ctx)
        deals_store.apply_confirm_result(cur, deal_id=open_deal.id, confirming_observation_id=observed_id, result=result)
        return DealAction(kind="confirmed", deal_id=open_deal.id, confirm_status=result.status)
    return None


def ingest_parsed_listing(
    conn,
    *,
    retailer_id: int,
    retailer_slug: str,
    category: str,
    listing: ParsedListing,
    response: FetchResponse,
    adapter_version: str,
    task_kind: str | None = None,
    category_bands: dict | None = None,
) -> IngestOutcome:
    """Persists one PRODUCT-page (or API_BATCH) `ParsedListing` and all of
    its offers, then runs deal detection/confirmation for each offer.

    Never called for a response whose block/outcome check was not OK, and
    never called with a `ParsedListing` built from a listing/grid page --
    both are the caller's responsibility (see module docstring)."""
    from fpt.pipeline.validate import load_category_bands

    bands = category_bands if category_bands is not None else load_category_bands()
    floor_cents = _category_floor_cents(category, bands)
    page_type = response.request.page_type
    kind = task_kind or default_task_kind(page_type)

    cur = conn.cursor()

    attributes_norm = _normalize_attributes(category, dict(listing.attributes_raw))
    gtin14 = normalize_all(list(listing.gtin_raw))
    variant_key = build_variant_key(category, attributes_norm)
    if variant_key is None:
        variant_key = _fallback_variant_key(retailer_slug, listing.retailer_sku)
    brand_display = canonical_brand(listing.brand_raw) or listing.brand_raw
    # model_key groups variants under one product (e.g. every size/power of
    # "Triumph Spinning Rod"); it is coarser than variant_key on purpose.
    # No title/model parser exists yet (out of this task's scope -- see
    # HANDOFF), so this falls back to the retailer's own model_raw/title,
    # meaning products rarely merge across retailers without a shared
    # model_raw string. GTIN matching (catalog.resolve_or_create_variant)
    # still links the *variant* across retailers regardless of this.
    model_key = catalog.slugify(listing.model_raw or listing.title_raw or listing.retailer_sku)

    product_id, variant_id, match_status = catalog.resolve_or_create_variant(
        cur,
        brand_raw=brand_display,
        category=category,
        model_key=model_key,
        variant_key=variant_key,
        label=listing.variant_label_raw or listing.title_raw,
        attributes=attributes_norm,
        unit_count=listing.offers[0].unit_count if listing.offers else None,
        gtin14=gtin14,
    )

    listing_id = listings_store.upsert_listing(
        cur,
        retailer_id=retailer_id,
        retailer_sku=listing.retailer_sku,
        retailer_product_code=listing.retailer_product_code,
        url=listing.url or response.final_url,
        title_raw=listing.title_raw,
        brand_raw=brand_display,
        model_raw=listing.model_raw,
        variant_label_raw=listing.variant_label_raw,
        attributes_raw=listing.attributes_raw,
        attributes_norm=attributes_norm,
        variant_key_observed=variant_key,
        gtin14=gtin14,
        mpn_raw=listing.mpn_raw,
        unit_count=listing.offers[0].unit_count if listing.offers else None,
        variant_id=variant_id,
        match_status=match_status,
    )

    dedupe_key = f"{kind}:{retailer_slug}:{listing.retailer_sku}:{response.snapshot_ref}"
    crawl_task_id = get_or_create_crawl_task(
        cur, retailer_id=retailer_id, kind=kind, page_type=page_type,
        url=response.final_url or listing.url, dedupe_key=dedupe_key,
    )

    outcome = IngestOutcome(listing_id=listing_id)

    for offer in listing.offers:
        seller_id = listings_store.get_or_create_seller(
            cur, retailer_id=retailer_id, seller_key=offer.seller_key,
            seller_type=offer.seller_type, name=offer.seller_name,
        )
        offer_id = listings_store.upsert_offer(
            cur, listing_id=listing_id, seller_id=seller_id, offer_key=offer.offer_key,
            condition=offer.condition, condition_raw=offer.condition_raw,
        )
        outcome.offer_ids.append(offer_id)

        last_ok_price, last_ok_unit_count = _last_ok_context(cur, offer_id)
        obs_input = ObservationInput(
            price_cents=offer.price_cents,
            currency="USD",
            category=category,
            claimed_reference_cents=offer.claimed_reference_cents,
            on_clearance=offer.on_clearance,
            unit_count=offer.unit_count,
            variant_key_observed=variant_key,
            availability=offer.availability,
            condition_wording_mapped=True,
            is_used_page_type=page_type == PageType.USED_LISTING,
        )
        validation = validate_observation(
            obs_input,
            ObservationContext(
                last_ok_price_cents=last_ok_price,
                last_ok_unit_count=last_ok_unit_count,
                listing_variant_key_observed=variant_key,
                listing_unit_count=listing.offers[0].unit_count if listing.offers else None,
            ),
            category_bands=bands,
        )

        recorded = record_observation(
            cur, offer_id=offer_id, crawl_task_id=crawl_task_id, task_kind=kind,
            observed_at=response.fetched_at, price_cents=offer.price_cents,
            shipping_cents=offer.shipping_cents, claimed_reference_cents=offer.claimed_reference_cents,
            claimed_reference_kind=offer.claimed_reference_kind.value if offer.claimed_reference_kind else None,
            on_clearance=offer.on_clearance, availability=offer.availability.value,
            stock_qty=offer.stock_qty, stock_qty_is_floor=offer.stock_qty_is_floor,
            restock_date=offer.restock_date, unit_count=offer.unit_count,
            variant_key_observed=variant_key, validation=validation,
            adapter_version=adapter_version, snapshot_ref=response.snapshot_ref,
        )
        outcome.observations.append(recorded)

        if recorded.was_duplicate:
            continue  # idempotent re-run: never re-detect/re-confirm off a replayed fetch

        confirm_action = _try_confirm(
            cur, offer_id=offer_id, offer=offer, listing=listing,
            listing_row_variant_key=variant_key, listing_row_unit_count=offer.unit_count,
            observed_id=recorded.observation_id, observed_at=response.fetched_at, page_type=page_type,
            validation_quality=validation.quality, validation_reasons=validation.reasons,
            category_floor_cents=floor_cents,
        )
        if confirm_action is not None:
            outcome.deal_actions.append(confirm_action)
        elif validation.quality == "OK" and offer.price_cents is not None:
            action = _run_detection(
                cur, offer_id=offer_id, offer=offer, observed_id=recorded.observation_id,
                observed_at=response.fetched_at, detected_via="PRODUCT_PAGE", category=category,
            )
            outcome.deal_actions.append(action)

    return outcome
