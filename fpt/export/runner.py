"""Orchestrates one export run: query -> build -> validate -> write
versioned -> integrity gate -> promote. Wired to `fpt export` in
fpt/cli.py.

Scope note on the integrity gate: the architecture doc (3.4) describes five
gate conditions computed against the *previous* export (>30% product/offer
swing, all-retailers-stale, >3% suspect deal rate, >20% of deals moving
discount_pct by 10+ points, schema validation). This task implements the
two that are checkable from data already produced in this run without a
persisted export-history table (which is Database Engineer scope): schema
validation, and all-retailers-stale. The size-swing and discount-drift
gates need the previous export's counts/deals for comparison; `read_latest`
lets a future pass add them by reading back `latest/meta.json` and
`latest/deals.json` before promoting -- deliberately left as a follow-up,
flagged in HANDOFF, rather than half-implemented against data this task
doesn't have a reliable source for yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import psycopg

from fpt.export import build, queries
from fpt.export.storage import Storage
from fpt.export.validate import ExportValidationError, validate_deals_feed, validate_meta, validate_product


@dataclass
class ExportOutcome:
    export_id: str
    promoted: bool
    gate_failures: list[str] = field(default_factory=list)
    counts: dict | None = None
    files_written: list[str] = field(default_factory=list)


def _dumps(obj) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=False) + "\n").encode("utf-8")


def build_export_documents(conn: "psycopg.Connection", *, generated_at: datetime) -> tuple[dict, dict, dict[str, dict]]:
    """Runs every query and builds meta.json / deals.json /
    products/<slug>.json as in-memory dicts. Raises `ExportValidationError`
    if any document fails schema validation -- callers treat that as a
    hard stop, never a partial publish."""
    deal_rows = queries.fetch_active_deal_rows(conn)
    deals_feed = build.build_deals_feed(deal_rows, generated_at=generated_at)
    validate_deals_feed(deals_feed)

    product_slugs = build.product_slugs_from_deals(deal_rows)
    variant_rows = queries.fetch_products_with_variants(conn, product_slugs)
    products_by_slug = build.group_variant_rows_by_product(variant_rows)
    variant_ids = [v["variant_id"] for v in variant_rows]

    offer_rows = queries.fetch_variant_offers(conn, variant_ids, stale_hours=build.STALE_AFTER_HOURS)
    history_rows = queries.fetch_variant_history(conn, variant_ids)
    offers_by_variant = build.group_rows_by_variant(offer_rows)
    history_by_variant = build.group_rows_by_variant(history_rows)

    products_json: dict[str, dict] = {}
    for slug, inputs in products_by_slug.items():
        inputs.offers_by_variant = offers_by_variant
        inputs.history_by_variant = history_by_variant
        product_doc = build.build_product(inputs)
        validate_product(product_doc)
        products_json[slug] = product_doc

    retailer_rows = queries.fetch_retailer_rows(conn)
    offers_tracked = queries.fetch_active_offer_count(conn)
    counts = build.counts_from_deals(deal_rows, product_count=len(products_json), offers_tracked=offers_tracked)
    export_id = build.build_export_id(generated_at=generated_at)
    meta = build.build_meta(retailer_rows=retailer_rows, counts=counts, generated_at=generated_at, export_id=export_id)
    validate_meta(meta)

    return meta, deals_feed, products_json


def _integrity_gate_failures(meta: dict, deals_feed: dict) -> list[str]:
    failures = []
    if meta["retailers"] and all(r["stale"] for r in meta["retailers"]):
        failures.append("all retailers stale (no successful fetch in 12h)")
    # L4: an empty feed is far more likely to mean a broken query, a
    # cascading rejection wave, or a bad deploy than "genuinely zero deals
    # right now" -- refuse to promote it over a previously-good `latest`
    # without an explicit --force. Schema validation (build_export_
    # documents, called unconditionally before this function ever runs)
    # still always executes even when --force is passed, since --force
    # only ever bypasses THIS gate, never the schema checks.
    if not deals_feed["deals"]:
        failures.append("empty deals feed (0 ACTIVE deals) -- pass --force to publish anyway")
    return failures


def run_export(
    conn: "psycopg.Connection",
    storage: Storage,
    *,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> ExportOutcome:
    generated_at = now or datetime.now(timezone.utc)

    try:
        meta, deals_feed, products_json = build_export_documents(conn, generated_at=generated_at)
    except ExportValidationError as exc:
        return ExportOutcome(export_id="", promoted=False, gate_failures=[str(exc)])

    files: dict[str, bytes] = {
        "meta.json": _dumps(meta),
        "deals.json": _dumps(deals_feed),
    }
    for slug, doc in products_json.items():
        files[f"products/{slug}.json"] = _dumps(doc)

    export_id = meta["export_id"]

    if dry_run:
        return ExportOutcome(export_id=export_id, promoted=False, counts=meta["counts"], files_written=[])

    storage.write_versioned(export_id, files)

    gate_failures = [] if force else _integrity_gate_failures(meta, deals_feed)
    if gate_failures:
        return ExportOutcome(
            export_id=export_id,
            promoted=False,
            gate_failures=gate_failures,
            counts=meta["counts"],
            files_written=list(files.keys()),
        )

    storage.promote(export_id, files)
    return ExportOutcome(
        export_id=export_id,
        promoted=True,
        counts=meta["counts"],
        files_written=list(files.keys()),
    )
