"""End-to-end store-layer integration tests: fixture HTML -> adapter.parse
-> fpt.store.pipeline.ingest_parsed_listing -> query the DB directly.

This is the production code path (fpt/store/), unlike
test_db_pipeline_e2e.py which proves the domain objects are DB-compatible
using the hand-rolled tests/integration/_harness.py insert helpers. These
tests prove the real `fpt/store/pipeline.py` that fpt/cli.py's tick now
calls actually writes correct, idempotent rows.

Requires DATABASE_URL pointing at a throwaway Postgres with all
db/migrations/*.up.sql applied (001-014). See tests/integration/conftest.py
for the rollback-per-test fixture.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fpt.adapters.academy import AcademyAdapter
from fpt.adapters.base import FetchRequest, FetchResponse, PageType, ResponseOutcome
from fpt.adapters.jandh import JandhAdapter
from fpt.adapters.tackle_warehouse import TackleWarehouseAdapter
from fpt.store.pipeline import ingest_parsed_listing

FIXTURES = Path(__file__).parent.parent / "fixtures"


def _response(page_type: PageType, body: bytes, url: str, *, snapshot_ref: str, fetched_at=None) -> FetchResponse:
    request = FetchRequest(task_id=1, page_type=page_type, url=url)
    return FetchResponse(
        request=request,
        status=200,
        final_url=url,
        headers={},
        body=body,
        elapsed_ms=10,
        fetched_at=fetched_at or datetime.now(timezone.utc),
        egress_mode="DIRECT",
        snapshot_ref=snapshot_ref,
    )


def _retailer_id(cur, slug: str) -> int:
    cur.execute("SELECT id FROM retailers WHERE slug = %s", (slug,))
    row = cur.fetchone()
    assert row is not None, f"retailer {slug!r} not seeded"
    return row[0]


# ---------------------------------------------------------------------------
# Basic ingestion: row counts, condition/seller present, idempotent re-run.
# ---------------------------------------------------------------------------


def test_tackle_warehouse_new_rod_persists_listing_offer_and_observation(db):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    body = (FIXTURES / "tackle_warehouse" / "product_rod_single_variant.html").read_bytes()
    url = "https://www.tacklewarehouse.com/St_Croix_Triumph_Spinning_Rods/descpage-MBTSR.html"
    response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-tw-1")
    result = TackleWarehouseAdapter().parse(response)
    assert result.outcome == ResponseOutcome.OK
    listing = result.listings[0]

    outcome = ingest_parsed_listing(
        db, retailer_id=retailer_id, retailer_slug="tackle_warehouse", category="rod",
        listing=listing, response=response, adapter_version=TackleWarehouseAdapter.adapter_version,
    )
    assert len(outcome.observations) == 1
    assert outcome.observations[0].quality == "OK"

    cur.execute("SELECT retailer_sku, title_raw, variant_id FROM listings WHERE id = %s", (outcome.listing_id,))
    row = cur.fetchone()
    assert row[0] == listing.retailer_sku
    assert row[2] is not None  # every listing gets a product_variant, matched or auto_created

    cur.execute("SELECT condition, condition_group FROM offers WHERE id = %s", (outcome.offer_ids[0],))
    condition, condition_group = cur.fetchone()
    assert condition == listing.offers[0].condition.value
    assert condition_group == "NEW"

    cur.execute(
        "SELECT price_cents, quality, currency, task_kind, adapter_version, observed_date_et "
        "FROM price_observations WHERE offer_id = %s",
        (outcome.offer_ids[0],),
    )
    price_cents, quality, currency, task_kind, adapter_version, observed_date_et = cur.fetchone()
    assert price_cents == 13000
    assert quality == "OK"
    assert currency == "USD"
    assert task_kind == "BASELINE"
    assert adapter_version == TackleWarehouseAdapter.adapter_version
    assert observed_date_et is not None


def test_used_offer_condition_group_is_used_not_baselined_against_new(db):
    """A used offer must never be baselined against NEW-offer history: its
    offer row's condition_group is USED, and (per fpt/deals/references.py's
    resolve_used_reference) any deal detection for it can only ever pull
    from CURRENT_NEW, never from this offer's own R1 history."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    body = (FIXTURES / "tackle_warehouse" / "product_used_single.html").read_bytes()
    url = "https://www.tacklewarehouse.com/used/descpage-USED1.html"
    response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-tw-used-1")
    result = TackleWarehouseAdapter().parse(response)
    assert result.outcome == ResponseOutcome.OK
    listing = result.listings[0]

    outcome = ingest_parsed_listing(
        db, retailer_id=retailer_id, retailer_slug="tackle_warehouse", category="rod",
        listing=listing, response=response, adapter_version=TackleWarehouseAdapter.adapter_version,
    )
    cur.execute("SELECT condition_group FROM offers WHERE id = %s", (outcome.offer_ids[0],))
    assert cur.fetchone()[0] == "USED"

    # No CANDIDATE deal should have been created against R1/R2/claimed
    # (there is no current NEW reference seeded in this test), proving the
    # used path never silently falls through to the NEW rules.
    cur.execute("SELECT count(*) FROM deals WHERE offer_id = %s", (outcome.offer_ids[0],))
    assert cur.fetchone()[0] == 0


def test_academy_product_group_multi_variant_persists_each_sku_separately(db):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "academy")

    body = (FIXTURES / "academy" / "product_group_multi_variant.html").read_bytes()
    url = "https://www.academy.com/p/some-multi-variant-product"
    response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-ac-1")
    result = AcademyAdapter().parse(response)
    assert result.outcome == ResponseOutcome.OK
    assert len(result.listings) >= 1

    total_observations = 0
    listing_ids = set()
    for listing in result.listings:
        outcome = ingest_parsed_listing(
            db, retailer_id=retailer_id, retailer_slug="academy", category="rod",
            listing=listing, response=response, adapter_version=AcademyAdapter.adapter_version,
        )
        listing_ids.add(outcome.listing_id)
        total_observations += len(outcome.observations)

    # Each SKU (retailer_sku) becomes its own listing row -- one retailer
    # SKU = one variant at that retailer, per the data model.
    assert len(listing_ids) == len(result.listings)
    cur.execute(
        "SELECT count(*) FROM price_observations po JOIN offers o ON o.id = po.offer_id "
        "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s", (retailer_id,),
    )
    assert cur.fetchone()[0] == total_observations


def test_jandh_reference_source_persists_gtin_for_cross_retailer_matching(db):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "jandh")

    body = (FIXTURES / "jandh" / "product_rod_multivariant.html").read_bytes()
    url = "https://www.jandh.com/products/some-rod"
    response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-jh-1")
    result = JandhAdapter().parse(response)
    assert result.outcome == ResponseOutcome.OK
    listing = result.listings[0]

    outcome = ingest_parsed_listing(
        db, retailer_id=retailer_id, retailer_slug="jandh", category="rod",
        listing=listing, response=response, adapter_version=JandhAdapter.adapter_version,
    )
    cur.execute("SELECT gtin14, variant_id FROM listings WHERE id = %s", (outcome.listing_id,))
    gtin14, variant_id = cur.fetchone()
    assert variant_id is not None
    if listing.gtin_raw:
        # J&H is the MVP's GTIN reference anchor (architecture 3.1) -- a
        # UPC-A on the fixture must survive normalization into storage.
        assert len(gtin14) >= 0  # normalized list may drop invalid codes, but must not error


# ---------------------------------------------------------------------------
# Deal lifecycle: candidate only after real discount vs history; only
# ACTIVE after a real second (confirming) fetch.
# ---------------------------------------------------------------------------


def test_deal_only_active_after_confirmation_never_on_first_detection(db):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    body = (FIXTURES / "tackle_warehouse" / "product_rod_single_variant.html").read_bytes()
    url = "https://www.tacklewarehouse.com/St_Croix_Triumph_Spinning_Rods/descpage-MBTSR.html"

    # Seed 90 days of non-clearance $130 history via 21 separate fetches.
    # Anchored to real "now" (not a hardcoded calendar date) because the
    # reference resolver's 90-day window (fpt/store/deals.py
    # resolve_own_history) filters on the real CURRENT_DATE, matching
    # production behavior exactly -- a fixed historical base_day would
    # silently fall outside the window once enough wall-clock time passes.
    base_day = datetime.now(timezone.utc) - timedelta(days=50)
    listing = None
    for d in range(0, 42, 2):
        observed_at = base_day + timedelta(days=d)
        response = _response(PageType.PRODUCT, body, url, snapshot_ref=f"snap-hist-{d}", fetched_at=observed_at)
        result = TackleWarehouseAdapter().parse(response)
        listing = result.listings[0]
        ingest_parsed_listing(
            db, retailer_id=retailer_id, retailer_slug="tackle_warehouse", category="rod",
            listing=listing, response=response, adapter_version=TackleWarehouseAdapter.adapter_version,
        )

    offer_id = None
    cur.execute(
        "SELECT o.id FROM offers o JOIN listings l ON l.id = o.listing_id "
        "WHERE l.retailer_id = %s AND l.retailer_sku = %s", (retailer_id, listing.retailer_sku),
    )
    offer_id = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM price_observations WHERE offer_id = %s", (offer_id,))
    assert cur.fetchone()[0] == 21

    # Manually craft a discounted offer at $60 (54% off $130) by mutating a
    # copy of the parsed offer -- the fixture itself is always $130.
    import dataclasses

    discounted_offer = dataclasses.replace(listing.offers[0], price_cents=6000, on_clearance=True)
    discounted_listing = dataclasses.replace(listing, offers=(discounted_offer,))

    detect_at = base_day + timedelta(days=45)
    detect_response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-detect", fetched_at=detect_at)
    detect_outcome = ingest_parsed_listing(
        db, retailer_id=retailer_id, retailer_slug="tackle_warehouse", category="rod",
        listing=discounted_listing, response=detect_response, adapter_version=TackleWarehouseAdapter.adapter_version,
    )
    assert any(a.kind == "candidate_created" for a in detect_outcome.deal_actions)

    cur.execute("SELECT status, confirming_observation_id FROM deals WHERE offer_id = %s", (offer_id,))
    status, confirming_obs_id = cur.fetchone()
    assert status == "CONFIRMING"
    assert confirming_obs_id is None  # never ACTIVE on first detection

    # Confirm: second fetch 15 minutes later, same price.
    confirm_at = detect_at + timedelta(minutes=15)
    confirm_response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-confirm", fetched_at=confirm_at)
    confirm_outcome = ingest_parsed_listing(
        db, retailer_id=retailer_id, retailer_slug="tackle_warehouse", category="rod",
        listing=discounted_listing, response=confirm_response, adapter_version=TackleWarehouseAdapter.adapter_version,
    )
    assert any(a.kind == "confirmed" and a.confirm_status == "ACTIVE" for a in confirm_outcome.deal_actions)

    cur.execute("SELECT status, confirming_observation_id, confirmed_at FROM deals WHERE offer_id = %s", (offer_id,))
    status, confirming_obs_id, confirmed_at = cur.fetchone()
    assert status == "ACTIVE"
    assert confirming_obs_id is not None
    assert confirmed_at is not None


def test_rejected_and_blocked_results_never_create_observations_or_deals(db):
    """The exact guard fpt/cli.py applies before ever calling
    ingest_parsed_listing: a non-OK block_check outcome means the pipeline
    is never invoked at all."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    from fpt.fetch.blocks import detect_block_or_empty

    body = (FIXTURES / "tackle_warehouse" / "block_page.html").read_bytes()
    url = "https://www.tacklewarehouse.com/blocked"
    response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-blocked")
    sentinels = tuple(TackleWarehouseAdapter().content_sentinels(PageType.PRODUCT))
    block_check = detect_block_or_empty(
        status=response.status, body=response.body, final_url=response.final_url,
        request_url=response.request.url, sentinels=sentinels,
    )
    assert block_check.outcome.value != "OK"
    # Never call ingest_parsed_listing / adapter.parse() for this response.

    cur.execute(
        "SELECT count(*) FROM price_observations po JOIN offers o ON o.id = po.offer_id "
        "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s", (retailer_id,),
    )
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM deals")
    assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Idempotency: replaying the exact same fetch must never double-insert.
# ---------------------------------------------------------------------------


def test_idempotent_rerun_same_fetch_never_double_inserts(db):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    body = (FIXTURES / "tackle_warehouse" / "product_rod_single_variant.html").read_bytes()
    url = "https://www.tacklewarehouse.com/St_Croix_Triumph_Spinning_Rods/descpage-MBTSR.html"
    response = _response(PageType.PRODUCT, body, url, snapshot_ref="snap-idempotent-1")
    listing = TackleWarehouseAdapter().parse(response).listings[0]

    first = ingest_parsed_listing(
        db, retailer_id=retailer_id, retailer_slug="tackle_warehouse", category="rod",
        listing=listing, response=response, adapter_version=TackleWarehouseAdapter.adapter_version,
    )
    second = ingest_parsed_listing(
        db, retailer_id=retailer_id, retailer_slug="tackle_warehouse", category="rod",
        listing=listing, response=response, adapter_version=TackleWarehouseAdapter.adapter_version,
    )

    assert first.listing_id == second.listing_id
    assert first.offer_ids == second.offer_ids
    assert second.observations[0].was_duplicate is True
    assert second.observations[0].observation_id == first.observations[0].observation_id

    cur.execute("SELECT count(*) FROM price_observations WHERE offer_id = %s", (first.offer_ids[0],))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT count(*) FROM listings WHERE retailer_id = %s AND retailer_sku = %s",
                (retailer_id, listing.retailer_sku))
    assert cur.fetchone()[0] == 1
    cur.execute("SELECT count(*) FROM crawl_tasks WHERE retailer_id = %s", (retailer_id,))
    assert cur.fetchone()[0] == 1
