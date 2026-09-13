"""CLI entrypoint. `python -m fpt.cli tick` runs one scheduler tick.

Scope note: this task owns the scraper/adapter/deal-engine core, not the
full DB-backed scheduler (crawl_tasks, tick_runs, etc. -- architecture doc
4.1 tables, owned by the database engineer). `tick` therefore runs a
single, self-contained pass over `config/discovery_pages.yaml` and prints
a structured summary (discovered items, parsed listings, validated
observations) to stdout. If `DATABASE_URL` is set and the expected tables
already exist, it also persists price_observations -- otherwise it prints
a warning and continues in dry-run mode. This keeps the CLI runnable and
testable today, and gives the DB layer a stable seam (`fpt/db.py`,
`fpt.pipeline.validate`) to attach real persistence to without changing
this module's public behavior.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import yaml

from fpt.adapters.registry import get_adapter, registered_slugs
from fpt.core.models import PageType
from fpt.db import DatabaseUnavailable, connect, table_exists
from fpt.export.runner import run_export
from fpt.export.storage import build_storage_from_env
from fpt.fetch.blocks import detect_block_or_empty
from fpt.fetch.fixture_fetcher import ClockedFixtureFetcher, FixtureFetcher
from fpt.fetch.http_fetcher import HttpFetcher
from fpt.fetch.fetcher import EgressConfig
from fpt.fetch.redact import redact
from fpt.fetch.robots import IDENTIFIED_USER_AGENT, RobotsGate
from fpt.fetch.url_safety import UnsafeUrlError, check_url_safety, fetch_safely
from fpt.pipeline.validate import (
    ObservationContext,
    ObservationInput,
    validate_observation,
)
from fpt.scheduler import enroller
from fpt.scheduler.limiter import BreakerPolicy, RetailerLimiter, RetailerPolicy, effective_min_delay_s
from fpt.scheduler.queue import drain_retailer_queue
from fpt.store.observations import get_or_create_crawl_task
from fpt.store.pipeline import ingest_parsed_listing

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class _FakeTask:
    def __init__(self, task_id: int, url: str, page_type: PageType):
        self.id = task_id
        self.url = url
        self.page_type = page_type
        self.params: dict = {}


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _enabled_retailers() -> list[dict]:
    data = _load_yaml(CONFIG_DIR / "retailers.yaml")
    return [r for r in (data.get("retailers") or []) if r.get("enabled")]


def _discovery_pages_from_db(conn, *, retailer_id: int, now: datetime) -> list[dict]:
    """Loads this retailer's discovery pages from the DB `discovery_pages`
    table -- the authoritative, migration-seeded source (db/migrations/
    012-019_seed_*.sql seed 20 of the 23 configured retailers; see
    db/migrations/004_discovery.up.sql for the table itself).

    A page is "due" when it has never been swept (`last_swept_at IS NULL`
    -- always due on a fresh DB) or its last sweep plus its own
    `interval_minutes` has elapsed by `now`. `discovery_pages.next_due_at`
    (migration 004) is intentionally NOT used for this check: nothing in
    this codebase ever advances it past its `DEFAULT now()` insert-time
    value, so gating on it would make every page "due" forever after
    being seeded regardless of `interval_minutes` -- the same silent
    bug shape this fix is closing, just moved one column over."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT page_type, url, category_hint, interval_minutes, max_pages, last_swept_at
            FROM discovery_pages
            WHERE retailer_id = %s AND enabled
            ORDER BY id
            """,
            (retailer_id,),
        )
        rows = cur.fetchall()

    due_pages: list[dict] = []
    for page_type, url, category_hint, interval_minutes, max_pages, last_swept_at in rows:
        if last_swept_at is not None and now < last_swept_at + timedelta(minutes=interval_minutes):
            continue
        due_pages.append(
            {
                "page_type": page_type,
                "url": url,
                "category_hint": category_hint,
                "interval_minutes": interval_minutes,
                "max_pages": max_pages,
            }
        )
    return due_pages


def _discovery_pages_for(
    retailer_slug: str,
    *,
    conn=None,
    retailer_id: int | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """Discovery pages for one retailer.

    Bug fixed here (found while investigating a real tick persisting zero
    rows): this used to read ONLY `config/discovery_pages.yaml`, which
    has only ever carried tackle_warehouse's four pages. Every other
    retailer's discovery pages were instead seeded straight into the DB
    `discovery_pages` table by db/migrations/012-019_seed_*.sql (a live
    DB has rows for 20 of 23 configured retailers) -- so for ~19 enabled
    retailers `fpt tick` silently got `pages=[]` and did nothing, every
    tick, forever, with no error.

    The DB is now the authoritative source whenever one is available and
    this retailer has been resolved to a DB id (`conn` and `retailer_id`
    both given): see `_discovery_pages_from_db`. Falls back to the YAML
    file otherwise -- no live DB (dry-run / `--fixtures-manifest` /
    offline runs) or a retailer that hasn't been seeded into `retailers`
    yet -- which keeps existing offline behavior and tests unchanged.
    """
    if conn is not None and retailer_id is not None:
        return _discovery_pages_from_db(conn, retailer_id=retailer_id, now=now or datetime.now(timezone.utc))
    data = _load_yaml(CONFIG_DIR / "discovery_pages.yaml")
    return [p for p in (data.get("discovery_pages") or []) if p.get("retailer") == retailer_slug]


def _page_type_for(page_type_str: str) -> PageType:
    return PageType(page_type_str)


def _resolve_retailer_id(conn, slug: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM retailers WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return row[0] if row else None


def _build_retailer_policy(policy_cfg: dict) -> RetailerPolicy:
    breaker_cfg = policy_cfg.get("breaker", {})
    return RetailerPolicy(
        min_delay_s=policy_cfg.get("min_delay_s", 10),
        jitter_s=policy_cfg.get("jitter_s", 5),
        max_requests_per_hour=policy_cfg.get("max_requests_per_hour", 90),
        max_requests_per_day=policy_cfg.get("max_requests_per_day", 1200),
        reserved_share=dict(policy_cfg.get("reserved_share", {})),
        breaker=BreakerPolicy(
            consecutive_blocks=breaker_cfg.get("consecutive_blocks", 5),
            block_rate_threshold=breaker_cfg.get("block_rate_threshold", 0.20),
            block_rate_min_sample=breaker_cfg.get("block_rate_min_sample", 20),
            cooldown_hours=breaker_cfg.get("cooldown_hours", 12),
            disable_after_trips_in_48h=breaker_cfg.get("disable_after_trips_in_48h", 2),
        ),
    )


def build_fixture_fetcher(manifest_path: str, *, now: datetime) -> ClockedFixtureFetcher:
    """First-class no-network path for `fpt tick --fixtures-manifest`
    (flagged missing by the TEST phase). See fpt/fetch/fixture_fetcher.py."""
    return ClockedFixtureFetcher(FixtureFetcher.from_manifest(manifest_path), now=now)


def run_tick(
    *,
    max_discovery_pages: int | None = None,
    use_stealth: bool = False,
    fetcher=None,
    now: datetime | None = None,
    resolver=None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict:
    # Looked up on `time` at call time (not bound as a default parameter
    # value) so `monkeypatch.setattr(cli_module.time, "sleep", ...)` in
    # tests -- the only way to intercept sleeping through `main()`, which
    # exposes no CLI flag for this -- actually takes effect. A default
    # bound at `def` time would capture the original `time.sleep` function
    # object once and ignore any later monkeypatch of the module attribute.
    sleep_fn = sleep_fn or time.sleep
    started_at = now or datetime.now(timezone.utc)
    injected_fetcher = fetcher is not None
    egress = EgressConfig(mode="DIRECT")
    robots_gate = RobotsGate()

    summary: dict = {
        "started_at": started_at.isoformat(),
        "registered_adapters": registered_slugs(),
        "retailers": [],
    }

    task_id_counter = 1
    all_observations: list[dict] = []
    deal_actions: list[dict] = []
    persisted_count = 0

    def _process_retailers(conn) -> None:
        nonlocal task_id_counter, persisted_count

        for retailer_cfg in _enabled_retailers():
            slug = retailer_cfg["slug"]
            adapter_slug = retailer_cfg.get("adapter_slug", slug)
            base_url = retailer_cfg.get("base_url", "")
            allowed_hosts = retailer_cfg.get("allowed_hosts") or []
            retailer_summary: dict = {"slug": slug, "pages": []}

            if not allowed_hosts:
                # Security review M3: no allowlist configured means no URL
                # for this retailer can ever pass `check_url_safety` -- fail
                # loud rather than silently fetching an unvalidated URL.
                retailer_summary["error"] = "no_allowed_hosts_configured"
                summary["retailers"].append(retailer_summary)
                continue

            try:
                adapter = get_adapter(adapter_slug)
            except Exception as exc:  # noqa: BLE001
                retailer_summary["error"] = f"unregistered_adapter:{redact(str(exc))}"
                summary["retailers"].append(retailer_summary)
                continue

            # Security review M2: `--use-stealth` alone is never enough --
            # it must be combined with this retailer's own
            # `stealth_approved: true` (default false for every retailer).
            # An injected fetcher (tests, `--fixtures-manifest`) bypasses
            # this entirely since it never touches a real network client.
            if injected_fetcher:
                retailer_fetcher = fetcher
            else:
                retailer_stealth = use_stealth and bool(retailer_cfg.get("stealth_approved", False))
                retailer_fetcher = HttpFetcher(use_stealth=retailer_stealth)

            retailer_id = _resolve_retailer_id(conn, slug) if conn is not None else None
            if conn is not None and retailer_id is None:
                retailer_summary["warning"] = "retailer not seeded in DB (see db/migrations/012); persistence skipped"

            policy_cfg = dict(retailer_cfg.get("policy", {}))
            crawl_delay = robots_gate.crawl_delay(
                retailer_slug=slug, base_url=base_url, fetcher=retailer_fetcher, egress=egress,
                user_agent=IDENTIFIED_USER_AGENT, timeout_s=30.0, allowed_hosts=allowed_hosts, resolver=resolver,
            )
            policy_cfg["min_delay_s"] = effective_min_delay_s(policy_cfg.get("min_delay_s", 10), crawl_delay)
            limiter = RetailerLimiter(_build_retailer_policy(policy_cfg))
            clock = started_at

            pages = _discovery_pages_for(slug, conn=conn, retailer_id=retailer_id, now=clock)
            if max_discovery_pages is not None:
                pages = pages[:max_discovery_pages]

            for page_cfg in pages:
                page_type = _page_type_for(page_cfg["page_type"])
                category = page_cfg.get("category_hint", "other")
                page_result: dict = {"url": page_cfg["url"], "page_type": page_type.value}

                url = page_cfg["url"]
                pages_fetched = 0
                page_max = max(1, int(page_cfg.get("max_pages", 1)))

                while url and pages_fetched < page_max:
                    pages_fetched += 1

                    parsed_url = urlparse(url)
                    path = parsed_url.path + (f"?{parsed_url.query}" if parsed_url.query else "")
                    robots_check = robots_gate.admits(
                        retailer_slug=slug, base_url=base_url, path=path or "/", fetcher=retailer_fetcher,
                        egress=egress, user_agent=IDENTIFIED_USER_AGENT, timeout_s=30.0,
                        allowed_hosts=allowed_hosts, resolver=resolver,
                    )
                    if not robots_check.allowed:
                        page_result["outcome"] = "SKIPPED_ROBOTS"
                        page_result["robots_reason"] = robots_check.reason
                        break

                    admitted, deny_reason = limiter.can_admit("DISCOVERY", now=clock)
                    if not admitted:
                        page_result["outcome"] = "SKIPPED_BUDGET"
                        page_result["budget_reason"] = deny_reason
                        break

                    task = _FakeTask(task_id_counter, url, page_type)
                    task_id_counter += 1
                    request = adapter.build_request(task)

                    try:
                        response = fetch_safely(
                            retailer_fetcher, request, egress, IDENTIFIED_USER_AGENT, timeout_s=30.0,
                            allowed_hosts=allowed_hosts, resolver=resolver,
                        )
                    except UnsafeUrlError as exc:
                        page_result["error"] = f"unsafe_url:{exc.reason}"
                        break
                    except Exception as exc:  # noqa: BLE001
                        page_result["error"] = f"fetch_failed:{redact(str(exc))}"
                        break

                    limiter.record_request(now=clock)

                    sentinels = tuple(adapter.content_sentinels(page_type))
                    block_check = detect_block_or_empty(
                        status=response.status,
                        body=response.body,
                        final_url=response.final_url,
                        request_url=request.url,
                        sentinels=sentinels,
                    )
                    limiter.record_outcome(blocked=block_check.outcome.value == "BLOCKED", now=clock)
                    page_result["outcome"] = block_check.outcome.value
                    page_result["block_signature"] = block_check.block_signature

                    delay = limiter.next_delay_s()
                    sleep_fn(delay)
                    clock = clock + timedelta(seconds=delay)

                    # BLOCKED/EMPTY/etc: never parsed, never persisted -- no
                    # observation or deal is ever attempted from a non-OK fetch.
                    if block_check.outcome.value != "OK":
                        break

                    # Robustness review M4: a hostile/malformed page must
                    # never crash the whole tick.
                    try:
                        parse_result = adapter.parse(response)
                    except RecursionError:
                        page_result["error"] = "recursion_limit_exceeded"
                        break
                    except Exception as exc:  # noqa: BLE001
                        page_result["error"] = f"parse_failed:{redact(str(exc))}"
                        break

                    page_result["parse_outcome"] = parse_result.outcome.value
                    page_result["discovered_count"] = page_result.get("discovered_count", 0) + len(parse_result.discovered)
                    page_result["listing_count"] = page_result.get("listing_count", 0) + len(parse_result.listings)
                    page_result["warnings"] = list(parse_result.warnings)

                    # `parse_result.listings` is only ever populated for
                    # PRODUCT (or API_BATCH) pages -- a CLEARANCE_LISTING/
                    # USED_LISTING grid's rows come back as
                    # `parse_result.discovered` instead (architecture doc
                    # 3.1: a grid row is never an observation on its own).
                    # Enroll every discovered row into discovery_hits/
                    # tracked_urls and (for new or fast-track items) a
                    # QUEUED ENROLL crawl_task -- fpt/scheduler/queue.py
                    # drains those PRODUCT-page fetches after this pages
                    # loop, in the SAME tick, which is what actually
                    # produces listings/observations/deals from a real
                    # discovery sweep.
                    if conn is not None and retailer_id is not None and parse_result.discovered:
                        enroll_cur = conn.cursor()
                        discovery_page_id = enroller.get_or_create_discovery_page(
                            enroll_cur, retailer_id=retailer_id, page_type=page_type, url=page_cfg["url"],
                            category_hint=category, interval_minutes=page_cfg.get("interval_minutes", 1440),
                            max_pages=page_cfg.get("max_pages", 1), seen_at=response.fetched_at,
                            item_count=len(parse_result.discovered),
                        )
                        disc_crawl_task_id = get_or_create_crawl_task(
                            enroll_cur, retailer_id=retailer_id, kind="DISCOVERY", page_type=page_type,
                            url=url, dedupe_key=f"DISCOVERY:{slug}:{url}:{response.snapshot_ref}",
                        )
                        enroll_outcome = enroller.enroll_discovery_page(
                            enroll_cur, retailer_id=retailer_id, discovery_page_id=discovery_page_id,
                            page_type=page_type, category_hint=category, crawl_task_id=disc_crawl_task_id,
                            discovered=parse_result.discovered, seen_at=response.fetched_at,
                            tracked_url_cap=retailer_cfg.get("tracked_url_cap"),
                            allowed_hosts=allowed_hosts, resolver=resolver,
                        )
                        page_result["enrolled"] = {
                            "hits": len(enroll_outcome.discovery_hit_ids),
                            "tasks_enqueued": enroll_outcome.tasks_enqueued,
                            "items_capped": enroll_outcome.items_capped,
                            "items_unsafe": enroll_outcome.items_unsafe,
                            "items_skipped": enroll_outcome.items_skipped,
                        }

                    next_url = parse_result.next_page_url
                    if next_url:
                        # Security review M3: pagination follows a URL the
                        # RETAILER supplied in its own response -- validate
                        # it exactly like any other outbound URL before
                        # ever building a request from it.
                        next_safety = check_url_safety(next_url, allowed_hosts=allowed_hosts, resolver=resolver)
                        if not next_safety.allowed:
                            page_result.setdefault("warnings", []).append(f"next_page_url_unsafe:{next_safety.reason}")
                            next_url = None

                    for listing in parse_result.listings:
                        if conn is not None and retailer_id is not None:
                            outcome = ingest_parsed_listing(
                                conn,
                                retailer_id=retailer_id,
                                retailer_slug=slug,
                                category=category,
                                listing=listing,
                                response=response,
                                adapter_version=adapter.adapter_version,
                            )
                            persisted_count += len(outcome.observations)
                            for offer, recorded in zip(listing.offers, outcome.observations):
                                all_observations.append(
                                    {
                                        "retailer": slug,
                                        "retailer_sku": listing.retailer_sku,
                                        "price_cents": offer.price_cents,
                                        "condition": offer.condition.value,
                                        "quality": recorded.quality,
                                        "reasons": recorded.reasons,
                                        "was_duplicate": recorded.was_duplicate,
                                    }
                                )
                            for action in outcome.deal_actions:
                                if action.kind != "none":
                                    deal_actions.append(
                                        {
                                            "retailer": slug,
                                            "retailer_sku": listing.retailer_sku,
                                            "kind": action.kind,
                                            "deal_id": action.deal_id,
                                            "confirm_status": action.confirm_status,
                                        }
                                    )
                        else:
                            # No live DB (or retailer not yet seeded): validate
                            # only, matching the previous dry-run behavior.
                            for offer in listing.offers:
                                obs_input = ObservationInput(
                                    price_cents=offer.price_cents,
                                    currency="USD",
                                    category=category,
                                    claimed_reference_cents=offer.claimed_reference_cents,
                                    on_clearance=offer.on_clearance,
                                    unit_count=offer.unit_count,
                                    variant_key_observed=None,
                                    availability=offer.availability,
                                    condition_wording_mapped=offer.condition is not None,
                                    is_used_page_type=page_type == PageType.USED_LISTING,
                                )
                                result = validate_observation(obs_input, ObservationContext())
                                all_observations.append(
                                    {
                                        "retailer": slug,
                                        "retailer_sku": listing.retailer_sku,
                                        "price_cents": offer.price_cents,
                                        "condition": offer.condition.value,
                                        "quality": result.quality,
                                        "reasons": result.reasons,
                                    }
                                )

                    url = next_url

                retailer_summary["pages"].append(page_result)

            # Drain this retailer's QUEUED ENROLL/CONFIRM/HOT/BASELINE
            # crawl_tasks within its rate-limit budget -- this is what
            # actually performs the product-page fetches the enroller (and
            # fpt/store/pipeline.py, on candidate detection) queued above,
            # in the SAME tick where doing so respects the confirmation gap
            # (a CONFIRM task's `not_before` is never before this tick's
            # `now`, so it is only ever leased on a LATER tick).
            if conn is not None and retailer_id is not None:
                drain_result = drain_retailer_queue(
                    conn, retailer_id=retailer_id, retailer_slug=slug, base_url=base_url, adapter=adapter,
                    fetcher=retailer_fetcher, egress=egress, limiter=limiter, robots_gate=robots_gate,
                    allowed_hosts=allowed_hosts, user_agent=IDENTIFIED_USER_AGENT, timeout_s=30.0,
                    now=clock, resolver=resolver, sleep_fn=sleep_fn,
                )
                retailer_summary["queue"] = drain_result.tasks
                for task_result in drain_result.tasks:
                    for ingested in task_result.get("ingested", []):
                        persisted_count += ingested["observations"]
                        for kind in ingested["deal_actions"]:
                            if kind != "none":
                                deal_actions.append(
                                    {"retailer": slug, "kind": kind, "source": task_result["kind"]}
                                )

            summary["retailers"].append(retailer_summary)

    db_status: str
    try:
        with connect() as conn:
            if not table_exists(conn, "price_observations"):
                db_status = "skipped: price_observations table does not exist yet"
                _process_retailers(None)
            else:
                _process_retailers(conn)
                db_status = f"connected: persisted {persisted_count} observation(s)"
    except DatabaseUnavailable as exc:
        db_status = f"dry-run (no DB): {exc}"
        _process_retailers(None)

    summary["observations"] = all_observations
    summary["deal_actions"] = deal_actions
    summary["db"] = db_status
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fpt", description="Fishing Gear Deal Tracker CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    tick_parser = subparsers.add_parser("tick", help="Run one scheduler tick")
    tick_parser.add_argument(
        "--max-discovery-pages",
        type=int,
        default=None,
        help="Limit discovery pages fetched per retailer (smoke-test use)",
    )
    tick_parser.add_argument(
        "--use-stealth",
        action="store_true",
        help="Escalate to StealthyFetcher (never enabled by default; requires owner approval, see architecture doc 7.3/A4)",
    )
    tick_parser.add_argument(
        "--fixtures-manifest",
        default=None,
        help="Path to a JSON {url: file_path} manifest -- runs the tick with zero network access, "
             "serving fixture bytes instead (see fpt/fetch/fixture_fetcher.py). Never contacts a real retailer.",
    )
    tick_parser.add_argument(
        "--now",
        default=None,
        help="ISO-8601 timestamp to treat as 'now' for this tick (offline/testing use only -- "
             "lets --fixtures-manifest runs simulate the passage of time between ticks without sleeping).",
    )

    export_parser = subparsers.add_parser(
        "export", help="Build and publish the public deals feed (architecture doc 3.4)"
    )
    export_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and validate the feed but do not write to storage",
    )
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the integrity gate (all-retailers-stale check) and promote unconditionally",
    )

    args = parser.parse_args(argv)

    if args.command == "tick":
        tick_now = datetime.fromisoformat(args.now) if args.now else None
        tick_fetcher = None
        if args.fixtures_manifest:
            tick_fetcher = build_fixture_fetcher(args.fixtures_manifest, now=tick_now or datetime.now(timezone.utc))
        summary = run_tick(
            max_discovery_pages=args.max_discovery_pages,
            use_stealth=args.use_stealth,
            fetcher=tick_fetcher,
            now=tick_now,
        )
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    if args.command == "export":
        try:
            with connect() as conn:
                storage = build_storage_from_env()
                outcome = run_export(conn, storage, force=args.force, dry_run=args.dry_run)
        except DatabaseUnavailable as exc:
            json.dump({"error": f"database_unavailable: {exc}"}, sys.stdout, indent=2)
            sys.stdout.write("\n")
            return 1

        json.dump(
            {
                "export_id": outcome.export_id,
                "promoted": outcome.promoted,
                "gate_failures": outcome.gate_failures,
                "counts": outcome.counts,
                "files_written": outcome.files_written,
            },
            sys.stdout,
            indent=2,
            default=str,
        )
        sys.stdout.write("\n")
        return 0 if outcome.promoted or args.dry_run else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
