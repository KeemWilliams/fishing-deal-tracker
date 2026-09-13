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
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import yaml

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
_DEAL_RULES_CONFIG = Path(__file__).resolve().parents[2] / "config" / "deal_rules.yaml"
_DEFAULT_STALE_HOURS = 24
_DEFAULT_MAX_PRICE_INCREASE_PCT = 2.0
_DEFAULT_MIN_DISCOUNT_PCT = 50.0


def _load_deal_rules(config_path: Path | None = None) -> dict:
    path = config_path or _DEAL_RULES_CONFIG
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data.get("deal_rules") or {}


def _load_stale_hours(config_path: Path | None = None) -> int:
    rules = _load_deal_rules(config_path)
    return int((rules.get("expiry") or {}).get("stale_hours", _DEFAULT_STALE_HOURS))
_CURRENT_NEW_MAX_AGE_HOURS = 48


def get_offer_context(cur, offer_id: int) -> dict | None:
    """Loaded directly from the stored `listings` row (never from a
    fresh parse) so H3/H4 checks compare against what the DB actually has
    on file for this offer's listing, not whatever the current fetch
    happened to produce. `unit_count` and `category` (via the listing's
    matched variant/product, when matched) back the H4 cross-retailer
    matching guards below."""
    cur.execute(
        """
        SELECT o.condition_group, l.variant_id, l.id AS listing_id, l.match_status,
               s.seller_type, l.retailer_id, l.unit_count, l.variant_key_observed, p.category
        FROM offers o
        JOIN listings l ON l.id = o.listing_id
        JOIN sellers s ON s.id = o.seller_id
        LEFT JOIN product_variants pv ON pv.id = l.variant_id
        LEFT JOIN products p ON p.id = pv.product_id
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
        "unit_count": row[6],
        "variant_key_observed": row[7],
        "category": row[8],
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


def resolve_cross_retailer(
    cur,
    *,
    offer_id: int,
    variant_id: int | None,
    condition_group: str,
    unit_count: int | None = None,
    category: str | None = None,
) -> CrossRetailerCandidate | None:
    """R2: NEW offers only, other listings matched to the same variant_id
    with a verifiable match_status, most recent in-stock OK observation
    within the last 7 days.

    H4 guards (belt-and-suspenders on top of catalog.py's own GTIN
    matching, re-checked here at resolution time against the stored
    listing rows -- never the current parse):
      - `unit_count` must match on BOTH sides and neither may be NULL.
        NULL is never comparable: two listings that both simply don't
        record a pack count are NOT thereby "the same" pack count, so a
        NULL own `unit_count` disqualifies R2 entirely rather than
        matching every other unit_count-less listing.
      - the candidate listing's product must be in the SAME category as
        this offer's own product (a cross-category GTIN collision, or a
        variant merged into the wrong product, must never verify a
        reference).
      - any listing with a recorded `gtin_attr_conflict` against the
        variant it would be joined through is excluded outright, even if
        its `match_status` somehow does not already reflect that (the
        match_status filter below already excludes 'needs_review', which
        is what catalog.py sets for a conflict -- this is the explicit,
        redundant check requested for this fix)."""
    if variant_id is None or condition_group != "NEW":
        return None
    if unit_count is None:
        return None
    cur.execute(
        """
        SELECT DISTINCT ON (l2.retailer_id)
          r2.slug, l2.match_status, po2.price_cents, l2.variant_key_observed, l2.unit_count
        FROM listings l2
        JOIN offers o2 ON o2.listing_id = l2.id AND o2.condition_group = 'NEW' AND o2.is_active
        JOIN sellers s2 ON s2.id = o2.seller_id AND s2.seller_type = 'FIRST_PARTY'
        JOIN retailers r2 ON r2.id = l2.retailer_id
        JOIN product_variants pv2 ON pv2.id = l2.variant_id
        JOIN products p2 ON p2.id = pv2.product_id
        JOIN LATERAL (
          SELECT price_cents, observed_at
          FROM price_observations po
          WHERE po.offer_id = o2.id AND po.quality = 'OK' AND po.availability = 'IN_STOCK'
            AND po.observed_at >= now() - (%(days)s::text || ' days')::interval
          ORDER BY po.observed_at DESC LIMIT 1
        ) po2 ON true
        WHERE l2.variant_id = %(variant_id)s AND o2.id != %(offer_id)s
          AND l2.unit_count IS NOT NULL AND l2.unit_count = %(unit_count)s
          AND (%(category)s::text IS NULL OR p2.category = %(category)s)
          AND NOT EXISTS (
            SELECT 1 FROM match_candidates mc
            WHERE mc.listing_id = l2.id AND mc.variant_id = l2.variant_id
              AND mc.reason = 'gtin_attr_conflict'
          )
        ORDER BY l2.retailer_id, po2.observed_at DESC
        """,
        {
            "days": _CROSS_RETAILER_MAX_AGE_DAYS,
            "variant_id": variant_id,
            "offer_id": offer_id,
            "unit_count": unit_count,
            "category": category,
        },
    )
    candidates = [
        CrossRetailerCandidate(
            retailer_slug=row[0],
            price_cents=row[2],
            is_r1_quality=False,
            match_status=row[1],
            variant_key_observed=row[3],
            unit_count=row[4],
        )
        for row in cur.fetchall()
    ]
    return resolve_cross_retailer_new(candidates)


def get_observation_identity(cur, observation_id: int | None) -> dict | None:
    """H3: the DETECTING observation's own recorded `variant_key_observed`
    and `unit_count`, loaded fresh from `price_observations` by id -- never
    substituted with whatever the CURRENT (confirming) fetch's parse
    produced. This is one half of what C4 must compare the confirming
    observation and the stored listing row against."""
    if observation_id is None:
        return None
    cur.execute(
        "SELECT variant_key_observed, unit_count FROM price_observations WHERE id = %s",
        (observation_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"variant_key_observed": row[0], "unit_count": row[1]}


@dataclass(frozen=True)
class CurrentNewReference:
    cents: int
    variant_key_observed: str | None
    unit_count: int | None


def resolve_current_new_reference_with_identity(
    cur, *, variant_id: int | None, exclude_offer_id: int
) -> CurrentNewReference | None:
    """Same U1 selection as `resolve_current_new_for_variant`, but also
    returns the winning reference listing's own `variant_key_observed` and
    `unit_count` (H3/C8: "the reference offer's own variant key and unit
    count returned by the reference lookup"). Used only by the
    confirmation path (`build_confirm_context`), which must verify C8
    against the reference actually used -- never assume it shares this
    offer's own identity, which is only true by coincidence for U1 since
    the reference is a DIFFERENT (NEW) offer, potentially at a different
    retailer, from the USED offer being confirmed."""
    if variant_id is None:
        return None
    cur.execute(
        """
        SELECT DISTINCT ON (l2.retailer_id)
          COALESCE(po2.price_cents + po2.shipping_cents, po2.price_cents) AS landed_price_cents,
          l2.variant_key_observed, l2.unit_count
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
    rows = [(r[0], r[1], r[2]) for r in cur.fetchall() if r[0] is not None]
    if not rows:
        return None
    cents, variant_key_observed, unit_count = min(rows, key=lambda r: r[0])
    return CurrentNewReference(cents=cents, variant_key_observed=variant_key_observed, unit_count=unit_count)


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
        cur,
        offer_id=offer_id,
        variant_id=ctx["variant_id"],
        condition_group=ctx["condition_group"],
        unit_count=ctx["unit_count"],
        category=ctx["category"],
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
    confirming_retailer_sku: str,
    confirming_offer_key: str,
    confirming_condition: str,
    confirming_seller_key: str,
    confirming_variant_key_observed: str | None,
    confirming_unit_count: int | None,
    category_floor_cents: int | None,
    confirming_claimed_reference_cents: int | None = None,
) -> ConfirmContext:
    """Recomputes the reference fresh (C8's "freshly resolved reference")
    from the confirming observation's own values, and recomputes the
    discount against it -- never reuses the CANDIDATE-time discount.

    H3: the DETECTED side of C3/C4's identity checks is loaded here, fresh
    from the DB, rather than accepted as caller-supplied parameters --
    the detecting observation's own row (`price_observations`, by
    `open_deal.detected_observation_id`) and the listing's own stored row
    (`listings`, via `get_offer_context`). Both were previously passed in
    by the caller using the CURRENT (confirming) parse's values for every
    side of the comparison, which made C4 compare the confirming fetch
    against itself and could never fail.

    The offer-identity fields (retailer_sku/offer_key/condition/seller_key)
    are NOT re-derived historically here: `offer_id` is fixed for the life
    of a deal, so those four are properties of the persistent offer entity,
    not a per-fetch snapshot -- both the detecting and confirming fetch are
    checking identity against the SAME offer row by construction."""
    ctx = get_offer_context(cur, offer_id) or {}
    offer_condition_group = ctx.get("condition_group") or CONDITION_GROUP.get(confirming_condition, "USED")

    detected = get_observation_identity(cur, open_deal.detected_observation_id)
    detected_variant_key_observed = detected["variant_key_observed"] if detected else None
    detected_unit_count = detected["unit_count"] if detected else None
    listing_variant_key_observed = ctx.get("variant_key_observed")
    listing_unit_count = ctx.get("unit_count")

    offer_variant_key = confirming_variant_key_observed or listing_variant_key_observed
    offer_unit_count = confirming_unit_count if confirming_unit_count is not None else listing_unit_count

    reference_cents: int | None
    recomputed_reference_variant_key: str | None = None
    recomputed_reference_unit_count: int | None = None
    recomputed_reference_condition_group: str | None = None

    if open_deal.lane == "USED":
        current_new = resolve_current_new_reference_with_identity(
            cur, variant_id=ctx.get("variant_id"), exclude_offer_id=offer_id
        )
        reference_cents = current_new.cents if current_new else None
        if current_new is not None:
            # C8: the reference offer's OWN variant key/unit count, per H3 --
            # this is a DIFFERENT (NEW) offer than the one being confirmed.
            recomputed_reference_variant_key = current_new.variant_key_observed
            recomputed_reference_unit_count = current_new.unit_count
            recomputed_reference_condition_group = "NEW"
    else:
        verified = resolve_verified_reference_for_offer(cur, offer_id)
        reference_cents = verified.cents if verified else None
        if verified is not None:
            if verified.kind == "CROSS_RETAILER_NEW":
                recomputed_reference_variant_key = verified.detail.get("variant_key_observed")
                recomputed_reference_unit_count = verified.detail.get("unit_count")
            else:
                # OWN_HISTORY_MEDIAN_90D (and DATA_API_HISTORY, Phase 2) are
                # this SAME offer's own history -- its reference identity is
                # this offer's own stored identity.
                recomputed_reference_variant_key = offer_variant_key
                recomputed_reference_unit_count = offer_unit_count
            recomputed_reference_condition_group = "NEW"
        elif open_deal.lane == "CLAIMED" and confirming_claimed_reference_cents:
            # Architecture 6.1: a CLAIMED-lane deal has no VERIFIED
            # reference by definition -- its reference IS the retailer's
            # own claimed price (R4). C7's "freshly resolved reference"
            # must therefore re-check against the CONFIRMING fetch's own
            # fresh claimed_reference_cents (this offer's own identity,
            # same as the own-history case), not silently fall through to
            # `reference_cents = None` and reject every CLAIMED deal at
            # confirmation regardless of price. If a VERIFIED reference has
            # since appeared, the branch above already took it -- R1/R2
            # always outrank R4 per 6.1's "a VERIFIED reference always
            # wins", auto-upgrading the deal's numbers without a lane
            # change (a full CLAIMED->VERIFIED lane promotion is tracked
            # separately, out of this fix's scope).
            reference_cents = confirming_claimed_reference_cents
            recomputed_reference_variant_key = offer_variant_key
            recomputed_reference_unit_count = offer_unit_count
            recomputed_reference_condition_group = "NEW"

    if reference_cents and reference_cents > 0:
        recomputed_discount_pct = round((1 - (confirming_price_cents / reference_cents)) * 100, 2)
    else:
        recomputed_discount_pct = 0.0

    return ConfirmContext(
        detected_at=open_deal.detected_at,
        confirming_observed_at=confirming_observed_at,
        detected_via=open_deal.detected_via,
        confirming_page_type=confirming_page_type,
        detected_retailer_sku=confirming_retailer_sku,
        confirming_retailer_sku=confirming_retailer_sku,
        detected_offer_key=confirming_offer_key,
        confirming_offer_key=confirming_offer_key,
        detected_condition=confirming_condition,
        confirming_condition=confirming_condition,
        detected_seller_key=confirming_seller_key,
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
        recomputed_reference_unit_count=recomputed_reference_unit_count,
        offer_variant_key=offer_variant_key,
        offer_condition_group=offer_condition_group,
        offer_unit_count=offer_unit_count,
        confirming_price_cents_for_floor=confirming_price_cents,
        category_floor_cents=category_floor_cents,
        reference_cents=reference_cents,
    )


def apply_confirm_result(
    cur, *, deal_id: int, confirming_observation_id: int, result: ConfirmResult, ctx: ConfirmContext | None = None
) -> None:
    """Sets status AND confirming_observation_id together in one statement
    for ACTIVE/HELD_REVIEW, per migration 014's
    `deals_confirmed_status_requires_observation` CHECK -- REJECTED is the
    one status that constraint deliberately allows without a confirming
    observation (confirm_deadline_missed / retailer_blocked can reject
    before any CONFIRM fetch ever happens), so this function still records
    the confirming_observation_id when it exists (it always does when this
    function is reached from `record_and_confirm`) to preserve the audit
    trail even on rejection.

    L1: when the verdict is ACTIVE/HELD_REVIEW and `ctx` (the same
    `ConfirmContext` `confirm_candidate` was evaluated against) is
    supplied, also writes the deal's live fields -- `price_cents`,
    `discount_pct`, `reference_cents` -- to the CONFIRM-time recomputed
    values rather than leaving them at the stale CANDIDATE-time snapshot.
    `ctx` is optional only so existing callers that pre-date this fix keep
    working during a rolling deploy; every call from this module's own
    pipeline wiring always passes it."""
    if result.status in ("ACTIVE", "HELD_REVIEW"):
        if ctx is not None:
            cur.execute(
                """
                UPDATE deals
                SET status = %s, confirming_observation_id = %s, last_confirmed_observation_id = %s,
                    hold_reason = %s, price_cents = %s, discount_pct = %s,
                    reference_cents = COALESCE(%s, reference_cents),
                    confirmed_at = CASE WHEN %s = 'ACTIVE' THEN now() ELSE confirmed_at END
                WHERE id = %s
                """,
                (
                    result.status, confirming_observation_id, confirming_observation_id, result.hold_reason,
                    ctx.confirming_price_cents, ctx.recomputed_discount_pct, ctx.reference_cents,
                    result.status, deal_id,
                ),
            )
        else:
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


def enqueue_confirm_task(
    cur,
    *,
    retailer_id: int,
    deal_id: int,
    url: str,
    category_hint: str,
    detected_at: datetime,
    min_delay_minutes: int = 10,
    deadline_hours: int = 3,
) -> bool:
    """Architecture doc 5.1/6.4 C1: a CANDIDATE deal's confirmation is a
    SEPARATE fetch at least `min_delay_minutes` after detection, enqueued
    here rather than performed inline -- this is the one thing this store
    layer deliberately never fakes. The queue drainer
    (fpt/scheduler/queue.py) only leases this task once `not_before` has
    passed, so confirmation always lands on a later tick in practice (a
    15-minute tick cadence vs. a 10-minute minimum gap).

    Deduped on `deal_id` (one open CONFIRM task per deal, via
    `crawl_tasks_dedupe_open`); returns False if one is already queued.
    """
    dedupe_key = f"CONFIRM:{deal_id}"
    not_before = detected_at + timedelta(minutes=min_delay_minutes)
    deadline = detected_at + timedelta(hours=deadline_hours)
    cur.execute("SAVEPOINT before_confirm_task")
    try:
        cur.execute(
            """
            INSERT INTO crawl_tasks (
                retailer_id, kind, priority, page_type, url, params, deal_id,
                dedupe_key, not_before, deadline, status
            ) VALUES (%s, 'CONFIRM', 10, 'PRODUCT', %s, %s::jsonb, %s, %s, %s, %s, 'QUEUED')
            """,
            (retailer_id, url, json.dumps({"category_hint": category_hint}), deal_id, dedupe_key, not_before, deadline),
        )
        cur.execute("RELEASE SAVEPOINT before_confirm_task")
        return True
    except psycopg.errors.UniqueViolation:
        cur.execute("ROLLBACK TO SAVEPOINT before_confirm_task")
        return False


# ---------------------------------------------------------------------------
# H2: ACTIVE deal lifecycle (architecture 6.3 -- "ACTIVE deals are
# re-checked on every HOT fetch", "EXPIRED when price rises above
# threshold, offer vanishes or goes out of stock, no OK fetch in 12h").
#
# Two complementary mechanisms:
#   1. `refresh_or_expire_active_deal` -- called by the pipeline for every
#      fresh OK observation of an offer that already carries an ACTIVE
#      deal. Recomputes against the CURRENT reference and either refreshes
#      the deal's live fields (L1) or expires it immediately.
#   2. `expire_stale_deals` -- a periodic sweep the tick calls once per run
#      for the one trigger an observation can never produce on its own:
#      staleness from the ABSENCE of a fresh observation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DealRecheckResult:
    action: str  # "refreshed" | "expired"
    expire_reason: str | None = None


def refresh_or_expire_active_deal(
    cur,
    *,
    open_deal: OpenDeal,
    offer_id: int,
    observation_id: int,
    compare_price_cents: int,
    landed_price_cents: int | None,
    availability: str,
    min_discount_pct: float | None = None,
    max_price_increase_pct: float | None = None,
) -> DealRecheckResult:
    """H2 + L1: called for every fresh OK observation of an offer whose
    open deal is already ACTIVE. Recomputes the discount against the
    CURRENT reference (never the CANDIDATE/CONFIRM-time snapshot) and
    either:
      - EXPIREs the deal (`expired_at`, `expire_reason`) when stock is no
        longer sellable, the reference disappeared, the price recovered
        above the confirm tolerance, or the recomputed discount fell below
        the headline threshold; or
      - refreshes the deal's live fields -- `price_cents`,
        `landed_price_cents`, `discount_pct`, `reference_cents`,
        `last_confirmed_observation_id` -- to the freshly observed values
        (L1), leaving `status` at ACTIVE.

    `compare_price_cents` must already be in the SAME basis
    `open_deal.price_cents` was stored in at candidate-creation time
    (`fpt/deals/detect.py`'s convention: the landed price for
    USED_VS_CURRENT_NEW, the raw item price for the other two rules) --
    the caller (fpt/store/pipeline.py) decides that per-rule, matching
    `_run_detection`'s own convention exactly so a rise/fall is measured
    against a consistent basis across the deal's whole lifecycle.
    `landed_price_cents` is stored separately, verbatim, for display
    (architecture 3.4: null when shipping is unknown).

    `not_observed` staleness (no fresh observation at all, for any offer)
    is NOT this function's concern -- there is no observation to recheck
    against in that case. See `expire_stale_deals` for that trigger."""
    thresholds = _load_deal_rules()
    confirm_cfg = thresholds.get("confirm") or {}
    min_discount = min_discount_pct if min_discount_pct is not None else thresholds.get("min_discount_pct", _DEFAULT_MIN_DISCOUNT_PCT)
    max_increase = (
        max_price_increase_pct
        if max_price_increase_pct is not None
        else confirm_cfg.get("max_price_increase_pct", _DEFAULT_MAX_PRICE_INCREASE_PCT)
    )

    if availability not in ("IN_STOCK", "STORE_ONLY"):
        _expire_active_deal(cur, deal_id=open_deal.id, expire_reason="sold_out")
        return DealRecheckResult(action="expired", expire_reason="sold_out")

    if open_deal.lane == "USED":
        ctx = get_offer_context(cur, offer_id)
        reference_cents = resolve_current_new_for_variant(
            cur, variant_id=ctx["variant_id"] if ctx else None, exclude_offer_id=offer_id
        )
    else:
        verified = resolve_verified_reference_for_offer(cur, offer_id)
        reference_cents = verified.cents if verified else None

    if reference_cents is None or reference_cents <= 0:
        _expire_active_deal(cur, deal_id=open_deal.id, expire_reason="reference_lost")
        return DealRecheckResult(action="expired", expire_reason="reference_lost")

    discount_pct = round((1 - (compare_price_cents / reference_cents)) * 100, 2)

    # "Price rises above threshold": allow the same tolerance C6 grants at
    # confirmation time before treating a rise as a real recovery, so a
    # one-cent jitter around the ACTIVE price doesn't flap the deal.
    max_allowed_price = open_deal.price_cents * (1 + max_increase / 100.0)
    if compare_price_cents > max_allowed_price or discount_pct < min_discount:
        _expire_active_deal(cur, deal_id=open_deal.id, expire_reason="price_recovered")
        return DealRecheckResult(action="expired", expire_reason="price_recovered")

    cur.execute(
        """
        UPDATE deals
        SET price_cents = %s, landed_price_cents = %s, discount_pct = %s,
            reference_cents = %s, last_confirmed_observation_id = %s
        WHERE id = %s
        """,
        (compare_price_cents, landed_price_cents, discount_pct, reference_cents, observation_id, open_deal.id),
    )
    return DealRecheckResult(action="refreshed")


def _expire_active_deal(cur, *, deal_id: int, expire_reason: str) -> None:
    cur.execute(
        "UPDATE deals SET status = 'EXPIRED', expired_at = now(), expire_reason = %s WHERE id = %s",
        (expire_reason, deal_id),
    )


def expire_stale_deals(conn, now: datetime, *, stale_hours: int | None = None) -> int:
    """H2 periodic sweep: expires every ACTIVE deal whose offer has had no
    fresh OK observation within `stale_hours` (config/deal_rules.yaml
    `deal_rules.expiry.stale_hours`, default 24) -- the `not_observed`
    trigger from architecture 6.3, which by definition cannot be caught by
    `refresh_or_expire_active_deal` (there is no new observation to react
    to). Intended to be called once per tick by the scheduler
    (`fpt.store.deals.expire_stale_deals(conn, now)`, per this task's
    explicit contract) -- takes a connection (not a cursor) since it issues
    its own statement and returns a plain row count for logging; it does
    not commit, matching every other function in this module (the caller's
    transaction boundary decides that).

    Every ACTIVE deal already has `confirming_observation_id` set (the
    `deals_confirmed_status_requires_observation` CHECK, migration 014,
    covers EXPIRED too), so this UPDATE never needs to touch that column."""
    hours = stale_hours if stale_hours is not None else _load_stale_hours()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE deals d
        SET status = 'EXPIRED', expired_at = %(now)s, expire_reason = 'not_observed'
        WHERE d.status = 'ACTIVE'
          AND NOT EXISTS (
            SELECT 1 FROM price_observations po
            WHERE po.offer_id = d.offer_id AND po.quality = 'OK'
              AND po.observed_at >= %(now)s - (%(hours)s::text || ' hours')::interval
          )
        """,
        {"now": now, "hours": hours},
    )
    return cur.rowcount
