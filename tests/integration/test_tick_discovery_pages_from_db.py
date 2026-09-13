"""Regression test for a real bug found in this task: `fpt tick` used to
read discovery pages ONLY from `config/discovery_pages.yaml`, which has
ever only carried tackle_warehouse's four pages. Every other retailer's
discovery pages were instead seeded straight into the DB `discovery_pages`
table by db/migrations/012-019_seed_*.sql, so a real tick against a real
Postgres silently discovered zero pages -- and therefore persisted zero
discovery_hits/crawl_tasks/price_observations/deals -- for ~19 of the 20
enabled retailers, forever, with no error surfaced anywhere.

This test deliberately does NOT monkeypatch `fpt.cli._discovery_pages_for`
(the tackle_warehouse e2e test in test_tick_enroll_confirm_e2e.py does,
for speed/focus) -- the whole point here is to exercise the real DB-backed
code path added by the fix and prove it against the retailer/page row the
seed migrations actually put in Postgres, using academy's own adapter and
its own real fixture (tests/fixtures/academy/clearance_listing.html, which
independently-verified adapter tests already assert parses into 5
discovered items at exactly this URL).

Requires DATABASE_URL pointing at a throwaway Postgres with all
db/migrations/*.up.sql applied. See tests/integration/conftest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fpt.cli as cli_module
from fpt.core.models import FetchResponse

FIXTURES = Path(__file__).parent.parent / "fixtures" / "academy"

_ACADEMY_CLEARANCE_URL = (
    "https://www.academy.com/c/academy-clearance/outdoors-clearance--1"
    "/fishing-clearance-items/rods-reels-clearance--1"
)
_ROBOTS_ALLOW_ALL = b"User-agent: *\nDisallow:\n"


def _fake_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def _academy_retailer_cfg() -> dict:
    return {
        "slug": "academy",
        "name": "Academy Sports + Outdoors",
        "base_url": "https://www.academy.com",
        "adapter_slug": "academy",
        "enabled": True,
        "allowed_hosts": ["www.academy.com"],
        "stealth_approved": False,
        "policy": {
            "min_delay_s": 0.001,
            "jitter_s": 0,
            "max_requests_per_hour": 9000,
            "max_requests_per_day": 9000,
            "reserved_share": {"CONFIRM": 0.15, "HOT": 0.15},
        },
        "egress": {"mode": "DIRECT", "proxy_url_env": None, "api_key_env": None},
        "tracked_url_cap": 2500,
    }


class _ManifestFetcher:
    """Fetcher stand-in serving fixed bytes per exact URL -- no network,
    ever. Product-page URLs discovered from the clearance grid are served
    a 404 here: this test only needs to prove discovery/enrollment
    (discovery_hits, tracked_urls, crawl_tasks) works from a DB-sourced
    page, not the full enroll -> confirm -> deal lane (already proven for
    tackle_warehouse in test_tick_enroll_confirm_e2e.py)."""

    def __init__(self, url_to_body: dict[str, bytes]):
        self._bodies = dict(url_to_body)
        self.now = datetime.now(timezone.utc)
        self.calls: list[str] = []

    def fetch(self, request, egress, user_agent, timeout_s):
        self.calls.append(request.url)
        body = self._bodies.get(request.url, b"")
        status = 200 if request.url in self._bodies else 404
        return FetchResponse(
            request=request, status=status, final_url=request.url, headers={}, body=body,
            elapsed_ms=1, fetched_at=self.now, egress_mode="DIRECT",
            snapshot_ref=f"manifest:{request.url}:{self.now.isoformat()}",
        )


def _retailer_id(cur, slug: str) -> int:
    cur.execute("SELECT id FROM retailers WHERE slug = %s", (slug,))
    row = cur.fetchone()
    assert row is not None, f"{slug} not seeded (expected from db/migrations seed files)"
    return row[0]


class _SharedConnectionCtx:
    """Stands in for `fpt.db.connect()`'s context manager but wraps the
    SAME connection the test's `db` fixture already holds, and neither
    commits nor closes it -- see the identical helper in
    test_tick_enroll_confirm_e2e.py for the full rationale."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture()
def academy_only(db, monkeypatch):
    """Restrict fpt.cli to ONLY academy -- keeps this test fast and
    focused without needing allowlist/robots fixtures for the other 19
    enabled retailers. Deliberately leaves `_discovery_pages_for`
    UNPATCHED so the real DB-backed lookup runs."""
    monkeypatch.setattr(cli_module, "_enabled_retailers", lambda: [_academy_retailer_cfg()])
    monkeypatch.setattr(cli_module, "connect", lambda: _SharedConnectionCtx(db))
    monkeypatch.setattr(cli_module, "table_exists", lambda conn, name: True)


def test_discovery_pages_for_reads_from_db_not_yaml(db, academy_only):
    """Direct proof of the fix: config/discovery_pages.yaml has NO academy
    entry (only tackle_warehouse) -- if `_discovery_pages_for` were still
    YAML-only, this would return []. With the fix it returns the one row
    db/migrations/012_seed_retailers.up.sql seeded for academy."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "academy")

    pages = cli_module._discovery_pages_for(
        "academy", conn=db, retailer_id=retailer_id, now=datetime.now(timezone.utc)
    )

    assert len(pages) == 1
    assert pages[0]["url"] == _ACADEMY_CLEARANCE_URL
    assert pages[0]["page_type"] == "CLEARANCE_LISTING"

    # Sanity check on the premise of the bug: the YAML fallback path (no
    # conn/retailer_id) must NOT see this page -- proving the DB path is
    # what actually supplies it now, not a YAML entry nobody noticed.
    yaml_only_pages = cli_module._discovery_pages_for("academy")
    assert yaml_only_pages == []


def test_real_tick_against_seeded_db_discovers_and_enrolls_academy(db, academy_only):
    """The acceptance criterion from the bug report: `python run.py tick`
    against a real Postgres with the seed-migration-populated
    discovery_pages writes discovery_hits > 0 and crawl_tasks > 0 on the
    first pass, for a retailer that has ZERO entries in
    config/discovery_pages.yaml."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "academy")

    clearance_body = (FIXTURES / "clearance_listing.html").read_bytes()
    fetcher = _ManifestFetcher(
        {
            "https://www.academy.com/robots.txt": _ROBOTS_ALLOW_ALL,
            _ACADEMY_CLEARANCE_URL: clearance_body,
        }
    )
    now = datetime.now(timezone.utc)
    fetcher.now = now

    summary = cli_module.run_tick(fetcher=fetcher, now=now, resolver=_fake_resolver, sleep_fn=lambda s: None)

    assert summary["db"].startswith("connected")
    academy_summary = next(r for r in summary["retailers"] if r["slug"] == "academy")
    assert "error" not in academy_summary
    assert len(academy_summary["pages"]) == 1
    assert academy_summary["pages"][0]["outcome"] == "OK"
    assert academy_summary["pages"][0]["discovered_count"] == 5

    cur.execute(
        "SELECT count(*) FROM discovery_hits dh JOIN discovery_pages dp ON dp.id = dh.discovery_page_id "
        "WHERE dp.retailer_id = %s",
        (retailer_id,),
    )
    discovery_hit_count = cur.fetchone()[0]
    assert discovery_hit_count == 5  # every grid row recorded, verbatim -- proves DB persistence, not just a dry-run print

    cur.execute("SELECT count(*) FROM tracked_urls WHERE retailer_id = %s", (retailer_id,))
    assert cur.fetchone()[0] == 5  # all 5 are "never seen before" -> enrolled

    cur.execute(
        "SELECT count(*) FROM crawl_tasks WHERE retailer_id = %s AND kind = 'ENROLL'", (retailer_id,)
    )
    enroll_task_count = cur.fetchone()[0]
    assert enroll_task_count == 5  # one ENROLL crawl_task queued per never-before-seen product URL

    cur.execute(
        "SELECT last_swept_at, last_item_count FROM discovery_pages WHERE retailer_id = %s", (retailer_id,)
    )
    last_swept_at, last_item_count = cur.fetchone()
    assert last_swept_at is not None
    assert last_item_count == 5

    # The ENROLL tasks queued above got drained in this SAME tick (the
    # product pages return 404 from this test's fetcher, so no listing
    # is ever parsed/persisted from them -- proving discovery+enrollment
    # works end to end without needing this test to also fake academy's
    # JSON-LD product-page shape).
    cur.execute(
        "SELECT status, outcome FROM crawl_tasks WHERE retailer_id = %s AND kind = 'ENROLL'", (retailer_id,)
    )
    enroll_rows = cur.fetchall()
    assert all(status == "DONE" and outcome == "NOT_FOUND" for status, outcome in enroll_rows)


def test_second_tick_before_interval_elapses_is_not_due(db, academy_only):
    """Scheduling respect (part of the fix's contract): once a page has
    been swept, it must not be re-swept before its own `interval_minutes`
    (360 for this academy row) has elapsed -- otherwise every retailer's
    discovery pages would be hit every single tick regardless of their
    configured cadence, defeating the whole point of `interval_minutes`
    and `discovery_pages.up.sql`'s own due-index."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "academy")

    clearance_body = (FIXTURES / "clearance_listing.html").read_bytes()
    fetcher = _ManifestFetcher(
        {
            "https://www.academy.com/robots.txt": _ROBOTS_ALLOW_ALL,
            _ACADEMY_CLEARANCE_URL: clearance_body,
        }
    )
    tick1_at = datetime.now(timezone.utc)
    fetcher.now = tick1_at
    cli_module.run_tick(fetcher=fetcher, now=tick1_at, resolver=_fake_resolver, sleep_fn=lambda s: None)

    cur.execute("SELECT count(*) FROM discovery_hits")
    assert cur.fetchone()[0] == 5

    # Tick 2, only 10 minutes later -- well inside the 360-minute interval.
    tick2_at = tick1_at + timedelta(minutes=10)
    fetcher.now = tick2_at
    fetcher.calls.clear()
    summary2 = cli_module.run_tick(fetcher=fetcher, now=tick2_at, resolver=_fake_resolver, sleep_fn=lambda s: None)

    academy_summary2 = next(r for r in summary2["retailers"] if r["slug"] == "academy")
    assert academy_summary2["pages"] == []  # not due yet -- no fetch attempted at all
    assert _ACADEMY_CLEARANCE_URL not in fetcher.calls

    cur.execute("SELECT count(*) FROM discovery_hits")
    assert cur.fetchone()[0] == 5  # unchanged -- tick 2 swept nothing
