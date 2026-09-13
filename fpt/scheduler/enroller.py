"""Discovery enrollment (architecture doc 5.1 sequence, 5.2 planner rules).

Turns every `DiscoveredItem` from a CLEARANCE_LISTING/USED_LISTING/
CATALOG_LISTING sweep into:

  1. a `discovery_hits` row -- every grid row seen, per sweep (180-day
     retention per the architecture doc; this module never decides
     retention, only inserts). A discovery hit is NEVER itself an
     observation or a deal (architecture 3.1's variant-mismatch guard).
  2. an upserted `tracked_urls` row (source DISCOVERY_CLEARANCE/
     DISCOVERY_USED/DISCOVERY_CATALOG, 120-day TTL from this sweep).
  3. a QUEUED `crawl_tasks` ENROLL task for the product page -- but ONLY
     for a never-before-seen tracked_url, or one whose implied discount
     (from the retailer's own claimed reference on the grid) is at or
     above the fast-track threshold (architecture 5.1: "priority 40, or 15
     if implied >= 45%"). This is the one place `implied_discount_pct`
     (computed here, stored on discovery_hits) is used -- it is
     deliberately NEVER treated as a real discount anywhere downstream
     (architecture 4.1's own comment on the column: "never published
     directly"); it only decides queue priority.

Respects `tracked_url_cap` (architecture 3.3, per-retailer): once a
retailer's enabled tracked_urls reach the cap, new items are still
recorded as discovery_hits (nothing is ever lost) but do not get a new
tracked_url/ENROLL task. Eviction by `interest_score` to make room is
scheduler/cap-management scope and is not built here -- flagged in the
HANDOFF.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Sequence

import psycopg

from fpt.core.models import DiscoveredItem, PageType

logger = logging.getLogger(__name__)

# Architecture doc 5.1: "upsert tracked_urls (source DISCOVERY_CLEARANCE, TTL 120d)".
DISCOVERY_TTL_DAYS = 120
FAST_TRACK_MIN_IMPLIED_DISCOUNT_PCT = 45.0
FAST_TRACK_PRIORITY = 15
NORMAL_ENROLL_PRIORITY = 40

_SOURCE_BY_PAGE_TYPE = {
    PageType.CLEARANCE_LISTING: "DISCOVERY_CLEARANCE",
    PageType.USED_LISTING: "DISCOVERY_USED",
    PageType.CATALOG_LISTING: "DISCOVERY_CATALOG",
}


def _implied_discount_pct(price_cents: int | None, claimed_reference_cents: int | None) -> float | None:
    if price_cents is None or claimed_reference_cents is None or claimed_reference_cents <= 0:
        return None
    return round((1 - (price_cents / claimed_reference_cents)) * 100, 2)


@dataclass
class EnrollOutcome:
    discovery_hit_ids: list[int] = field(default_factory=list)
    tasks_enqueued: int = 0
    items_capped: int = 0
    items_unsafe: int = 0
    unsafe_reasons: list[str] = field(default_factory=list)
    items_skipped: int = 0
    skip_reasons: list[str] = field(default_factory=list)


def get_or_create_discovery_page(
    cur,
    *,
    retailer_id: int,
    page_type: PageType,
    url: str,
    category_hint: str,
    interval_minutes: int,
    max_pages: int,
    seen_at: datetime,
    item_count: int,
) -> int:
    """Upserts the `discovery_pages` row this sweep belongs to (needed as
    `discovery_hits.discovery_page_id`'s FK target) and records the sweep
    itself (`last_swept_at`, `last_item_count` -- architecture 5.4's
    "Discovery page item count collapses" health check reads this)."""
    cur.execute("SELECT id FROM discovery_pages WHERE retailer_id = %s AND url = %s", (retailer_id, url))
    row = cur.fetchone()
    if row is not None:
        cur.execute(
            "UPDATE discovery_pages SET last_swept_at = %s, last_item_count = %s WHERE id = %s",
            (seen_at, item_count, row[0]),
        )
        return row[0]

    cur.execute(
        """
        INSERT INTO discovery_pages (
            retailer_id, page_type, url, category_hint, interval_minutes, max_pages,
            last_swept_at, last_item_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (retailer_id, page_type.value, url, category_hint, interval_minutes, max_pages, seen_at, item_count),
    )
    return cur.fetchone()[0]


def _enabled_tracked_url_count(cur, retailer_id: int) -> int:
    cur.execute("SELECT count(*) FROM tracked_urls WHERE retailer_id = %s AND enabled", (retailer_id,))
    return cur.fetchone()[0]


def _get_or_create_tracked_url(
    cur, *, retailer_id: int, url: str, category_hint: str, source: str, seen_at: datetime
) -> tuple[int, bool]:
    """Returns (tracked_url_id, was_new). A re-sighting of an already
    tracked URL pushes its expiry back out another 120 days from this
    sweep -- still-clearanced items should not silently expire mid-sale."""
    cur.execute("SELECT id, expires_at FROM tracked_urls WHERE retailer_id = %s AND url = %s", (retailer_id, url))
    row = cur.fetchone()
    if row is not None:
        tracked_url_id, expires_at = row
        if expires_at is not None:
            cur.execute(
                "UPDATE tracked_urls SET expires_at = %s, enabled = true WHERE id = %s",
                (seen_at + timedelta(days=DISCOVERY_TTL_DAYS), tracked_url_id),
            )
        return tracked_url_id, False

    cur.execute(
        """
        INSERT INTO tracked_urls (retailer_id, url, category_hint, source, expires_at)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING id
        """,
        (retailer_id, url, category_hint, source, seen_at + timedelta(days=DISCOVERY_TTL_DAYS)),
    )
    return cur.fetchone()[0], True


def _enqueue_enroll_task(
    cur, *, retailer_id: int, url: str, category_hint: str, priority: int, dedupe_key: str, not_before: datetime
) -> bool:
    """Returns True if a new ENROLL task was queued, False if one was
    already open (`crawl_tasks_dedupe_open` -- migration 006).

    `not_before` is set explicitly to the discovery sweep's own `seen_at`
    rather than left to the column's `DEFAULT now()` -- the queue drainer
    (fpt/scheduler/queue.py) compares `not_before <= clock` using a
    Python-side clock that may be captured moments before this INSERT
    actually runs, and the DB server's own wall clock is not guaranteed to
    be perfectly synchronized with the application host's (a real,
    reproduced bug during this task's own testing: a few hundred
    milliseconds of host/container clock skew made a `DEFAULT now()`
    ENROLL task's `not_before` land AFTER the same tick's `now`, so the
    task it had just enqueued was never drained in that same tick)."""
    cur.execute("SAVEPOINT before_enroll_task")
    try:
        cur.execute(
            """
            INSERT INTO crawl_tasks (retailer_id, kind, priority, page_type, url, params, dedupe_key, status, not_before)
            VALUES (%s, 'ENROLL', %s, 'PRODUCT', %s, %s::jsonb, %s, 'QUEUED', %s)
            """,
            (retailer_id, priority, url, json.dumps({"category_hint": category_hint}), dedupe_key, not_before),
        )
        cur.execute("RELEASE SAVEPOINT before_enroll_task")
        return True
    except psycopg.errors.UniqueViolation:
        cur.execute("ROLLBACK TO SAVEPOINT before_enroll_task")
        return False


def enroll_discovery_page(
    cur,
    *,
    retailer_id: int,
    discovery_page_id: int,
    page_type: PageType,
    category_hint: str,
    crawl_task_id: int,
    discovered: Sequence[DiscoveredItem],
    seen_at: datetime,
    tracked_url_cap: int | None,
    allowed_hosts,
    resolver=None,
    skip_enroll_urls: frozenset[str] | None = None,
) -> EnrollOutcome:
    """`allowed_hosts`/`resolver`: security review M3 -- a retailer's own
    grid page is untrusted input, and `item.product_url` is a value it
    supplied. Every `product_url` is checked with the same SSRF guard
    (`fpt.fetch.url_safety.check_url_safety`) used for actual fetches
    BEFORE it is ever written to `tracked_urls` or turned into a queued
    fetch task -- an unsafe URL still gets its `discovery_hits` row (the
    row we saw, verbatim, is data worth keeping for the health/audit
    trail) but is never enrolled or enqueued, and is counted separately
    from `items_capped` in the returned outcome.

    `skip_enroll_urls` (mission item 3/4, throughput): a set of
    `product_url`s that a caller has ALREADY detected/confirmed directly
    from this grid row (`fpt.scheduler.listing_ingest`, for
    "listing-complete" retailers) -- those URLs still get their
    `discovery_hits`/`tracked_urls` bookkeeping here (never lost) but are
    never turned into a per-product ENROLL crawl_task, since that fetch
    would be pure duplicate work: the observation it would produce already
    exists.

    Each item is processed in its own SAVEPOINT (robustness review M4):
    one item whose data trips a constraint the query above didn't already
    guard against (a malformed value, an unexpected DB error) is skipped
    and logged rather than rolling back every other item already
    processed in this same discovery sweep.
    """
    skip_enroll_urls = skip_enroll_urls or frozenset()
    from fpt.fetch.url_safety import check_url_safety

    source = _SOURCE_BY_PAGE_TYPE.get(page_type, "DISCOVERY_CLEARANCE")
    outcome = EnrollOutcome()

    for item in discovered:
        cur.execute("SAVEPOINT before_discovery_item")
        try:
            implied_pct = _implied_discount_pct(item.price_cents, item.claimed_reference_cents)
            cur.execute(
                """
                INSERT INTO discovery_hits (
                    discovery_page_id, crawl_task_id, title_raw, price_cents, price_is_range,
                    claimed_reference_cents, claimed_reference_kind, implied_discount_pct,
                    condition_hint, seen_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    discovery_page_id,
                    crawl_task_id,
                    item.title_raw,
                    item.price_cents,
                    item.price_is_range,
                    item.claimed_reference_cents,
                    item.claimed_reference_kind.value if item.claimed_reference_kind else None,
                    implied_pct,
                    item.condition_hint.value if item.condition_hint else None,
                    seen_at,
                ),
            )
            hit_id = cur.fetchone()[0]
            outcome.discovery_hit_ids.append(hit_id)

            safety = check_url_safety(item.product_url, allowed_hosts=allowed_hosts, resolver=resolver)
            if not safety.allowed:
                outcome.items_unsafe += 1
                outcome.unsafe_reasons.append(safety.reason or "unsafe_url")
                cur.execute("RELEASE SAVEPOINT before_discovery_item")
                continue

            current_count = _enabled_tracked_url_count(cur, retailer_id)
            if tracked_url_cap is not None and current_count >= tracked_url_cap:
                outcome.items_capped += 1
                cur.execute("RELEASE SAVEPOINT before_discovery_item")
                continue

            item_category = item.category_hint or category_hint
            tracked_url_id, was_new = _get_or_create_tracked_url(
                cur, retailer_id=retailer_id, url=item.product_url,
                category_hint=item_category, source=source, seen_at=seen_at,
            )
            cur.execute("UPDATE discovery_hits SET tracked_url_id = %s WHERE id = %s", (tracked_url_id, hit_id))

            fast_track = implied_pct is not None and implied_pct >= FAST_TRACK_MIN_IMPLIED_DISCOUNT_PCT
            if (was_new or fast_track) and item.product_url not in skip_enroll_urls:
                priority = FAST_TRACK_PRIORITY if fast_track else NORMAL_ENROLL_PRIORITY
                dedupe_key = f"ENROLL:{retailer_id}:{item.product_url}"
                if _enqueue_enroll_task(
                    cur, retailer_id=retailer_id, url=item.product_url,
                    category_hint=item_category, priority=priority, dedupe_key=dedupe_key,
                    not_before=seen_at,
                ):
                    outcome.tasks_enqueued += 1

            cur.execute("RELEASE SAVEPOINT before_discovery_item")
        except Exception as exc:  # noqa: BLE001 - one bad item must never sink the whole sweep
            cur.execute("ROLLBACK TO SAVEPOINT before_discovery_item")
            outcome.items_skipped += 1
            outcome.skip_reasons.append(f"{item.product_url}: {type(exc).__name__}")
            logger.warning(
                "enroller.item_skipped retailer_id=%s url=%s error=%s",
                retailer_id, item.product_url, type(exc).__name__,
            )

    logger.info(
        "enroller.sweep_complete retailer_id=%s hits=%d tasks_enqueued=%d capped=%d unsafe=%d skipped=%d",
        retailer_id, len(outcome.discovery_hit_ids), outcome.tasks_enqueued, outcome.items_capped,
        outcome.items_unsafe, outcome.items_skipped,
    )
    return outcome


def expire_stale_tracked_urls(cur, *, retailer_id: int, now: datetime) -> int:
    """Architecture 5.2 item 2: "Expired tracked_urls (past expires_at, no
    watches, no open deal) are disabled." Watches are not modeled yet (no
    Phase covers them in this task), so the guard here is "no open deal on
    any offer belonging to a listing at this URL"."""
    cur.execute(
        """
        UPDATE tracked_urls tu SET enabled = false
        WHERE tu.retailer_id = %s AND tu.enabled AND tu.expires_at IS NOT NULL AND tu.expires_at < %s
          AND NOT EXISTS (
            SELECT 1 FROM listings l
            JOIN offers o ON o.listing_id = l.id
            JOIN deals d ON d.offer_id = o.id
            WHERE l.retailer_id = tu.retailer_id AND l.url = tu.url
              AND d.status IN ('CANDIDATE', 'CONFIRMING', 'ACTIVE', 'HELD_REVIEW')
          )
        RETURNING tu.id
        """,
        (retailer_id, now),
    )
    return len(cur.fetchall())
