"""Drains QUEUED `crawl_tasks` for one retailer within its rate-limit
budget (architecture doc 5.2 worker rules), fetching PRODUCT pages for
ENROLL/CONFIRM/HOT/BASELINE tasks and ingesting the result through
`fpt.store.pipeline.ingest_parsed_listing`.

This is the second half of the enroll -> confirm loop:
`fpt/scheduler/enroller.py` turns discovery rows into QUEUED ENROLL tasks
and `fpt/store/pipeline.py` enqueues a QUEUED CONFIRM task the moment a
CANDIDATE deal is created; this module is what actually performs those
fetches. There is no separate always-on worker process in this MVP (see
fpt/cli.py's stated scope) -- `fpt tick` calls this once per enabled
retailer, per run.

Rate limiting: `fpt/scheduler/limiter.py`'s `RetailerLimiter` is pure and
clock-injected. A single tick has no real elapsed wall-clock time between
successive fetches (nothing sleeps here, matching the rest of this
codebase's MVP style), so this module advances a local `clock` variable by
`limiter.next_delay_s()` after every fetch attempt -- this keeps the
limiter's admission math meaningful (a burst of N tasks in one tick still
respects `min_delay_s` between them) without an actual `time.sleep`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable
from urllib.parse import urlparse

from fpt.core.models import PageType, ResponseOutcome
from fpt.fetch.blocks import detect_block_or_empty
from fpt.fetch.redact import redact
from fpt.fetch.robots import RobotsGate
from fpt.fetch.url_safety import UnsafeUrlError, fetch_safely
from fpt.scheduler.limiter import RetailerLimiter
from fpt.store.pipeline import ingest_parsed_listing

# Task kinds this drainer will ever lease. DISCOVERY tasks are not leased
# here -- fpt/cli.py's tick still fetches discovery pages directly (see
# its own docstring) and calls fpt/scheduler/enroller.py inline; only the
# PRODUCT-page follow-up work (ENROLL/CONFIRM/HOT/BASELINE) goes through
# this queue.
_DRAINABLE_KINDS = ("CONFIRM", "ENROLL", "HOT", "BASELINE")


class _QueuedTask:
    """Minimal duck-typed `CrawlTask` (fpt/adapters/base.py's Protocol) so
    `adapter.build_request()` can be called against a leased queue row
    without a real scheduler object."""

    def __init__(self, task_id: int, url: str, page_type: PageType, params: dict):
        self.id = task_id
        self.url = url
        self.page_type = page_type
        self.params = params


def expire_overdue_confirm_tasks(cur, *, retailer_id: int, now: datetime) -> int:
    """Architecture 5.2 item 3 / 6.4: a CONFIRM task past its `deadline`
    that never got fetched EXPIRES, and the candidate it would have
    confirmed is REJECTED (`confirm_deadline_missed`) rather than left
    open forever. Migration 014's CHECK explicitly allows REJECTED without
    a confirming_observation_id for exactly this path."""
    cur.execute(
        """
        UPDATE crawl_tasks SET status = 'EXPIRED', finished_at = %s
        WHERE retailer_id = %s AND kind = 'CONFIRM' AND status = 'QUEUED' AND deadline < %s
        RETURNING deal_id
        """,
        (now, retailer_id, now),
    )
    deal_ids = [row[0] for row in cur.fetchall() if row[0] is not None]
    for deal_id in deal_ids:
        cur.execute(
            "UPDATE deals SET status = 'REJECTED', reject_reason = 'confirm_deadline_missed' "
            "WHERE id = %s AND status IN ('CANDIDATE', 'CONFIRMING')",
            (deal_id,),
        )
    return len(deal_ids)


@dataclass
class DrainResult:
    tasks: list[dict] = field(default_factory=list)


def drain_retailer_queue(
    conn,
    *,
    retailer_id: int,
    retailer_slug: str,
    base_url: str,
    adapter,
    fetcher,
    egress,
    limiter: RetailerLimiter,
    robots_gate: RobotsGate,
    allowed_hosts,
    user_agent: str,
    timeout_s: float,
    now: datetime,
    resolver=None,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_tasks: int = 50,
) -> DrainResult:
    cur = conn.cursor()
    expire_overdue_confirm_tasks(cur, retailer_id=retailer_id, now=now)

    result = DrainResult()
    clock = now

    for _ in range(max_tasks):
        cur.execute(
            """
            SELECT id, kind, page_type, url, params
            FROM crawl_tasks
            WHERE retailer_id = %s AND status = 'QUEUED' AND not_before <= %s
              AND kind = ANY(%s)
            ORDER BY priority ASC, not_before ASC
            LIMIT 1
            """,
            (retailer_id, clock, list(_DRAINABLE_KINDS)),
        )
        row = cur.fetchone()
        if row is None:
            break
        task_id, kind, page_type_value, url, params = row
        params = params or {}

        admitted, reason = limiter.can_admit(kind, now=clock)
        if not admitted:
            if reason in ("hourly_cap", "daily_cap"):
                # Non-reserve-eligible kinds (ENROLL/BASELINE) exhausted
                # their share -- SKIPPED_BUDGET, re-planned on a later
                # sweep (architecture 5.2 item 3). Try the next task; a
                # lower-priority one of a different kind may still fit.
                cur.execute(
                    "UPDATE crawl_tasks SET status = 'SKIPPED_BUDGET', finished_at = %s WHERE id = %s",
                    (clock, task_id),
                )
                result.tasks.append({"task_id": task_id, "kind": kind, "url": url, "status": "SKIPPED_BUDGET"})
                continue
            # breaker_open / disabled / delay_not_elapsed: nothing else
            # will succeed this tick for this retailer -- stop draining.
            break

        # Security review M2: robots.txt admission, checked for THIS
        # task's actual path, before every single fetch -- not just once
        # per retailer per tick.
        parsed_url = urlparse(url)
        path = parsed_url.path + (f"?{parsed_url.query}" if parsed_url.query else "")
        robots_check = robots_gate.admits(
            retailer_slug=retailer_slug, base_url=base_url, path=path or "/",
            fetcher=fetcher, egress=egress, user_agent=user_agent, timeout_s=timeout_s,
            allowed_hosts=allowed_hosts, resolver=resolver,
        )
        if not robots_check.allowed:
            cur.execute(
                "UPDATE crawl_tasks SET status = 'SKIPPED_ROBOTS', finished_at = %s WHERE id = %s",
                (clock, task_id),
            )
            result.tasks.append(
                {"task_id": task_id, "kind": kind, "url": url, "status": "SKIPPED_ROBOTS", "reason": robots_check.reason}
            )
            continue

        cur.execute("UPDATE crawl_tasks SET status = 'LEASED' WHERE id = %s", (task_id,))
        page_type = PageType(page_type_value)
        task = _QueuedTask(task_id, url, page_type, params)
        request = adapter.build_request(task)

        try:
            # Security review M3: SSRF guard on both the request URL and
            # the fetch's final_url (after any redirects).
            response = fetch_safely(
                fetcher, request, egress, user_agent, timeout_s,
                allowed_hosts=allowed_hosts, resolver=resolver,
            )
        except UnsafeUrlError as exc:
            cur.execute(
                "UPDATE crawl_tasks SET status = 'FAILED', finished_at = %s, attempts = attempts + 1 WHERE id = %s",
                (clock, task_id),
            )
            result.tasks.append(
                {"task_id": task_id, "kind": kind, "url": url, "status": "FAILED", "error": f"unsafe_url:{exc.reason}"}
            )
            delay = limiter.next_delay_s()
            sleep_fn(delay)
            clock = clock + timedelta(seconds=delay)
            continue
        except Exception as exc:  # noqa: BLE001 - a transport failure must not abort the tick
            cur.execute(
                "UPDATE crawl_tasks SET status = 'FAILED', finished_at = %s, attempts = attempts + 1 WHERE id = %s",
                (clock, task_id),
            )
            limiter.record_request(now=clock)
            result.tasks.append({"task_id": task_id, "kind": kind, "url": url, "status": "FAILED", "error": redact(str(exc))})
            delay = limiter.next_delay_s()
            sleep_fn(delay)
            clock = clock + timedelta(seconds=delay)
            continue

        limiter.record_request(now=clock)

        sentinels = tuple(adapter.content_sentinels(page_type))
        block_check = detect_block_or_empty(
            status=response.status, body=response.body, final_url=response.final_url,
            request_url=request.url, sentinels=sentinels,
        )
        limiter.record_outcome(blocked=block_check.outcome == ResponseOutcome.BLOCKED, now=clock)

        task_result = {"task_id": task_id, "kind": kind, "url": url, "outcome": block_check.outcome.value}

        if block_check.outcome != ResponseOutcome.OK:
            # BLOCKED/EMPTY/etc: never parsed, never persisted (same guard
            # as fpt/cli.py's discovery-page loop and fpt/store/pipeline.py's
            # scope note) -- just record the fetch attempt.
            cur.execute(
                "UPDATE crawl_tasks SET status = 'DONE', outcome = %s, finished_at = %s, attempts = attempts + 1 WHERE id = %s",
                (block_check.outcome.value, clock, task_id),
            )
            result.tasks.append(task_result)
            delay = limiter.next_delay_s()
            sleep_fn(delay)
            clock = clock + timedelta(seconds=delay)
            continue

        # Robustness review M4: a hostile or malformed page (deeply nested
        # JSON/HTML, a pathological regex target) must never crash the
        # whole tick -- one bad PRODUCT page is a FAILED task, not a
        # process-ending exception.
        try:
            parse_result = adapter.parse(response)
        except RecursionError:
            cur.execute(
                "UPDATE crawl_tasks SET status = 'FAILED', outcome = 'STRUCTURE_CHANGED', finished_at = %s, "
                "attempts = attempts + 1 WHERE id = %s",
                (clock, task_id),
            )
            result.tasks.append({"task_id": task_id, "kind": kind, "url": url, "status": "FAILED", "error": "recursion_limit_exceeded"})
            delay = limiter.next_delay_s()
            sleep_fn(delay)
            clock = clock + timedelta(seconds=delay)
            continue
        except Exception as exc:  # noqa: BLE001 - same rationale as above
            cur.execute(
                "UPDATE crawl_tasks SET status = 'FAILED', outcome = 'STRUCTURE_CHANGED', finished_at = %s, "
                "attempts = attempts + 1 WHERE id = %s",
                (clock, task_id),
            )
            result.tasks.append({"task_id": task_id, "kind": kind, "url": url, "status": "FAILED", "error": redact(str(exc))})
            delay = limiter.next_delay_s()
            sleep_fn(delay)
            clock = clock + timedelta(seconds=delay)
            continue

        task_result["listing_count"] = len(parse_result.listings)
        category = params.get("category_hint", "other")
        ingested = []
        for listing in parse_result.listings:
            ingest_outcome = ingest_parsed_listing(
                conn, retailer_id=retailer_id, retailer_slug=retailer_slug, category=category,
                listing=listing, response=response, adapter_version=adapter.adapter_version,
                task_kind=kind, crawl_task_id=task_id,
            )
            ingested.append(
                {
                    "listing_id": ingest_outcome.listing_id,
                    "observations": len(ingest_outcome.observations),
                    "deal_actions": [a.kind for a in ingest_outcome.deal_actions],
                }
            )
        task_result["ingested"] = ingested

        cur.execute(
            "UPDATE crawl_tasks SET status = 'DONE', outcome = 'OK', finished_at = %s, attempts = attempts + 1 WHERE id = %s",
            (clock, task_id),
        )
        result.tasks.append(task_result)
        delay = limiter.next_delay_s()
        sleep_fn(delay)
        clock = clock + timedelta(seconds=delay)

    return result
