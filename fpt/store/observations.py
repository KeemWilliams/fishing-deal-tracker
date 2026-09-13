"""Crawl task + price_observations persistence (db/migrations 006, 007).

Two things this module owns that were entirely missing before (HANDOFF
CRITICAL-1):
  1. `observed_date_et`: price_observations.observed_date_et is NOT NULL
     and is the ET calendar day the reference resolver groups by (section
     6.1's "prior 90 days" and offer_daily_price are both ET-day grained).
     Nothing anywhere converted `observed_at` (always UTC, per FetchResponse)
     to an America/New_York date before this.
  2. `task_kind`: price_observations.task_kind is NOT NULL and must be one
     of CONFIRM/HOT/DISCOVERY/ENROLL/BASELINE/CANARY. There is no real
     crawl-task scheduler yet (that's the DB-backed scheduler explicitly
     out of fpt/cli.py's stated scope), so `crawl_tasks` rows are created
     here as a minimal, honest record of "a fetch happened for this
     purpose" -- not a full priority/lease queue.

Idempotency: `dedupe_key` on record_observation() is checked against
existing price_observations for the SAME offer with the SAME
`snapshot_ref` (the fetch's own identity, per FetchResponse.snapshot_ref)
before anything is written, so replaying the same fetch twice (e.g. a
retried tick, or a test re-running the same fixture through the pipeline)
never double-inserts an observation or a crawl_task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from fpt.adapters.base import PageType
from fpt.pipeline.validate import ValidationResult

_EASTERN = ZoneInfo("America/New_York")

# MVP heuristic mapping in the absence of a real scheduler: a listing/used
# page is DISCOVERY work (grid rows only -- see pipeline.py, this never
# backs an observation on its own); a product page defaults to BASELINE
# unless the caller overrides task_kind explicitly (e.g. "CONFIRM" for a
# second fetch of the same product page during confirmation).
DEFAULT_TASK_KIND_BY_PAGE_TYPE: dict[PageType, str] = {
    PageType.PRODUCT: "BASELINE",
    PageType.CLEARANCE_LISTING: "DISCOVERY",
    PageType.USED_LISTING: "DISCOVERY",
    PageType.CATALOG_LISTING: "DISCOVERY",
    PageType.API_BATCH: "BASELINE",
}


def default_task_kind(page_type: PageType) -> str:
    return DEFAULT_TASK_KIND_BY_PAGE_TYPE.get(page_type, "BASELINE")


def get_or_create_crawl_task(
    cur,
    *,
    retailer_id: int,
    kind: str,
    page_type: PageType | str,
    url: str,
    dedupe_key: str,
    priority: int = 30,
    status: str = "DONE",
    outcome: str | None = "OK",
) -> int:
    """`dedupe_key` is constructed by the caller (fpt/store/pipeline.py) to
    include the fetch's own `snapshot_ref`, so the same fetch replayed
    twice (a retried tick, or a test re-running the same fixture) reuses
    the same crawl_task row instead of creating a new one -- this is part
    of the "same fetch twice must not double-insert" idempotency
    guarantee, alongside the observation-level dedupe in
    `record_observation`."""
    cur.execute("SELECT id FROM crawl_tasks WHERE dedupe_key = %s ORDER BY id DESC LIMIT 1", (dedupe_key,))
    existing = cur.fetchone()
    if existing is not None:
        return existing[0]

    page_type_value = page_type.value if isinstance(page_type, PageType) else page_type
    cur.execute(
        """
        INSERT INTO crawl_tasks (retailer_id, kind, priority, page_type, url, dedupe_key, status, outcome, finished_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
        RETURNING id
        """,
        (retailer_id, kind, priority, page_type_value, url, dedupe_key, status, outcome),
    )
    return cur.fetchone()[0]


def find_existing_observation_by_snapshot(cur, *, offer_id: int, snapshot_ref: str) -> int | None:
    cur.execute(
        "SELECT id FROM price_observations WHERE offer_id = %s AND snapshot_ref = %s LIMIT 1",
        (offer_id, snapshot_ref),
    )
    row = cur.fetchone()
    return row[0] if row else None


@dataclass(frozen=True)
class RecordedObservation:
    observation_id: int
    quality: str
    reasons: list[str] = field(default_factory=list)
    was_duplicate: bool = False


def record_observation(
    cur,
    *,
    offer_id: int,
    crawl_task_id: int,
    task_kind: str,
    observed_at: datetime,
    price_cents: int | None,
    shipping_cents: int | None,
    claimed_reference_cents: int | None,
    claimed_reference_kind: str | None,
    on_clearance: bool,
    availability: str,
    stock_qty: int | None,
    stock_qty_is_floor: bool,
    restock_date,
    unit_count: int | None,
    variant_key_observed: str | None,
    validation: ValidationResult,
    adapter_version: str,
    snapshot_ref: str,
    currency: str = "USD",
) -> RecordedObservation:
    """Insert one price_observations row, deduped on (offer_id,
    snapshot_ref). `currency` is always written (migration 014 added the
    column) -- there is no TODO/skip path here."""
    existing_id = find_existing_observation_by_snapshot(cur, offer_id=offer_id, snapshot_ref=snapshot_ref)
    if existing_id is not None:
        return RecordedObservation(
            observation_id=existing_id, quality=validation.quality, reasons=validation.reasons, was_duplicate=True
        )

    observed_date_et = observed_at.astimezone(_EASTERN).date()
    cur.execute(
        """
        INSERT INTO price_observations (
            offer_id, crawl_task_id, task_kind, observed_at, observed_date_et,
            price_cents, shipping_cents, claimed_reference_cents, claimed_reference_kind,
            on_clearance, availability, stock_qty, stock_qty_is_floor, restock_date,
            unit_count, variant_key_observed, quality, quality_reasons,
            adapter_version, snapshot_ref, currency
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            offer_id,
            crawl_task_id,
            task_kind,
            observed_at,
            observed_date_et,
            price_cents,
            shipping_cents,
            claimed_reference_cents,
            claimed_reference_kind,
            on_clearance,
            availability,
            stock_qty,
            stock_qty_is_floor,
            restock_date,
            unit_count,
            variant_key_observed,
            validation.quality,
            validation.reasons,
            adapter_version,
            snapshot_ref,
            currency,
        ),
    )
    observation_id = cur.fetchone()[0]
    return RecordedObservation(observation_id=observation_id, quality=validation.quality, reasons=validation.reasons)
