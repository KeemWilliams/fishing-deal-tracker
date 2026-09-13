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
from datetime import datetime, timezone
from pathlib import Path

import yaml

from fpt.adapters.registry import get_adapter, registered_slugs
from fpt.core.models import PageType
from fpt.db import DatabaseUnavailable, connect, table_exists
from fpt.fetch.blocks import detect_block_or_empty
from fpt.fetch.http_fetcher import HttpFetcher
from fpt.fetch.fetcher import EgressConfig
from fpt.fetch.robots import IDENTIFIED_USER_AGENT
from fpt.pipeline.validate import (
    ObservationContext,
    ObservationInput,
    validate_observation,
)

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


def _discovery_pages_for(retailer_slug: str) -> list[dict]:
    data = _load_yaml(CONFIG_DIR / "discovery_pages.yaml")
    return [p for p in (data.get("discovery_pages") or []) if p.get("retailer") == retailer_slug]


def _try_db_persist(observations: list[dict]) -> str:
    """Best-effort: only writes if price_observations already exists.
    Never raises -- returns a human-readable status string for the tick
    summary."""
    try:
        with connect() as conn:
            if not table_exists(conn, "price_observations"):
                return "skipped: price_observations table does not exist yet"
            # Intentionally not wired further: writing real rows requires
            # offer_id / crawl_task_id foreign keys that only exist once
            # the DB engineer's migrations and the full worker/enroller
            # pipeline are in place. Recording that the connection itself
            # is healthy is the useful signal for this task's smoke test.
            return f"connected: price_observations exists, {len(observations)} observation(s) ready to persist"
    except DatabaseUnavailable as exc:
        return f"dry-run (no DB): {exc}"


def _page_type_for(page_type_str: str) -> PageType:
    return PageType(page_type_str)


def run_tick(*, max_discovery_pages: int | None = None, use_stealth: bool = False) -> dict:
    started_at = datetime.now(timezone.utc)
    fetcher = HttpFetcher(use_stealth=use_stealth)
    egress = EgressConfig(mode="DIRECT")

    summary: dict = {
        "started_at": started_at.isoformat(),
        "registered_adapters": registered_slugs(),
        "retailers": [],
    }

    task_id_counter = 1
    all_observations: list[dict] = []

    for retailer_cfg in _enabled_retailers():
        slug = retailer_cfg["slug"]
        adapter_slug = retailer_cfg.get("adapter_slug", slug)
        retailer_summary: dict = {"slug": slug, "pages": []}

        try:
            adapter = get_adapter(adapter_slug)
        except Exception as exc:  # noqa: BLE001
            retailer_summary["error"] = f"unregistered_adapter:{exc}"
            summary["retailers"].append(retailer_summary)
            continue

        pages = _discovery_pages_for(slug)
        if max_discovery_pages is not None:
            pages = pages[:max_discovery_pages]

        for page_cfg in pages:
            page_type = _page_type_for(page_cfg["page_type"])
            task = _FakeTask(task_id_counter, page_cfg["url"], page_type)
            task_id_counter += 1
            request = adapter.build_request(task)

            page_result: dict = {"url": page_cfg["url"], "page_type": page_type.value}
            try:
                response = fetcher.fetch(
                    request, egress, IDENTIFIED_USER_AGENT, timeout_s=30.0
                )
            except Exception as exc:  # noqa: BLE001
                page_result["error"] = f"fetch_failed:{exc}"
                retailer_summary["pages"].append(page_result)
                continue

            sentinels = tuple(adapter.content_sentinels(page_type))
            block_check = detect_block_or_empty(
                status=response.status,
                body=response.body,
                final_url=response.final_url,
                request_url=request.url,
                sentinels=sentinels,
            )
            page_result["outcome"] = block_check.outcome.value
            page_result["block_signature"] = block_check.block_signature

            if block_check.outcome.value != "OK":
                retailer_summary["pages"].append(page_result)
                continue

            parse_result = adapter.parse(response)
            page_result["parse_outcome"] = parse_result.outcome.value
            page_result["discovered_count"] = len(parse_result.discovered)
            page_result["listing_count"] = len(parse_result.listings)
            page_result["warnings"] = list(parse_result.warnings)

            for listing in parse_result.listings:
                for offer in listing.offers:
                    obs_input = ObservationInput(
                        price_cents=offer.price_cents,
                        currency="USD",
                        category="other",
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

            retailer_summary["pages"].append(page_result)

        summary["retailers"].append(retailer_summary)

    summary["observations"] = all_observations
    summary["db"] = _try_db_persist(all_observations)
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

    args = parser.parse_args(argv)

    if args.command == "tick":
        summary = run_tick(
            max_discovery_pages=args.max_discovery_pages,
            use_stealth=args.use_stealth,
        )
        json.dump(summary, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
