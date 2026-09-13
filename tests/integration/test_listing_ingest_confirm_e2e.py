"""Proves mission item 3 (confirmation-via-listing-re-observation) end to
end against a throwaway Postgres: two synthetic CLEARANCE_LISTING sweeps
of the SAME grid row, >=10 minutes apart, reach an ACTIVE deal with ZERO
product-page fetches -- via `fpt.scheduler.listing_ingest`, not the
per-product ENROLL/CONFIRM queue path `test_tick_enroll_confirm_e2e.py`
already covers.

Every assertion below is a live query against the throwaway DB (the `db`
fixture, `tests/integration/conftest.py`), never a mocked/inspected
in-memory object -- this is deliberately "real test output" per the
mission's own instruction not to trust green execution status alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fpt.adapters.base import ClaimedReferenceKind, FetchRequest, FetchResponse, PageType
from fpt.core.models import DiscoveredItem
from fpt.scheduler import listing_ingest

_PRODUCT_URL = "https://www.abugarcia.com/collections/clearance/products/revo-sx"


def _retailer_id(cur, slug: str) -> int:
    cur.execute("SELECT id FROM retailers WHERE slug = %s", (slug,))
    row = cur.fetchone()
    assert row is not None, f"{slug} not seeded -- check db/migrations 012-019"
    return row[0]


def _fetch_response(*, url: str, fetched_at: datetime) -> FetchResponse:
    request = FetchRequest(task_id=1, page_type=PageType.CLEARANCE_LISTING, url=url)
    return FetchResponse(
        request=request, status=200, final_url=url, headers={}, body=b"<html></html>",
        elapsed_ms=1, fetched_at=fetched_at, egress_mode="DIRECT",
        snapshot_ref=f"listing-ingest-test:{fetched_at.isoformat()}",
    )


def _grid_item(*, price_cents: int, claimed_reference_cents: int) -> DiscoveredItem:
    """A 54% discount vs the grid's own claimed reference -- above the
    50% min_discount_pct AND the min_saving_cents floor (config/deal_rules.yaml)."""
    return DiscoveredItem(
        product_url=_PRODUCT_URL,
        retailer_product_code="revo-sx",
        title_raw="Abu Garcia Revo SX Reel",
        price_cents=price_cents,
        price_is_range=False,
        claimed_reference_cents=claimed_reference_cents,
        claimed_reference_kind=ClaimedReferenceKind.MSRP,
        condition_hint=None,
        category_hint="reel",
    )


def test_two_listing_sweeps_reach_active_deal_with_zero_product_fetches(db):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "abugarcia")

    def _seed_discovery_task(seen_at: datetime) -> int:
        cur.execute(
            """
            INSERT INTO crawl_tasks (retailer_id, kind, priority, page_type, url, dedupe_key, status, outcome, finished_at)
            VALUES (%s, 'DISCOVERY', 30, 'CLEARANCE_LISTING', %s, %s, 'DONE', 'OK', %s)
            RETURNING id
            """,
            (retailer_id, _PRODUCT_URL + "?sweep", f"DISCOVERY:test:{seen_at.isoformat()}", seen_at),
        )
        return cur.fetchone()[0]

    sweep1_at = datetime.now(timezone.utc)
    item = _grid_item(price_cents=6000, claimed_reference_cents=13000)  # 53.8% off

    task1_id = _seed_discovery_task(sweep1_at)
    response1 = _fetch_response(url=_PRODUCT_URL + "?sweep", fetched_at=sweep1_at)

    outcome1 = listing_ingest.ingest_discovered_items_as_observations(
        db, retailer_id=retailer_id, retailer_slug="abugarcia", category="reel",
        page_type=PageType.CLEARANCE_LISTING, discovered=[item], response=response1,
        adapter_version="test", crawl_task_id=task1_id,
    )
    assert outcome1.eligible_count == 1
    assert outcome1.ineligible_count == 0

    # --- Real queried evidence, sweep 1: one observation, one CANDIDATE
    # deal, zero PRODUCT-page fetches anywhere in crawl_tasks.
    cur.execute(
        "SELECT count(*) FROM price_observations po JOIN offers o ON o.id = po.offer_id "
        "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s AND l.retailer_sku = 'revo-sx'",
        (retailer_id,),
    )
    assert cur.fetchone()[0] == 1

    cur.execute(
        "SELECT status, rule, lane FROM deals d JOIN offers o ON o.id = d.offer_id "
        "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s AND l.retailer_sku = 'revo-sx'",
        (retailer_id,),
    )
    deal_row = cur.fetchone()
    assert deal_row is not None, "expected a CANDIDATE deal after sweep 1"
    assert deal_row[0] == "CONFIRMING"
    assert deal_row[1] == "DEEP_DISCOUNT_CLAIMED"
    assert deal_row[2] == "CLAIMED"

    cur.execute(
        "SELECT count(*) FROM crawl_tasks WHERE retailer_id = %s AND kind IN ('ENROLL', 'CONFIRM') "
        "AND page_type = 'PRODUCT'",
        (retailer_id,),
    )
    assert cur.fetchone()[0] == 0  # zero product-page fetches -- confirmation via listing sweep only

    # --- Sweep 2, 15 minutes later (>= C1's 10-minute confirm gap): the
    # SAME grid row observed again, same price, same claimed reference.
    sweep2_at = sweep1_at + timedelta(minutes=15)
    task2_id = _seed_discovery_task(sweep2_at)
    response2 = _fetch_response(url=_PRODUCT_URL + "?sweep", fetched_at=sweep2_at)

    outcome2 = listing_ingest.ingest_discovered_items_as_observations(
        db, retailer_id=retailer_id, retailer_slug="abugarcia", category="reel",
        page_type=PageType.CLEARANCE_LISTING, discovered=[item], response=response2,
        adapter_version="test", crawl_task_id=task2_id,
    )
    assert outcome2.eligible_count == 1
    assert any(a.kind == "confirmed" for lo in outcome2.outcomes for a in lo.deal_actions)

    # --- Real queried evidence, sweep 2: TWO observations total now, deal
    # is ACTIVE, discount recomputed at confirm time, and still zero
    # product-page fetches were ever queued or run for this offer.
    cur.execute(
        "SELECT count(*) FROM price_observations po JOIN offers o ON o.id = po.offer_id "
        "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s AND l.retailer_sku = 'revo-sx'",
        (retailer_id,),
    )
    assert cur.fetchone()[0] == 2

    cur.execute(
        "SELECT status, discount_pct, price_cents, reference_cents, confirmed_at FROM deals d "
        "JOIN offers o ON o.id = d.offer_id JOIN listings l ON l.id = o.listing_id "
        "WHERE l.retailer_id = %s AND l.retailer_sku = 'revo-sx'",
        (retailer_id,),
    )
    status, discount_pct, price_cents, reference_cents, confirmed_at = cur.fetchone()
    assert status == "ACTIVE"
    assert confirmed_at is not None
    assert price_cents == 6000
    assert reference_cents == 13000
    assert float(discount_pct) == pytest.approx(53.85, abs=0.01)

    cur.execute(
        "SELECT count(*) FROM crawl_tasks WHERE retailer_id = %s AND page_type = 'PRODUCT'",
        (retailer_id,),
    )
    assert cur.fetchone()[0] == 0


def test_range_priced_grid_row_is_ineligible_and_untouched(db):
    """Scope guard (module docstring): a "from $X" grid row is never routed
    through listing_ingest -- it still gets discovery_hits bookkeeping via
    the normal enroller, unaffected here, but produces zero observations
    from this path."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "abugarcia")
    now = datetime.now(timezone.utc)

    cur.execute(
        """
        INSERT INTO crawl_tasks (retailer_id, kind, priority, page_type, url, dedupe_key, status, outcome, finished_at)
        VALUES (%s, 'DISCOVERY', 30, 'CLEARANCE_LISTING', %s, %s, 'DONE', 'OK', %s)
        RETURNING id
        """,
        (retailer_id, "https://www.abugarcia.com/collections/clearance", "DISCOVERY:test:range", now),
    )
    task_id = cur.fetchone()[0]

    item = DiscoveredItem(
        product_url="https://www.abugarcia.com/collections/clearance/products/some-rod",
        retailer_product_code="some-rod",
        title_raw="Some Rod",
        price_cents=5000,
        price_is_range=True,  # "from $50" -- ambiguous, must be excluded
        claimed_reference_cents=10000,
        claimed_reference_kind=ClaimedReferenceKind.MSRP,
        condition_hint=None,
        category_hint="rod",
    )
    outcome = listing_ingest.ingest_discovered_items_as_observations(
        db, retailer_id=retailer_id, retailer_slug="abugarcia", category="rod",
        page_type=PageType.CLEARANCE_LISTING, discovered=[item],
        response=_fetch_response(url="https://www.abugarcia.com/collections/clearance", fetched_at=now),
        adapter_version="test", crawl_task_id=task_id,
    )
    assert outcome.eligible_count == 0
    assert outcome.ineligible_count == 1

    cur.execute(
        "SELECT count(*) FROM listings WHERE retailer_id = %s AND retailer_sku = 'some-rod'",
        (retailer_id,),
    )
    assert cur.fetchone()[0] == 0
