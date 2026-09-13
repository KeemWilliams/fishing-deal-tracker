"""Integration tests for fpt.export against the shared Postgres used by
this directory's other DB tests (see tests/integration/conftest.py --
requires DATABASE_URL, collection-skips cleanly when it's unset).

Follows this directory's established convention rather than spinning up a
private Docker container: every test runs inside the `db` fixture's
transaction, which is rolled back at teardown, so nothing here ever
commits. Seeded rows use uuid-suffixed names/slugs so this file is safe to
run concurrently with other integration test files (or other agents'
sessions) against the same shared instance without colliding on unique
constraints.
"""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest

from fpt.export import queries
from fpt.export.runner import build_export_documents, run_export
from fpt.export.storage import LocalStorage
from fpt.export.validate import validate_deals_feed, validate_meta, validate_product

from tests.integration._harness import (
    get_retailer_id,
    insert_deal,
    insert_observation,
    seed_brand,
    seed_crawl_task,
    seed_listing,
    seed_offer,
    seed_product_variant,
    seed_seller,
)


def _uid() -> str:
    return uuid.uuid4().hex[:10]


def _seed_deal_scenario(db):
    """Seeds one product/variant on the tackle_warehouse retailer (already
    present from migration 012_seed_retailers) with three ACTIVE deals --
    one per lane -- plus one CANDIDATE deal that must never be exported.
    All names/slugs are uuid-suffixed so this is collision-safe against
    concurrently-committed data from other tests/agents on a shared
    instance. Returns the deal pub_ids and the product slug so assertions
    can find exactly these rows in query results that may also contain
    other, unrelated committed deals."""
    cur = db.cursor()
    uid = _uid()
    now = datetime.now(timezone.utc)

    retailer_id = get_retailer_id(cur, "tackle_warehouse")
    brand_id = seed_brand(cur, f"ExportTestBrand-{uid}")
    seller_id = seed_seller(
        cur, retailer_id=retailer_id, seller_key=f"export_seller_{uid}", seller_type="FIRST_PARTY",
        name="Tackle Warehouse",
    )
    product_slug = f"export-test-rod-{uid}"
    _, variant_id = seed_product_variant(
        cur,
        brand_id=brand_id,
        slug=product_slug,
        category="rod",
        model_key=f"model-{uid}",
        variant_key=f"vk-{uid}",
        label="7'6\" Medium, Fast",
    )
    crawl_task_id = seed_crawl_task(
        cur, retailer_id=retailer_id, kind="CONFIRM", page_type="PRODUCT",
        url=f"https://www.tacklewarehouse.com/{uid}", dedupe_key=f"dedupe-{uid}",
    )

    # No harness helper exists for fetch_log/tick_runs -- seeded directly so
    # tackle_warehouse reads as recently-fetched (not stale) for the
    # integrity-gate tests below. Without this, meta.build_retailer_meta
    # would see last_successful_fetch_at = NULL for every retailer in a
    # fresh database and the "all retailers stale" gate would always fire.
    cur.execute(
        "INSERT INTO tick_runs (started_at, finished_at) VALUES (%s, %s) RETURNING id",
        (now, now),
    )
    tick_run_id = cur.fetchone()[0]
    cur.execute(
        """INSERT INTO fetch_log
           (crawl_task_id, retailer_id, tick_run_id, attempt, http_status, outcome,
            adapter_version, egress_mode, fetched_at)
           VALUES (%s, %s, %s, 1, 200, 'OK', 'test-harness-1', 'DIRECT', %s)""",
        (crawl_task_id, retailer_id, tick_run_id, now),
    )

    def _seed_active_deal(lane, rule, condition, price_cents, reference_cents,
                           reference_kind, reference_detail, claimed_kind, claimed_cents,
                           claimed_inflated, discount_pct):
        listing_id = seed_listing(
            cur,
            retailer_id=retailer_id,
            retailer_sku=f"sku-{uuid.uuid4().hex[:8]}",
            url=f"https://www.tacklewarehouse.com/deal/{uid}",
            title_raw="Export Test Rod",
            variant_label_raw="7'6\" Medium",
            attributes_raw={},
            attributes_norm={},
            gtin14=[],
            unit_count=None,
            variant_id=variant_id,
            variant_key_observed=f"vk-{uid}",
            match_status="manual",
        )
        offer_id = seed_offer(
            cur, listing_id=listing_id, seller_id=seller_id,
            offer_key=f"offer-{uuid.uuid4().hex[:8]}", condition=condition,
        )
        obs_id = insert_observation(
            cur, offer_id=offer_id, crawl_task_id=crawl_task_id, task_kind="CONFIRM",
            observed_at=now, price_cents=price_cents, on_clearance=False,
            availability="IN_STOCK", quality="OK", reasons=[], stock_qty=2,
        )
        deal_id = insert_deal(
            cur, offer_id=offer_id, rule=rule, lane=lane, status="ACTIVE",
            detected_observation_id=obs_id, confirming_observation_id=obs_id,
            detected_via="PRODUCT_PAGE", price_cents=price_cents,
            reference_kind=reference_kind, reference_cents=reference_cents,
            reference_detail=reference_detail, discount_pct=discount_pct,
            claimed_reference_kind=claimed_kind, claimed_reference_cents=claimed_cents,
            claimed_inflated=claimed_inflated,
        )
        cur.execute("SELECT pub_id FROM deals WHERE id = %s", (deal_id,))
        return cur.fetchone()[0], offer_id

    verified_pub_id, _ = _seed_active_deal(
        "VERIFIED", "DEEP_DISCOUNT_NEW", "NEW", 6497, 12999,
        "OWN_HISTORY_MEDIAN_90D", {"days": 74}, None, None, None, 50.0,
    )
    claimed_pub_id, _ = _seed_active_deal(
        "CLAIMED", "DEEP_DISCOUNT_CLAIMED", "NEW", 3499, 6999,
        "RETAILER_CLAIMED", {}, "WAS", 6999, None, 50.0,
    )
    used_pub_id, used_offer_id = _seed_active_deal(
        "USED", "USED_VS_CURRENT_NEW", "USED_LIKE_NEW", 5499, 12999,
        "CURRENT_NEW", {}, None, None, None, 57.69,
    )

    # A CANDIDATE (never confirmed) deal on its own, separate offer -- must
    # never show up in the export. Separate offer avoids the
    # deals_one_open_per_offer_rule partial-unique index colliding with the
    # ACTIVE deals above.
    candidate_listing_id = seed_listing(
        cur, retailer_id=retailer_id, retailer_sku=f"sku-{uuid.uuid4().hex[:8]}",
        url=f"https://www.tacklewarehouse.com/candidate/{uid}", title_raw="Export Test Rod",
        variant_label_raw="7'6\" Medium", attributes_raw={}, attributes_norm={}, gtin14=[],
        unit_count=None, variant_id=variant_id, variant_key_observed=f"vk-{uid}", match_status="manual",
    )
    candidate_offer_id = seed_offer(
        cur, listing_id=candidate_listing_id, seller_id=seller_id,
        offer_key=f"offer-{uuid.uuid4().hex[:8]}", condition="NEW",
    )
    candidate_obs_id = insert_observation(
        cur, offer_id=candidate_offer_id, crawl_task_id=crawl_task_id, task_kind="DISCOVERY",
        observed_at=now, price_cents=1000, on_clearance=False, availability="IN_STOCK",
        quality="OK", reasons=[],
    )
    candidate_deal_id = insert_deal(
        cur, offer_id=candidate_offer_id, rule="DEEP_DISCOUNT_NEW", lane="VERIFIED", status="CANDIDATE",
        detected_observation_id=candidate_obs_id, detected_via="DISCOVERY_GRID",
        price_cents=1000, reference_kind="OWN_HISTORY_MEDIAN_90D", reference_cents=2000,
        discount_pct=50.0,
    )
    cur.execute("SELECT pub_id FROM deals WHERE id = %s", (candidate_deal_id,))
    candidate_pub_id = cur.fetchone()[0]

    # offer_daily_price is a materialized view (009_views.up.sql) that
    # product history is read from. Use a plain (non-CONCURRENT) refresh --
    # REFRESH ... CONCURRENTLY cannot run inside a transaction block, and
    # this whole test runs inside one (the `db` fixture's rollback-at-
    # teardown transaction). The plain form is fine here: it just re-runs
    # the view's defining query, which sees this transaction's own writes.
    cur.execute("REFRESH MATERIALIZED VIEW offer_daily_price;")

    return {
        "product_slug": product_slug,
        "verified_pub_id": verified_pub_id,
        "claimed_pub_id": claimed_pub_id,
        "used_pub_id": used_pub_id,
        "candidate_pub_id": candidate_pub_id,
        "used_offer_id": used_offer_id,
        "variant_id": variant_id,
    }


class TestQueriesAgainstRealSchema:
    def test_active_deal_rows_includes_seeded_active_deals_not_candidate(self, db):
        seeded = _seed_deal_scenario(db)
        rows = queries.fetch_active_deal_rows(db)
        pub_ids = {r["deal_id"] for r in rows}

        assert seeded["verified_pub_id"] in pub_ids
        assert seeded["claimed_pub_id"] in pub_ids
        assert seeded["used_pub_id"] in pub_ids
        # "only confirmed deals": a CANDIDATE deal must never appear.
        assert seeded["candidate_pub_id"] not in pub_ids

    def test_every_active_deal_row_has_a_condition(self, db):
        _seed_deal_scenario(db)
        rows = queries.fetch_active_deal_rows(db)
        assert rows, "expected at least the seeded deals"
        assert all(r["condition"] for r in rows)

    def test_products_with_variants_returns_seeded_variant(self, db):
        seeded = _seed_deal_scenario(db)
        rows = queries.fetch_products_with_variants(db, [seeded["product_slug"]])
        assert len(rows) == 1
        assert rows[0]["variant_id"] == seeded["variant_id"]


class TestBuildExportDocumentsAgainstRealSchema:
    def test_builds_and_validates_full_export(self, db):
        seeded = _seed_deal_scenario(db)
        generated_at = datetime.now(timezone.utc)
        meta, deals_feed, products_json = build_export_documents(db, generated_at=generated_at)

        # Structural validation against the site's contract must hold
        # regardless of how much OTHER committed data exists in the shared
        # instance.
        validate_meta(meta)
        validate_deals_feed(deals_feed)
        for doc in products_json.values():
            validate_product(doc)

        deal_ids = {d["deal_id"] for d in deals_feed["deals"]}
        assert seeded["verified_pub_id"] in deal_ids
        assert seeded["claimed_pub_id"] in deal_ids
        assert seeded["used_pub_id"] in deal_ids
        assert seeded["candidate_pub_id"] not in deal_ids

        assert seeded["product_slug"] in products_json
        variant = products_json[seeded["product_slug"]]["variants"][0]
        # 3 offers behind ACTIVE deals + 1 behind the CANDIDATE-only deal --
        # the product page legitimately shows every currently-active offer
        # for the variant, regardless of that offer's deal status.
        assert len(variant["offers"]) == 4
        assert len(variant["history"]) >= 1

        our_claimed = next(d for d in deals_feed["deals"] if d["deal_id"] == seeded["claimed_pub_id"])
        assert our_claimed["reference"] is None  # retailer-claimed price kept separate
        assert our_claimed["claimed_reference"]["cents"] == 6999

        our_used = next(d for d in deals_feed["deals"] if d["deal_id"] == seeded["used_pub_id"])
        assert our_used["condition"] == "USED_LIKE_NEW"
        assert our_used["reference"]["kind"] == "CROSS_RETAILER_NEW"  # CURRENT_NEW mapping


class TestRunExportEndToEnd:
    def test_run_export_promotes_to_local_storage(self, db, tmp_path):
        _seed_deal_scenario(db)
        storage = LocalStorage(tmp_path)

        outcome = run_export(db, storage)

        assert outcome.promoted is True
        assert outcome.gate_failures == []
        assert (tmp_path / "latest" / "meta.json").exists()
        assert (tmp_path / "latest" / "deals.json").exists()

        meta = json.loads((tmp_path / "latest" / "meta.json").read_text())
        assert meta["schema_version"] == 2

    def test_run_export_is_repeatable(self, db, tmp_path):
        _seed_deal_scenario(db)
        storage = LocalStorage(tmp_path)

        first = run_export(db, storage)
        second = run_export(db, storage)

        assert first.export_id != second.export_id
        assert second.promoted is True
        meta = json.loads((tmp_path / "latest" / "meta.json").read_text())
        assert meta["export_id"] == second.export_id

    def test_dry_run_writes_nothing(self, db, tmp_path):
        seeded = _seed_deal_scenario(db)
        storage = LocalStorage(tmp_path)

        outcome = run_export(db, storage, dry_run=True)

        assert outcome.promoted is False
        assert not (tmp_path / "exports").exists()
        assert not (tmp_path / "latest").exists()


class TestCliExportWiring:
    def test_cli_export_dry_run_wires_db_and_storage(self, db, monkeypatch, tmp_path, capsys):
        """Smoke test for the `export` subcommand's wiring in fpt/cli.py.
        Reuses this test's own already-seeded, already-open transaction
        (via a fake `connect()`) instead of opening a second real
        connection, so it stays inside the rollback-safe fixture instead
        of requiring a commit `fpt.db.connect()` could see."""
        seeded = _seed_deal_scenario(db)
        monkeypatch.setenv("EXPORT_STORAGE_BACKEND", "local")
        monkeypatch.setenv("EXPORT_LOCAL_DIR", str(tmp_path))

        import fpt.cli as cli_module

        @contextmanager
        def _fake_connect():
            yield db

        monkeypatch.setattr(cli_module, "connect", _fake_connect)

        exit_code = cli_module.main(["export", "--dry-run"])
        assert exit_code == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["promoted"] is False
        assert payload["counts"]["verified_deals"] >= 1
