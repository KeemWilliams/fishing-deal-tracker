"""Minimal hand-rolled persistence helpers used ONLY by the integration
tests in this directory.

IMPORTANT: this is test scaffolding, not application code. No `store.py`
or equivalent persistence layer exists anywhere under fpt/ today (see the
test-engineer HANDOFF, defect CRITICAL-1) -- fpt/cli.py's `_try_db_persist`
only checks connectivity and table existence, it never writes a row. These
helpers exist so the test suite can exercise the real schema end-to-end and
prove or disprove pipeline/DB compatibility; they intentionally duplicate
the *minimum* insert logic a real store.py would need, using the same
domain objects (ParsedListing/ParsedOffer/ValidationResult/DealCandidate/
ConfirmResult) the fpt/ package already produces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from fpt.core.models import CONDITION_GROUP


def get_retailer_id(cur, slug: str) -> int:
    cur.execute("SELECT id FROM retailers WHERE slug = %s", (slug,))
    row = cur.fetchone()
    if row is None:
        raise AssertionError(f"retailer {slug!r} not seeded (expected from 012_seed_retailers)")
    return row[0]


def seed_brand(cur, name: str) -> int:
    key = name.lower().replace(" ", "_")
    cur.execute(
        "INSERT INTO brands (name, name_key) VALUES (%s, %s) "
        "ON CONFLICT (name_key) DO UPDATE SET name = EXCLUDED.name RETURNING id",
        (name, key),
    )
    return cur.fetchone()[0]


def seed_product_variant(
    cur,
    *,
    brand_id: int,
    slug: str,
    category: str,
    model_key: str,
    variant_key: str,
    label: str,
    unit_count: int | None = None,
    gtin14: list[str] | None = None,
) -> tuple[int, int]:
    cur.execute(
        """
        INSERT INTO products (slug, brand_id, name, model_key, category, origin)
        VALUES (%s, %s, %s, %s, %s, 'auto_created')
        ON CONFLICT (brand_id, category, model_key) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (slug, brand_id, label, model_key, category),
    )
    product_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO product_variants (product_id, gtin14, attributes, variant_key, label, unit_count)
        VALUES (%s, %s, '{}'::jsonb, %s, %s, %s)
        ON CONFLICT (product_id, variant_key) DO UPDATE SET label = EXCLUDED.label
        RETURNING id
        """,
        (product_id, gtin14 or [], variant_key, label, unit_count),
    )
    variant_id = cur.fetchone()[0]
    return product_id, variant_id


def seed_listing(
    cur,
    *,
    retailer_id: int,
    retailer_sku: str,
    url: str,
    title_raw: str,
    variant_label_raw: str,
    attributes_raw: dict,
    attributes_norm: dict,
    gtin14: list[str],
    unit_count: int | None,
    variant_id: int | None,
    variant_key_observed: str | None,
    match_status: str = "auto_created",
) -> int:
    import json

    cur.execute(
        """
        INSERT INTO listings (
            retailer_id, retailer_sku, url, title_raw, variant_label_raw,
            attributes_raw, attributes_norm, gtin14, unit_count,
            variant_id, variant_key_observed, match_status
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (retailer_id, retailer_sku) DO UPDATE SET title_raw = EXCLUDED.title_raw
        RETURNING id
        """,
        (
            retailer_id,
            retailer_sku,
            url,
            title_raw,
            variant_label_raw,
            json.dumps(attributes_raw),
            json.dumps(attributes_norm),
            gtin14,
            unit_count,
            variant_id,
            variant_key_observed,
            match_status,
        ),
    )
    return cur.fetchone()[0]


def seed_seller(cur, *, retailer_id: int, seller_key: str, seller_type: str, name: str | None = None) -> int:
    cur.execute(
        """
        INSERT INTO sellers (retailer_id, seller_key, name, seller_type)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (retailer_id, seller_key) DO UPDATE SET seller_type = EXCLUDED.seller_type
        RETURNING id
        """,
        (retailer_id, seller_key, name, seller_type),
    )
    return cur.fetchone()[0]


def seed_offer(
    cur,
    *,
    listing_id: int,
    seller_id: int,
    offer_key: str,
    condition: str,
    condition_raw: str | None = None,
) -> int:
    condition_group = CONDITION_GROUP[condition]
    cur.execute(
        """
        INSERT INTO offers (listing_id, seller_id, offer_key, condition, condition_group, condition_raw)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (listing_id, offer_key) DO UPDATE SET condition = EXCLUDED.condition
        RETURNING id
        """,
        (listing_id, seller_id, offer_key, condition, condition_group, condition_raw),
    )
    return cur.fetchone()[0]


def seed_crawl_task(
    cur,
    *,
    retailer_id: int,
    kind: str,
    page_type: str,
    url: str,
    dedupe_key: str,
    status: str = "DONE",
) -> int:
    cur.execute(
        """
        INSERT INTO crawl_tasks (retailer_id, kind, priority, page_type, url, dedupe_key, status)
        VALUES (%s, %s, 30, %s, %s, %s, %s)
        RETURNING id
        """,
        (retailer_id, kind, page_type, url, dedupe_key, status),
    )
    return cur.fetchone()[0]


def insert_observation(
    cur,
    *,
    offer_id: int,
    crawl_task_id: int,
    task_kind: str,
    observed_at: datetime,
    price_cents: int | None,
    on_clearance: bool,
    availability: str,
    quality: str,
    reasons: list[str],
    claimed_reference_cents: int | None = None,
    claimed_reference_kind: str | None = None,
    unit_count: int | None = None,
    variant_key_observed: str | None = None,
    stock_qty: int | None = None,
    stock_qty_is_floor: bool = False,
    adapter_version: str = "test-harness-1",
    snapshot_ref: str = "test-harness",
) -> int:
    cur.execute(
        """
        INSERT INTO price_observations (
            offer_id, crawl_task_id, task_kind, observed_at, observed_date_et,
            price_cents, claimed_reference_cents, claimed_reference_kind,
            on_clearance, availability, stock_qty, stock_qty_is_floor,
            unit_count, variant_key_observed, quality, quality_reasons,
            adapter_version, snapshot_ref
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            offer_id,
            crawl_task_id,
            task_kind,
            observed_at,
            observed_at.date(),
            price_cents,
            claimed_reference_cents,
            claimed_reference_kind,
            on_clearance,
            availability,
            stock_qty,
            stock_qty_is_floor,
            unit_count,
            variant_key_observed,
            quality,
            reasons,
            adapter_version,
            snapshot_ref,
        ),
    )
    return cur.fetchone()[0]


def insert_deal(
    cur,
    *,
    offer_id: int,
    rule: str,
    lane: str,
    status: str,
    detected_observation_id: int,
    detected_via: str,
    price_cents: int,
    reference_kind: str,
    reference_cents: int,
    discount_pct: float,
    reference_detail: dict | None = None,
    confirming_observation_id: int | None = None,
    claimed_reference_cents: int | None = None,
    claimed_reference_kind: str | None = None,
    claimed_inflated: bool | None = None,
    reject_reason: str | None = None,
    hold_reason: str | None = None,
) -> int:
    import json

    cur.execute(
        """
        INSERT INTO deals (
            offer_id, rule, lane, status, detected_observation_id, confirming_observation_id,
            detected_via, price_cents, reference_kind, reference_cents, reference_detail,
            claimed_reference_cents, claimed_reference_kind, claimed_inflated,
            discount_pct, reject_reason, hold_reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            offer_id,
            rule,
            lane,
            status,
            detected_observation_id,
            confirming_observation_id,
            detected_via,
            price_cents,
            reference_kind,
            reference_cents,
            json.dumps(reference_detail or {}),
            claimed_reference_cents,
            claimed_reference_kind,
            claimed_inflated,
            discount_pct,
            reject_reason,
            hold_reason,
        ),
    )
    return cur.fetchone()[0]
