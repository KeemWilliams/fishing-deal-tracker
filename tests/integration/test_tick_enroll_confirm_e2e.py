"""End-to-end offline integration test for the enroller/queue wiring
(discovery -> discovery_hits/tracked_urls -> ENROLL crawl_task -> queue
drain -> ingest -> CANDIDATE -> CONFIRM crawl_task -> queue drain on a
LATER tick -> ACTIVE), plus the security/robustness fixes layered on top:
robots.txt admission, the SSRF allowlist, oversize-body/recursion-bomb
guards, and `products.slug` collision handling.

Runs `fpt.cli.run_tick` TWICE against a throwaway Postgres with a
`FixtureFetcher`-equivalent stub (no network to any retailer, ever). The
clock between the two ticks is advanced past the confirmation gap (C1,
architecture doc 6.4) WITHOUT sleeping -- `run_tick(now=...)` accepts an
explicit timestamp for exactly this reason.

Requires DATABASE_URL pointing at a throwaway Postgres with all
db/migrations/*.up.sql (001-014) applied. See tests/integration/conftest.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fpt.cli as cli_module
from fpt.core.models import FetchResponse

FIXTURES = Path(__file__).parent.parent / "fixtures" / "tackle_warehouse"

_PRODUCT_URL = "https://www.tacklewarehouse.com/St_Croix_Triumph_Spinning_Rods/descpage-MBTSR.html"
_CLEARANCE_URL = "https://www.tacklewarehouse.com/catpage-CLEARRODSPP.html"
_LOW_DISCOUNT_PRODUCT_URL = "https://www.tacklewarehouse.com/Some_Other_Rod/descpage-OTHERROD.html"

_ROBOTS_ALLOW_ALL = b"User-agent: *\nDisallow:\n"


def _fake_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


def _tw_retailer_cfg() -> dict:
    return {
        "slug": "tackle_warehouse",
        "name": "Tackle Warehouse",
        "base_url": "https://www.tacklewarehouse.com",
        "adapter_slug": "tackle_warehouse",
        "enabled": True,
        "allowed_hosts": ["www.tacklewarehouse.com"],
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


def _make_discounted_product_html() -> bytes:
    """Same fixture as product_rod_single_variant.html, with its price cut
    to $60 -- a genuine 53.8% discount off the $130 OWN_HISTORY_MEDIAN_90D
    this test pre-seeds for the same offer (see `_seed_own_history` below).

    NOTE: deliberately NOT using the `data-closeout-item`/`data-list-price`
    (DEEP_DISCOUNT_CLAIMED) path here. While building this test, that path
    exposed a real bug in fpt/store/deals.py's `build_confirm_context`
    (owned by a different coder as of this task): for any non-USED lane it
    unconditionally calls `resolve_verified_reference_for_offer` and, when
    no VERIFIED reference exists (which is exactly the precondition for a
    CLAIMED-lane deal to have been created in the first place), treats the
    missing reference as `reference_cents = None` -> `recomputed_discount_
    pct = 0.0` -> REJECTED `below_threshold_on_confirm`. This means NO
    DEEP_DISCOUNT_CLAIMED deal can ever reach ACTIVE under the current
    code. Flagged prominently in the HANDOFF; not fixed here (out of this
    task's file-ownership scope) -- this test uses the OWN_HISTORY
    (VERIFIED) path instead, which is unaffected."""
    body = (FIXTURES / "product_rod_single_variant.html").read_text(encoding="utf-8")
    body = body.replace(
        'class="js-ordering-price price">130.00<', 'class="js-ordering-price price">60.00<'
    )
    assert "60.00" in body
    return body.encode("utf-8")


def _make_low_discount_product_html() -> bytes:
    """A second, DIFFERENT SKU discounted only 20%, with NO history and no
    claimed reference -- must never produce a deal (no reference at all
    to measure a discount against, let alone a >=50% one)."""
    body = (FIXTURES / "product_rod_single_variant.html").read_text(encoding="utf-8")
    body = body.replace('data-code="MBTSR706M"', 'data-code="OTHERROD1"')
    body = body.replace(
        'class="js-ordering-price price">130.00<', 'class="js-ordering-price price">104.00<'
    )
    assert "104.00" in body
    return body.encode("utf-8")


def _seed_own_history(cur, *, retailer_id: int, retailer_sku: str, url: str, regular_price_cents: int) -> None:
    """Pre-seeds 21 non-clearance daily observations spanning 40 days
    (architecture 6.1's own_history_min_days=21 / min_span_days=30,
    config/deal_rules.yaml), anchored to real "now" so the reference
    resolver's 90-day window (which filters on the real CURRENT_DATE)
    finds them regardless of when this test runs -- a fixed historical
    base_day would silently fall outside the window over time (a bug this
    task's own test-writing already reproduced once, see
    tests/integration/test_store_pipeline.py's matching comment).

    Seeds through the SAME retailer_sku/url/offer_key the real tick will
    later upsert against (retailer_id, retailer_sku) and (listing_id,
    offer_key) -- so tick 1's ingestion updates this exact listing/offer
    row rather than creating a sibling."""
    from tests.integration._harness import (
        insert_observation,
        seed_brand,
        seed_crawl_task,
        seed_listing,
        seed_offer,
        seed_product_variant,
        seed_seller,
    )

    brand_id = seed_brand(cur, "St. Croix")
    _, variant_id = seed_product_variant(
        cur, brand_id=brand_id, slug=f"seed-{retailer_sku.lower()}", category="rod",
        model_key=f"seed-{retailer_sku.lower()}", variant_key="length_in=90|power=M", label="7'6\" M",
    )
    listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku=retailer_sku, url=url, title_raw="Seed listing",
        variant_label_raw="seed", attributes_raw={}, attributes_norm={"length_in": 90, "power": "M"},
        gtin14=[], unit_count=None, variant_id=variant_id, variant_key_observed="length_in=90|power=M",
    )
    seller_id = seed_seller(cur, retailer_id=retailer_id, seller_key="tackle_warehouse", seller_type="FIRST_PARTY")
    offer_id = seed_offer(cur, listing_id=listing_id, seller_id=seller_id, offer_key=retailer_sku, condition="NEW")

    base_day = datetime.now(timezone.utc) - timedelta(days=50)
    for d in range(0, 42, 2):  # 21 observations spanning 40 days
        observed_at = base_day + timedelta(days=d)
        task_id = seed_crawl_task(
            cur, retailer_id=retailer_id, kind="BASELINE", page_type="PRODUCT", url=url,
            dedupe_key=f"BASELINE:seed:{retailer_sku}:{d}",
        )
        insert_observation(
            cur, offer_id=offer_id, crawl_task_id=task_id, task_kind="BASELINE", observed_at=observed_at,
            price_cents=regular_price_cents, on_clearance=False, availability="IN_STOCK", quality="OK", reasons=[],
        )


def _make_clearance_html(*rows: tuple[str, str]) -> bytes:
    """Minimal TW clearance-grid markup: enough to satisfy
    TackleWarehouseAdapter's sentinels and CSS selectors. `rows` is
    (product_url, title) pairs."""
    cells = "\n".join(
        f"""
        <div class="cattable-wrap-cell" data-code="CODE-{i}">
          <a class="cattable-wrap-cell-info" href="{url}">
            <h3 class="cattable-wrap-cell-info-name">{title}</h3>
          </a>
          <div class="cattable-wrap-cell-info-price">$<span>1.00</span></div>
        </div>
        """
        for i, (url, title) in enumerate(rows)
    )
    return f"<html><body>{cells}<!-- gtm_impression --></body></html>".encode("utf-8")


class _ManifestFetcher:
    """Fetcher stand-in serving fixed bytes per exact URL, with an
    injectable clock -- no network, ever."""

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
    assert row is not None
    return row[0]


class _SharedConnectionCtx:
    """Stands in for `fpt.db.connect()`'s context manager but wraps the
    SAME connection the test's `db` fixture already holds, and neither
    commits nor closes it -- `run_tick`'s writes then land in the exact
    transaction the `db` fixture rolls back at teardown, instead of a
    separate connection that would really commit to the throwaway
    Postgres and leak state into the next test."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        return False  # never commit/close/rollback here -- the `db` fixture owns that


@pytest.fixture()
def tw_only(db, monkeypatch):
    """Restrict fpt.cli to ONLY tackle_warehouse, with a single clearance
    discovery page pointing at our crafted fixtures -- keeps this test
    fast and focused without needing allowlist/robots fixtures for the
    other five configured retailers. Also redirects `fpt.cli.connect()`
    (and therefore every DB write `run_tick` makes) onto the test's own
    rolled-back `db` connection -- see `_SharedConnectionCtx`."""
    monkeypatch.setattr(cli_module, "_enabled_retailers", lambda: [_tw_retailer_cfg()])
    monkeypatch.setattr(
        cli_module,
        "_discovery_pages_for",
        lambda slug, **_kwargs: [
            {
                "retailer": "tackle_warehouse",
                "page_type": "CLEARANCE_LISTING",
                "url": _CLEARANCE_URL,
                "category_hint": "rod",
                "interval_minutes": 240,
                "max_pages": 1,
            }
        ]
        if slug == "tackle_warehouse"
        else [],
    )
    monkeypatch.setattr(cli_module, "connect", lambda: _SharedConnectionCtx(db))
    monkeypatch.setattr(cli_module, "table_exists", lambda conn, name: True)


def test_two_tick_offline_enroll_to_active_deal(db, tw_only):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    _seed_own_history(cur, retailer_id=retailer_id, retailer_sku="MBTSR706M", url=_PRODUCT_URL, regular_price_cents=13000)

    clearance_body = _make_clearance_html(
        (_PRODUCT_URL, "St. Croix Triumph Spinning Rod (deep discount)"),
        (_LOW_DISCOUNT_PRODUCT_URL, "Some Other Rod (shallow discount)"),
    )
    fetcher = _ManifestFetcher(
        {
            "https://www.tacklewarehouse.com/robots.txt": _ROBOTS_ALLOW_ALL,
            _CLEARANCE_URL: clearance_body,
            _PRODUCT_URL: _make_discounted_product_html(),
            _LOW_DISCOUNT_PRODUCT_URL: _make_low_discount_product_html(),
        }
    )

    tick1_at = datetime.now(timezone.utc)
    fetcher.now = tick1_at

    # --- Tick 1: discover both rods, enroll both, drain the ENROLL queue
    # (which fetches the product pages and detects a CANDIDATE for the
    # deep-discount one only).
    summary1 = cli_module.run_tick(
        fetcher=fetcher, now=tick1_at, resolver=_fake_resolver, sleep_fn=lambda s: None,
    )
    assert summary1["db"].startswith("connected")

    cur.execute(
        "SELECT count(*) FROM discovery_hits dh JOIN discovery_pages dp ON dp.id = dh.discovery_page_id "
        "WHERE dp.retailer_id = %s", (retailer_id,),
    )
    assert cur.fetchone()[0] == 2  # both grid rows recorded, verbatim

    cur.execute("SELECT count(*) FROM tracked_urls WHERE retailer_id = %s", (retailer_id,))
    assert cur.fetchone()[0] == 2  # both enrolled (both are "never seen before")

    cur.execute(
        "SELECT id, kind, status, outcome, url FROM crawl_tasks WHERE retailer_id = %s ORDER BY id", (retailer_id,),
    )
    tasks_after_tick1 = cur.fetchall()
    assert any(k == "DISCOVERY" and s == "DONE" for _, k, s, _, _ in tasks_after_tick1)
    enroll_tasks = [t for t in tasks_after_tick1 if t[1] == "ENROLL"]
    # Follow-up fix (post-2570448): fpt/scheduler/queue.py now passes the
    # already-leased crawl_task_id into ingest_parsed_listing, so
    # fpt/store/pipeline.py attributes observations to the SAME row this
    # queue leased instead of synthesizing a second crawl_tasks row via
    # its own dedupe_key. One real ENROLL fetch now produces exactly ONE
    # crawl_tasks row per (kind, url) -- verified by both the count and
    # the per-URL grouping below.
    assert len(enroll_tasks) == 2
    assert all(t[2] == "DONE" and t[3] == "OK" for t in enroll_tasks)  # every product-page fetch ran and succeeded
    enrolled_urls = [t[4] for t in enroll_tasks]
    assert sorted(enrolled_urls) == sorted([_PRODUCT_URL, _LOW_DISCOUNT_PRODUCT_URL])
    assert len(enrolled_urls) == len(set(enrolled_urls))  # exactly one ENROLL row per URL, no duplicates

    # The observation itself must be attributed to the SAME crawl_task the
    # queue leased (not a second, orphaned one) -- the concrete assertion
    # behind "exactly one crawl_tasks row per (kind, url)".
    deep_discount_enroll_task_id = next(t[0] for t in enroll_tasks if t[4] == _PRODUCT_URL)
    cur.execute(
        "SELECT crawl_task_id FROM price_observations po JOIN offers o ON o.id = po.offer_id "
        "JOIN listings l ON l.id = o.listing_id WHERE l.retailer_id = %s AND l.retailer_sku = 'MBTSR706M' "
        "AND po.task_kind = 'ENROLL'",
        (retailer_id,),
    )
    assert cur.fetchone()[0] == deep_discount_enroll_task_id

    # The deep-discount offer got an observation and a CANDIDATE deal
    # (DEEP_DISCOUNT_NEW / VERIFIED -- $130 90-day own-history median vs
    # $60 confirming price = 53.8%).
    cur.execute(
        "SELECT o.id, o.condition FROM offers o JOIN listings l ON l.id = o.listing_id "
        "WHERE l.retailer_id = %s AND l.retailer_sku = 'MBTSR706M'", (retailer_id,),
    )
    offer_row = cur.fetchone()
    assert offer_row is not None
    offer_id = offer_row[0]

    cur.execute("SELECT count(*) FROM price_observations WHERE offer_id = %s", (offer_id,))
    assert cur.fetchone()[0] == 22  # 21 pre-seeded baseline days + tick 1's detecting fetch

    cur.execute(
        "SELECT status, rule, lane, discount_pct, reference_cents, confirming_observation_id "
        "FROM deals WHERE offer_id = %s", (offer_id,),
    )
    deal_row = cur.fetchone()
    assert deal_row is not None
    status, rule, lane, discount_pct, reference_cents, confirming_obs_id = deal_row
    assert status == "CONFIRMING"
    assert rule == "DEEP_DISCOUNT_NEW"
    assert lane == "VERIFIED"
    assert reference_cents == 13000
    assert float(discount_pct) >= 50.0
    assert confirming_obs_id is None  # never ACTIVE on first detection

    # The shallow-discount (20%) offer must never produce a deal at all.
    cur.execute(
        "SELECT o.id FROM offers o JOIN listings l ON l.id = o.listing_id "
        "WHERE l.retailer_id = %s AND l.retailer_sku = 'OTHERROD1'", (retailer_id,),
    )
    low_offer_id = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM deals WHERE offer_id = %s", (low_offer_id,))
    assert cur.fetchone()[0] == 0

    # A CONFIRM task should be queued, not yet due -- exactly one row.
    cur.execute(
        "SELECT id, status, not_before FROM crawl_tasks WHERE retailer_id = %s AND kind = 'CONFIRM'", (retailer_id,),
    )
    confirm_rows = cur.fetchall()
    assert len(confirm_rows) == 1
    confirm_task_id, confirm_status, confirm_not_before = confirm_rows[0]
    assert confirm_status == "QUEUED"
    assert confirm_not_before > tick1_at

    # --- Tick 2: clock advanced past the 10-minute confirmation gap
    # (architecture 6.4 C1) -- NEVER faked by shortening the gap itself,
    # only by moving `now` forward, exactly as a real 15-minute tick
    # cadence would.
    tick2_at = tick1_at + timedelta(minutes=15)
    fetcher.now = tick2_at
    summary2 = cli_module.run_tick(
        fetcher=fetcher, now=tick2_at, resolver=_fake_resolver, sleep_fn=lambda s: None,
    )
    assert summary2["db"].startswith("connected")

    cur.execute(
        "SELECT status, confirming_observation_id, confirmed_at FROM deals WHERE offer_id = %s", (offer_id,),
    )
    status, confirming_obs_id, confirmed_at = cur.fetchone()
    assert status == "ACTIVE"
    assert confirming_obs_id is not None
    assert confirmed_at is not None

    cur.execute("SELECT count(*) FROM price_observations WHERE offer_id = %s", (offer_id,))
    assert cur.fetchone()[0] == 23  # 21 seeded baseline + detecting + confirming, no more

    cur.execute(
        "SELECT status FROM crawl_tasks WHERE retailer_id = %s AND kind = 'CONFIRM'", (retailer_id,),
    )
    confirm_status_rows = cur.fetchall()
    assert len(confirm_status_rows) == 1  # still exactly one CONFIRM row -- tick 2 didn't create a second
    assert confirm_status_rows[0][0] == "DONE"

    # The confirming observation is attributed to the SAME crawl_task the
    # queue leased for this CONFIRM fetch -- the concrete proof that
    # ingest_parsed_listing used the passed-in crawl_task_id rather than
    # synthesizing a second row.
    cur.execute("SELECT crawl_task_id FROM price_observations WHERE id = %s", (confirming_obs_id,))
    assert cur.fetchone()[0] == confirm_task_id

    # --- fpt export against this same DB: the ACTIVE deal must appear.
    from fpt.export.runner import run_export
    from fpt.export.storage import LocalStorage

    export_dir = Path(__file__).parent.parent.parent / ".pytest_export_scratch"
    storage = LocalStorage(export_dir)
    try:
        outcome = run_export(db, storage, force=True, dry_run=False)
        assert outcome.promoted is True
        deals_bytes = storage.read_latest("deals.json")
        assert deals_bytes is not None
        import json as _json

        deals_doc = _json.loads(deals_bytes)
        skus_in_export = [d["retailer_url"] for d in deals_doc["deals"]]
        assert _PRODUCT_URL in skus_in_export
    finally:
        import shutil

        shutil.rmtree(export_dir, ignore_errors=True)


def test_robots_disallowed_clearance_page_enrolls_nothing(db, tw_only, monkeypatch):
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    fetcher = _ManifestFetcher(
        {
            "https://www.tacklewarehouse.com/robots.txt": b"User-agent: *\nDisallow: /\n",
            _CLEARANCE_URL: _make_clearance_html((_PRODUCT_URL, "Should never be fetched")),
        }
    )
    now = datetime.now(timezone.utc)
    fetcher.now = now

    cli_module.run_tick(fetcher=fetcher, now=now, resolver=_fake_resolver, sleep_fn=lambda s: None)

    assert _PRODUCT_URL not in fetcher.calls  # robots denial stopped it before any product fetch
    cur.execute("SELECT count(*) FROM discovery_hits")
    assert cur.fetchone()[0] == 0
    cur.execute("SELECT count(*) FROM tracked_urls WHERE retailer_id = %s", (retailer_id,))
    assert cur.fetchone()[0] == 0


def test_off_allowlist_host_never_enrolled(db, tw_only, monkeypatch):
    """A clearance grid whose row points OFF the retailer's own host (a
    compromised/misbehaving page, or an attacker-controlled injection)
    must never be enrolled or fetched (security review M3)."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    evil_url = "https://evil.example.com/steal-a-fetch"
    fetcher = _ManifestFetcher(
        {
            "https://www.tacklewarehouse.com/robots.txt": _ROBOTS_ALLOW_ALL,
            _CLEARANCE_URL: _make_clearance_html((evil_url, "Off-host row")),
        }
    )
    now = datetime.now(timezone.utc)
    fetcher.now = now

    cli_module.run_tick(fetcher=fetcher, now=now, resolver=_fake_resolver, sleep_fn=lambda s: None)

    assert evil_url not in fetcher.calls
    cur.execute("SELECT count(*) FROM discovery_hits dh JOIN discovery_pages dp ON dp.id = dh.discovery_page_id WHERE dp.retailer_id = %s", (retailer_id,))
    assert cur.fetchone()[0] == 1  # the sighting itself is still recorded
    cur.execute("SELECT count(*) FROM tracked_urls WHERE retailer_id = %s", (retailer_id,))
    assert cur.fetchone()[0] == 0  # but never enrolled/tracked
    cur.execute("SELECT count(*) FROM crawl_tasks WHERE retailer_id = %s AND kind = 'ENROLL'", (retailer_id,))
    assert cur.fetchone()[0] == 0  # and never queued


def test_private_ip_resolution_blocks_the_fetch(db, tw_only, monkeypatch):
    cur = db.cursor()
    fetcher = _ManifestFetcher(
        {
            "https://www.tacklewarehouse.com/robots.txt": _ROBOTS_ALLOW_ALL,
            _CLEARANCE_URL: _make_clearance_html((_PRODUCT_URL, "Private-IP host")),
        }
    )
    now = datetime.now(timezone.utc)
    fetcher.now = now

    cli_module.run_tick(
        fetcher=fetcher, now=now, resolver=lambda host: ["10.0.0.5"], sleep_fn=lambda s: None,
    )

    # Robots.txt itself resolves to the blocked IP too, so nothing is ever
    # admitted for this retailer at all.
    assert fetcher.calls == []
    cur.execute("SELECT count(*) FROM price_observations")
    assert cur.fetchone()[0] == 0


def test_recursion_bomb_adapter_parse_fails_the_task_not_the_tick(db, tw_only, monkeypatch):
    """Robustness review M4: a hostile/pathological page must never crash
    the whole tick. Monkeypatches the REAL adapter's `parse` to simulate a
    pathological input triggering RecursionError, and proves the task is
    marked FAILED while the tick itself completes normally."""
    cur = db.cursor()
    retailer_id = _retailer_id(cur, "tackle_warehouse")

    fetcher = _ManifestFetcher(
        {
            "https://www.tacklewarehouse.com/robots.txt": _ROBOTS_ALLOW_ALL,
            _CLEARANCE_URL: _make_clearance_html((_PRODUCT_URL, "Recursion bomb")),
            _PRODUCT_URL: _make_discounted_product_html(),
        }
    )
    now = datetime.now(timezone.utc)
    fetcher.now = now

    from fpt.adapters.tackle_warehouse import TackleWarehouseAdapter

    original_parse = TackleWarehouseAdapter.parse
    call_count = {"n": 0}

    def _bomb_or_real(self, response):
        call_count["n"] += 1
        if call_count["n"] == 2:  # let the CLEARANCE_LISTING parse succeed; bomb the PRODUCT parse
            raise RecursionError("simulated pathological nesting")
        return original_parse(self, response)

    monkeypatch.setattr(TackleWarehouseAdapter, "parse", _bomb_or_real)

    summary = cli_module.run_tick(fetcher=fetcher, now=now, resolver=_fake_resolver, sleep_fn=lambda s: None)
    assert "tackle_warehouse" in [r["slug"] for r in summary["retailers"]]  # tick completed, no crash

    cur.execute(
        "SELECT status, outcome FROM crawl_tasks WHERE retailer_id = %s AND kind = 'ENROLL'", (retailer_id,),
    )
    row = cur.fetchone()
    assert row == ("FAILED", "STRUCTURE_CHANGED")
    cur.execute("SELECT count(*) FROM price_observations")
    assert cur.fetchone()[0] == 0


def test_products_slug_collision_is_handled(db):
    """Robustness review M4: two DIFFERENT products (different brand)
    that slugify to the identical string must both get created, not
    crash on products.slug's global UNIQUE constraint."""
    from fpt.store.catalog import get_or_create_product

    cur = db.cursor()
    cur.execute("INSERT INTO brands (name, name_key) VALUES ('Brand Alpha', 'brand_alpha') RETURNING id")
    brand_a_id = cur.fetchone()[0]
    cur.execute("INSERT INTO brands (name, name_key) VALUES ('Brand Beta', 'brand_beta') RETURNING id")
    brand_b_id = cur.fetchone()[0]

    product_a_id = get_or_create_product(cur, brand_id=brand_a_id, category="rod", model_key="pro-series", name="Pro Series")
    product_b_id = get_or_create_product(cur, brand_id=brand_b_id, category="reel", model_key="pro-series", name="Pro Series")

    assert product_a_id != product_b_id
    cur.execute("SELECT slug FROM products WHERE id IN (%s, %s)", (product_a_id, product_b_id))
    slugs = {row[0] for row in cur.fetchall()}
    assert len(slugs) == 2  # both got distinct slugs despite identical name+model_key
