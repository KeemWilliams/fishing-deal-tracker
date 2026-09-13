"""Deal candidate/confirmation persistence (db/migrations 008_deals),
wiring the pure fpt/deals/{detect,confirm,references}.py functions to
live query results.

Reference resolution here queries `price_observations` / `listings` /
`offers` directly rather than the `offer_daily_price` / `offer_reference_
stats` / `variant_current_new` materialized views (db/migrations
009_views.sql, used by fpt/export/queries.py for the read-side feed).
Two reasons, both deliberate:

  1. Those views are refreshed on a best-effort cadence (architecture doc:
     "at most every 30 minutes") -- fine for the public export, too stale
     for the confirmation path, which must see an observation inserted
     moments ago in the SAME tick.
  2. `REFRESH MATERIALIZED VIEW CONCURRENTLY` inside an already-open
     transaction (this store's callers, and every integration test here,
     run inside one) is a real risk not worth taking for this task -- a
     plain SELECT against the base tables sees this transaction's own
     uncommitted writes correctly under MVCC and needs no refresh step.

Idempotency for deal rows leans on the DB's own `deals_one_open_per_offer_
rule` unique index (migration 008): `create_candidate_deal` catches the
resulting UniqueViolation and returns the existing open deal instead of
raising, so calling this twice for the same detected observation is safe.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import psycopg

from fpt.core.models import CONDITION_GROUP
from fpt.deals.confirm import ConfirmContext, ConfirmResult
from fpt.deals.detect import DealCandidate
from fpt.deals.references import (
    CrossRetailerCandidate,
    NonClearanceDay,
    OwnHistoryResult,
    VerifiedReference,
    resolve_cross_retailer_new,
    resolve_own_history_median,
    resolve_used_reference,
    resolve_verified_reference,
)

_OWN_HISTORY_WINDOW_DAYS = 90
_CROSS_RETAILER_MAX_AGE_DAYS = 7
_CURRENT_NEW_MAX_AGE_HOURS = 48


def get_offer_context(cur, offer_id: int) -> dict | None:
    cur.execute(
        """
        SELECT o.condition_group, l.variant_id, l.id AS listing_id, l.match_status,
               s.seller_type, l.retailer_id
        FROM offers o
        JOIN listings l ON l.id = o.listing_id
        JOIN sellers s ON s.id = o.seller_id
        WHERE o.id = %s
        """,
        (offer_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {
        "condition_group": row[0],
        "variant_id": row[1],
        "listing_id": row[2],
        "match_status": row[3],
        "seller_type": row[4],
        "retailer_id": row[5],
    }


def resolve_own_history(cur, offer_id: int) -> OwnHistoryResult | None:
    """R1: median of daily OK, non-clearance prices for this offer over the
    trailing 90 days -- one price per calendar day (last OK observation of
    the day), matching offer_daily_price's grain without querying it."""
    cur.execute(
        """
        SELECT DISTINCT ON (observed_date_et) observed_date_et, price_cents
        FROM price_observations
        WHERE offer_id = %s AND quality = 'OK' AND NOT on_clearance
          AND observed_date_et >= (CURRENT_DATE - %s::int)
        ORDER BY observed_date_et, observed_at DESC
        """,
        (offer_id, _OWN_HISTORY_WINDOW_DAYS),
    )
    days = [NonClearanceDay(day=row[0], price_cents=row[1]) for row in cur.fetchall()]
    return resolve_own_history_median(days)


def resolve_cross_retailer(cur, *, offer_id: int, variant_id: int | None, condition_group: str) -> CrossRetailerCandidate | None:
    """R2: NEW offers only, other listings matched to the same variant_id
    with a verifiable match_status, most recent in-stock OK observation
    within the last 7 days."""
    if variant_id is None or condition_group != "NEW":
        return None
    cur.execute(
        """
        SELECT DISTINCT ON (l2.retailer_id)
          r2.slug, l2.match_status, po2.price_cents
        FROM listings l2
        JOIN offers o2 ON o2.listing_id = l2.id AND o2.condition_group = 'NEW' AND o2.is_active
        JOIN sellers s2 ON s2.id = o2.seller_id AND s2.seller_type = 'FIRST_PARTY'
        JOIN retailers r2 ON r2.id = l2.retailer_id
        JOIN LATERAL (
          SELECT price_cents, observed_at
          FROM price_observations po
          WHERE po.offer_id = o2.id AND po.quality = 'OK' AND po.availability = 'IN_STOCK'
            AND po.observed_at >= now() - (%s::text || ' days')::interval
          ORDER BY po.observed_at DESC LIMIT 1
        ) po2 ON true
        WHERE l2.variant_id = %s AND o2.id != %s
        ORDER BY l2.retailer_id, po2.observed_at DESC
        """,
        (_CROSS_RETAILER_MAX_AGE_DAYS, variant_id, offer_id),
    )
    candidates = [
        CrossRetailerCandidate(retailer_slug=row[0], price_cents=row[2], is_r1_quality=False, match_status=row[1])
        for row in cur.fetchall()
    ]
    return resolve_cross_retailer_new(candidates)


def resolve_current_new_for_variant(cur, *, variant_id: int | None, exclude_offer_id: int) -> int | None:
    """U1: lowest current confirmed in-stock NEW price for the same
    variant, landed price when shipping is known, across ANY retailer
    (including the used offer's own listing's NEW sibling offer, per
    architecture 6.1's U1 definition) -- a used offer is never referenced
    against its own or any other NEW offer's own R1/R2/R3/R4 history,
    only this direct current-price lookup."""
    if variant_id is None:
        return None
    cur.execute(
        """
        SELECT DISTINCT ON (l2.retailer_id)
          COALESCE(po2.price_cents + po2.shipping_cents, po2.price_cents) AS landed_price_cents
        FROM listings l2
        JOIN offers o2 ON o2.listing_id = l2.id AND o2.condition_group = 'NEW' AND o2.is_active
        JOIN LATERAL (
          SELECT price_cents, shipping_cents, observed_at
          FROM price_observations po
          WHERE po.offer_id = o2.id AND po.quality = 'OK' AND po.availability = 'IN_STOCK'
            AND po.observed_at >= now() - (%s::text || ' hours')::interval
          ORDER BY po.observed_at DESC LIMIT 1
        ) po2 ON true
        WHERE l2.variant_id = %s AND o2.id != %s
        ORDER BY l2.retailer_id, po2.observed_at DESC
        """,
        (_CURRENT_NEW_MAX_AGE_HOURS, variant_id, exclude_offer_id),
    )
    prices = [row[0] for row in cur.fetchall() if row[0] is not None]
    return resolve_used_reference(prices)


def resolve_verified_reference_for_offer(cur, offer_id: int) -> VerifiedReference | None:
    ctx = get_offer_context(cur, offer_id)
    if ctx is None:
        return None
    own_history = resolve_own_history(cur, offer_id)
    cross_retailer = resolve_cross_retailer(
        cur, offer_id=offer_id, variant_id=ctx["variant_id"], condition_group=ctx["condition_group"]
    )
    return resolve_verified_reference(own_history=own_history, cross_retailer=cross_retailer)


@dataclass(frozen=True)
class OpenDeal:
    id: int
    rule: str
    lane: str
    detected_observation_id: int
    detected_at: datetime
    detected_via: str
    price_cents: int
    status: str


def find_open_deal(cur, *, offer_id: int, rule: str) -> OpenDeal | None:
    cur.execute(
        """
        SELECT id, rule, lane, detected_observation_id, detected_at, detected_via, price_cents, status
        FROM deals
        WHERE offer_id = %s AND rule = %s
          AND status IN ('CANDIDATE', 'CONFIRMING', 'ACTIVE', 'HELD_REVIEW')
        ORDER BY detected_at DESC LIMIT 1
        """,
        (offer_id, rule),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return OpenDeal(
        id=row[0], rule=row[1], lane=row[2], detected_observation_id=row[3],
        detected_at=row[4], detected_via=row[5], price_cents=row[6], status=row[7],
    )


def create_candidate_deal(
    cur,
    *,
    offer_id: int,
    candidate: DealCandidate,
    detected_observation_id: int,
    detected_via: str,
    reference_kind: str,
    reference_detail: dict,
    detected_at: datetime,
    claimed_reference_cents: int | None = None,
    claimed_reference_kind: str | None = None,
    claimed_inflated: bool | None = None,
) -> OpenDeal:
    """Inserts a CANDIDATE deal. If one is already open for this
    (offer_id, rule) -- e.g. this observation was already processed, or a
    concurrent tick raced us -- returns the existing row instead of
    raising, per the `deals_one_open_per_offer_rule` unique index.

    `detected_at` is set explicitly to the detecting observation's own
    `observed_at` -- NOT left to the column's `DEFAULT now()` -- because
    C1's "at least 10 minutes after the detecting fetch" gap (confirm.py)
    is computed against this timestamp. Relying on wall-clock insert time
    would silently break confirmation for any fetch whose `observed_at` is
    not the literal current instant (every historical/backfill fetch, and
    every test)."""
    cur.execute("SAVEPOINT before_deal_insert")
    try:
        cur.execute(
            """
            INSERT INTO deals (
                offer_id, rule, lane, status, detected_observation_id, detected_via,
                price_cents, reference_kind, reference_cents, reference_detail,
                claimed_reference_cents, claimed_reference_kind, claimed_inflated, discount_pct,
                detected_at
            ) VALUES (%s, %s, %s, 'CONFIRMING', %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            RETURNING id, detected_at, status
            """,
            (
                offer_id, candidate.rule, candidate.lane, detected_observation_id, detected_via,
                candidate.price_cents, reference_kind, candidate.reference_cents, json.dumps(reference_detail),
                claimed_reference_cents, claimed_reference_kind, claimed_inflated, candidate.discount_pct,
                detected_at,
            ),
        )
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT before_deal_insert")
        return OpenDeal(
            id=row[0], rule=candidate.rule, lane=candidate.lane, detected_observation_id=detected_observation_id,
            detected_at=row[1], detected_via=detected_via, price_cents=candidate.price_cents, status=row[2],
        )
    except psycopg.errors.UniqueViolation:
        cur.execute("ROLLBACK TO SAVEPOINT before_deal_insert")
        existing = find_open_deal(cur, offer_id=offer_id, rule=candidate.rule)
        if existing is None:  # pragma: no cover - defensive; the unique index guarantees one exists
            raise
        return existing


def build_confirm_context(
    cur,
    *,
    open_deal: OpenDeal,
    offer_id: int,
    confirming_observation_id: int,
    confirming_observed_at: datetime,
    confirming_page_type: str,
    confirming_price_cents: int,
    confirming_quality: str,
    confirming_quality_reasons: list[str],
    detected_retailer_sku: str,
    confirming_retailer_sku: str,
    detected_offer_key: str,
    confirming_offer_key: str,
    detected_condition: str,
    confirming_condition: str,
    detected_seller_key: str,
    confirming_seller_key: str,
    detected_variant_key_observed: str | None,
    confirming_variant_key_observed: str | None,
    listing_variant_key_observed: str | None,
    detected_unit_count: int | None,
    confirming_unit_count: int | None,
    listing_unit_count: int | None,
    category_floor_cents: int | None,
) -> ConfirmContext:
    """Recomputes the reference fresh (C8's "freshly resolved reference")
    from the confirming observation's own values, and recomputes the
    discount against it -- never reuses the CANDIDATE-time discount."""
    ctx = get_offer_context(cur, offer_id)
    offer_condition_group = ctx["condition_group"] if ctx else CONDITION_GROUP.get(confirming_condition, "USED")
    offer_variant_key = confirming_variant_key_observed or listing_variant_key_observed

    if open_deal.lane == "USED":
        reference_cents = resolve_current_new_for_variant(
            cur, variant_id=ctx["variant_id"] if ctx else None, exclude_offer_id=offer_id
        )
        recomputed_reference_variant_key = offer_variant_key
        recomputed_reference_condition_group = offer_condition_group
    else:
        verified = resolve_verified_reference_for_offer(cur, offer_id)
        reference_cents = verified.cents if verified else None
        recomputed_reference_variant_key = offer_variant_key if verified else None
        recomputed_reference_condition_group = "NEW" if verified else None

    if reference_cents and reference_cents > 0:
        recomputed_discount_pct = round((1 - (confirming_price_cents / reference_cents)) * 100, 2)
    else:
        recomputed_discount_pct = 0.0

    return ConfirmContext(
        detected_at=open_deal.detected_at,
        confirming_observed_at=confirming_observed_at,
        detected_via=open_deal.detected_via,
        confirming_page_type=confirming_page_type,
        detected_retailer_sku=detected_retailer_sku,
        confirming_retailer_sku=confirming_retailer_sku,
        detected_offer_key=detected_offer_key,
        confirming_offer_key=confirming_offer_key,
        detected_condition=detected_condition,
        confirming_condition=confirming_condition,
        detected_seller_key=detected_seller_key,
        confirming_seller_key=confirming_seller_key,
        detected_variant_key_observed=detected_variant_key_observed,
        confirming_variant_key_observed=confirming_variant_key_observed,
        listing_variant_key_observed=listing_variant_key_observed,
        detected_unit_count=detected_unit_count,
        confirming_unit_count=confirming_unit_count,
        listing_unit_count=listing_unit_count,
        confirming_quality=confirming_quality,
        confirming_quality_reasons=confirming_quality_reasons,
        detected_price_cents=open_deal.price_cents,
        confirming_price_cents=confirming_price_cents,
        recomputed_discount_pct=recomputed_discount_pct,
        recomputed_reference_variant_key=recomputed_reference_variant_key,
        recomputed_reference_condition_group=recomputed_reference_condition_group,
        offer_variant_key=offer_variant_key,
        offer_condition_group=offer_condition_group,
        confirming_price_cents_for_floor=confirming_price_cents,
        category_floor_cents=category_floor_cents,
    )


def apply_confirm_result(cur, *, deal_id: int, confirming_observation_id: int, result: ConfirmResult) -> None:
    """Sets status AND confirming_observation_id together in one statement
    for ACTIVE/HELD_REVIEW, per migration 014's
    `deals_confirmed_status_requires_observation` CHECK -- REJECTED is the
    one status that constraint deliberately allows without a confirming
    observation (confirm_deadline_missed / retailer_blocked can reject
    before any CONFIRM fetch ever happens), so this function still records
    the confirming_observation_id when it exists (it always does when this
    function is reached from `record_and_confirm`) to preserve the audit
    trail even on rejection."""
    if result.status in ("ACTIVE", "HELD_REVIEW"):
        cur.execute(
            """
            UPDATE deals
            SET status = %s, confirming_observation_id = %s, last_confirmed_observation_id = %s,
                hold_reason = %s, confirmed_at = CASE WHEN %s = 'ACTIVE' THEN now() ELSE confirmed_at END
            WHERE id = %s
            """,
            (result.status, confirming_observation_id, confirming_observation_id, result.hold_reason, result.status, deal_id),
        )
    else:
        cur.execute(
            "UPDATE deals SET status = 'REJECTED', confirming_observation_id = %s, reject_reason = %s WHERE id = %s",
            (confirming_observation_id, result.reject_reason, deal_id),
        )
