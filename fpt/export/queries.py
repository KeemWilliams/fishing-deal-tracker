"""Read-only SQL for the feed export (architecture doc section 3.4).

Every function here takes an open `psycopg.Connection` and returns plain
dicts/lists -- no ORM, matching the rest of this codebase (`fpt/db.py` is
intentionally schema-agnostic; this module is the one place in the export
package that knows the deal/catalog schema). Nothing here writes -- the
export process is read-only against the production database, which is why
`fpt export` can run on a read replica or with a read-only role in the
future without code changes.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.rows import dict_row

# Matches confirm.py's ConfirmContext.min_discount_pct default and the
# architecture doc's headline rule (section 1, 6.3). Not read from the DB
# because it is a publish-time threshold, not a per-deal fact.
MIN_DISCOUNT_PCT = 50

_RETAILER_META_SQL = """
SELECT
  r.slug,
  r.name,
  r.enabled,
  r.breaker_open_until,
  (SELECT max(fl.fetched_at) FROM fetch_log fl
     WHERE fl.retailer_id = r.id AND fl.outcome = 'OK') AS last_successful_fetch_at,
  (SELECT max(dp.last_swept_at) FROM discovery_pages dp
     WHERE dp.retailer_id = r.id) AS last_discovery_sweep_at,
  (SELECT COALESCE(sum(h.fetched), 0) FROM retailer_health_hourly h
     WHERE h.retailer_id = r.id AND h.hour_utc >= now() - interval '24 hours') AS fetched_24h,
  (SELECT COALESCE(sum(h.blocked), 0) FROM retailer_health_hourly h
     WHERE h.retailer_id = r.id AND h.hour_utc >= now() - interval '24 hours') AS blocked_24h
FROM retailers r
WHERE r.enabled
ORDER BY r.slug;
"""

_ACTIVE_OFFER_COUNT_SQL = "SELECT count(*) AS n FROM offers WHERE is_active;"

# Only ACTIVE deals are ever exported -- this is the "only confirmed deals"
# publish rule. ACTIVE is reached only via the two-fetch confirmation path
# (fpt/deals/confirm.py); CANDIDATE/CONFIRMING/HELD_REVIEW/REJECTED/EXPIRED
# never leave this query. The explicit `confirming_observation_id IS NOT
# NULL` guard is belt-and-suspenders with the status filter: per the
# store-layer's in-progress migration 014, a confirmed/ACTIVE deal must
# always carry a confirming observation, but the export never trusts status
# alone for a fact this cheap to re-check at read time.
#
# M1: CLAIMED-lane deals are excluded from the public feed entirely for now
# (owner's explicit default: hide a retailer's own unverified "was" claim
# until our own cross-retailer/history evidence exists) -- they are still
# fully stored and can later flip to VERIFIED on their own (architecture
# 6.1's auto-upgrade path); this filter only affects what gets published.
#
# L2: a USED/OPEN_BOX/REFURB deal is excluded whenever shipping is unknown
# for its most recent observation -- an unknown-shipping used item's total
# cost to the buyer cannot be verified against the 50% claim, so it must
# not be published as one (a NEW/CLAIMED deal's shipping is not part of its
# own discount math and is unaffected by this guard).
_ACTIVE_DEALS_SQL = """
SELECT
  d.pub_id AS deal_id,
  d.lane,
  d.rule,
  p.slug AS product_slug,
  p.name AS product_name,
  b.name AS brand,
  p.category,
  pv.pub_id AS variant_pub_id,
  pv.label AS variant_label,
  r.slug AS retailer_slug,
  r.name AS retailer_name,
  l.url AS retailer_url,
  o.condition,
  COALESCE(s.name, s.seller_key) AS seller_name,
  s.seller_type,
  d.price_cents,
  lo.shipping_cents,
  d.landed_price_cents,
  d.reference_kind,
  d.reference_cents,
  d.reference_detail,
  d.claimed_reference_kind,
  d.claimed_reference_cents,
  d.claimed_inflated,
  d.discount_pct,
  lo.availability,
  lo.stock_qty,
  d.confirmed_at,
  d.detected_at,
  lo.observed_at AS last_observed_at
FROM deals d
JOIN offers o ON o.id = d.offer_id
JOIN listings l ON l.id = o.listing_id
JOIN retailers r ON r.id = l.retailer_id
JOIN sellers s ON s.id = o.seller_id
JOIN product_variants pv ON pv.id = l.variant_id
JOIN products p ON p.id = pv.product_id
JOIN brands b ON b.id = p.brand_id
LEFT JOIN LATERAL (
  SELECT po.shipping_cents, po.availability, po.stock_qty, po.observed_at
  FROM price_observations po
  WHERE po.id = COALESCE(
    d.last_confirmed_observation_id, d.confirming_observation_id, d.detected_observation_id
  )
) lo ON true
WHERE d.status = 'ACTIVE'
  AND d.confirming_observation_id IS NOT NULL
  AND d.lane != 'CLAIMED'
  AND (d.lane != 'USED' OR lo.shipping_cents IS NOT NULL)
ORDER BY d.discount_pct DESC, d.detected_at DESC;
"""

_PRODUCTS_WITH_VARIANTS_SQL = """
SELECT
  p.slug AS product_slug,
  p.name AS product_name,
  b.name AS brand,
  p.category,
  pv.id AS variant_id,
  pv.pub_id AS variant_pub_id,
  pv.label AS variant_label,
  pv.attributes
FROM products p
JOIN brands b ON b.id = p.brand_id
JOIN product_variants pv ON pv.product_id = p.id
WHERE p.slug = ANY(%(slugs)s)
ORDER BY p.slug, pv.label;
"""

# One row per (variant, retailer, condition, seller) currently-active offer,
# carrying the most recent OK observation for that offer -- but ONLY when
# that observation is recent (L3: "product-page offers limited to recent
# observations"). Without this filter, a variant page could keep showing an
# offer whose last successful fetch was weeks ago as if it were current,
# with only the (buried) `observed_at` timestamp to notice -- this is the
# same "no fetch in 12h" staleness window the deals feed's retailer health
# uses (fpt.export.build.STALE_AFTER_HOURS), applied per-offer here. Used
# to build products/<slug>.json's `offers` array -- deliberately NOT
# filtered to the deal's own retailer, so the product page shows every
# retailer/condition currently and recently tracked for that variant.
_VARIANT_OFFERS_SQL = """
WITH latest_obs AS (
  SELECT DISTINCT ON (po.offer_id)
    po.offer_id, po.price_cents, po.shipping_cents, po.availability,
    po.on_clearance, po.observed_at
  FROM price_observations po
  WHERE po.quality = 'OK'
    AND po.observed_at >= now() - (%(stale_hours)s::text || ' hours')::interval
  ORDER BY po.offer_id, po.observed_at DESC
)
SELECT
  l.variant_id,
  r.slug AS retailer_slug,
  r.name AS retailer_name,
  l.url,
  o.condition,
  COALESCE(s.name, s.seller_key) AS seller_name,
  s.seller_type,
  lo.price_cents,
  lo.shipping_cents,
  lo.availability,
  lo.on_clearance,
  lo.observed_at
FROM offers o
JOIN listings l ON l.id = o.listing_id
JOIN retailers r ON r.id = l.retailer_id
JOIN sellers s ON s.id = o.seller_id
JOIN latest_obs lo ON lo.offer_id = o.id
WHERE o.is_active AND l.variant_id = ANY(%(variant_ids)s)
ORDER BY l.variant_id, lo.price_cents NULLS LAST;
"""

# 90-day daily price history per (variant, retailer, condition_group),
# taking the lowest offer price observed that day within the group -- a
# display aggregate only, never fed back into deal math (fpt/deals owns
# that, via the offer_reference_stats view).
_VARIANT_HISTORY_SQL = """
SELECT
  l.variant_id,
  r.slug AS retailer_slug,
  o.condition_group,
  odp.day_et AS day,
  min(odp.price_cents) AS price_cents
FROM offer_daily_price odp
JOIN offers o ON o.id = odp.offer_id
JOIN listings l ON l.id = o.listing_id
JOIN retailers r ON r.id = l.retailer_id
WHERE l.variant_id = ANY(%(variant_ids)s)
  AND odp.day_et >= (CURRENT_DATE - INTERVAL '90 days')
GROUP BY l.variant_id, r.slug, o.condition_group, odp.day_et
ORDER BY l.variant_id, odp.day_et;
"""


def _fetch_all(conn: "psycopg.Connection", sql: str, params: dict[str, Any] | None = None) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params or {})
        return list(cur.fetchall())


def fetch_retailer_rows(conn: "psycopg.Connection") -> list[dict]:
    return _fetch_all(conn, _RETAILER_META_SQL)


def fetch_active_offer_count(conn: "psycopg.Connection") -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_ACTIVE_OFFER_COUNT_SQL)
        row = cur.fetchone()
        return int(row["n"]) if row else 0


def fetch_active_deal_rows(conn: "psycopg.Connection") -> list[dict]:
    return _fetch_all(conn, _ACTIVE_DEALS_SQL)


def fetch_products_with_variants(conn: "psycopg.Connection", slugs: list[str]) -> list[dict]:
    if not slugs:
        return []
    return _fetch_all(conn, _PRODUCTS_WITH_VARIANTS_SQL, {"slugs": slugs})


def fetch_variant_offers(conn: "psycopg.Connection", variant_ids: list[int], *, stale_hours: int = 12) -> list[dict]:
    if not variant_ids:
        return []
    return _fetch_all(conn, _VARIANT_OFFERS_SQL, {"variant_ids": variant_ids, "stale_hours": stale_hours})


def fetch_variant_history(conn: "psycopg.Connection", variant_ids: list[int]) -> list[dict]:
    if not variant_ids:
        return []
    return _fetch_all(conn, _VARIANT_HISTORY_SQL, {"variant_ids": variant_ids})
