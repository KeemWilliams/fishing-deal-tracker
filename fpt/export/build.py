"""Pure transforms: DB rows (from `fpt.export.queries`) -> the JSON dicts
the site expects, per `site/src/lib/types.ts` and architecture doc 3.4.

Nothing here touches the database or a storage backend -- that separation
is what makes this module unit-testable without Postgres (see
`tests/test_export_build.py`) and lets `tests/test_export_integration.py`
exercise the DB-facing half (`fpt.export.queries`) independently.

Contract gaps (DB schema allows more values than the site's TypeScript
types): documented inline at each mapping table below and flagged again in
this task's HANDOFF as uncertainties for the architect/database engineer.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

MIN_DISCOUNT_PCT = 50
HISTORY_WINDOW_DAYS = 90
STALE_AFTER_HOURS = 12  # architecture doc 3.4 integrity gate: "all retailers stale (no successful fetch in 12h)"
DEGRADED_BLOCK_RATE = 0.20  # matches the breaker's block_rate_threshold convention (config/retailers.yaml)

# --- Enum gaps between the DB schema and site/src/lib/types.ts ---------
#
# seller_type: DB has FIRST_PARTY | RETAILER_RESALE | MARKETPLACE_3P;
# the site's SellerType is only FIRST_PARTY | MARKETPLACE. A retailer's own
# resale/clearance channel reads to a shopper as "sold by the retailer", so
# it maps to FIRST_PARTY; true third-party marketplace sellers map to
# MARKETPLACE. HIGH uncertainty -- flagged in HANDOFF.
_SELLER_TYPE_MAP = {
    "FIRST_PARTY": "FIRST_PARTY",
    "RETAILER_RESALE": "FIRST_PARTY",
    "MARKETPLACE_3P": "MARKETPLACE",
}

# availability: DB has IN_STOCK | OUT_OF_STOCK | STORE_ONLY | BACKORDER | UNKNOWN;
# site's Availability is only IN_STOCK | STORE_ONLY | OUT_OF_STOCK. BACKORDER
# and UNKNOWN both collapse to OUT_OF_STOCK -- the conservative choice: never
# imply a shopper can buy it now when we aren't sure. MEDIUM uncertainty.
_AVAILABILITY_MAP = {
    "IN_STOCK": "IN_STOCK",
    "STORE_ONLY": "STORE_ONLY",
    "OUT_OF_STOCK": "OUT_OF_STOCK",
    "BACKORDER": "OUT_OF_STOCK",
    "UNKNOWN": "OUT_OF_STOCK",
}

# claimed_reference.kind: DB has WAS | MSRP | LIST | COMPARE_AT; site's
# ClaimedReference.kind is only MSRP | WAS | LIST. COMPARE_AT (a retailer's
# "compare at" price, same concept as a claimed list price) maps to LIST.
# HIGH uncertainty -- flagged in HANDOFF.
_CLAIMED_KIND_MAP = {
    "WAS": "WAS",
    "MSRP": "MSRP",
    "LIST": "LIST",
    "COMPARE_AT": "LIST",
}

# deals.reference_kind: DB has OWN_HISTORY_MEDIAN_90D | CROSS_RETAILER_NEW |
# DATA_API_HISTORY | CURRENT_NEW | RETAILER_CLAIMED; site's ReferenceDetail.kind
# is only the first three. CURRENT_NEW (the USED lane's U1 rule) has no
# matching site enum value -- it maps to CROSS_RETAILER_NEW with a USED-lane
# label, since both mean "lowest price at another currently-tracked offer".
# RETAILER_CLAIMED means there is no verified reference at all -- `reference`
# is omitted (null) and only `claimed_reference` is populated. HIGH
# uncertainty -- flagged in HANDOFF.
_REFERENCE_KIND_MAP = {
    "OWN_HISTORY_MEDIAN_90D": "OWN_HISTORY_MEDIAN_90D",
    "CROSS_RETAILER_NEW": "CROSS_RETAILER_NEW",
    "DATA_API_HISTORY": "DATA_API_HISTORY",
    "CURRENT_NEW": "CROSS_RETAILER_NEW",
}


def _iso_z(dt: datetime | None) -> str | None:
    """Format a timestamptz as the doc's example format: no microseconds,
    trailing Z. `None` in, `None` out -- callers decide whether that's
    acceptable for a given field."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.isoformat().replace("+00:00", "Z")


def _reference_label(mapped_kind: str, raw_kind: str, retailer_name: str, detail: dict) -> str:
    if raw_kind == "OWN_HISTORY_MEDIAN_90D":
        days = detail.get("days") or detail.get("observed_days")
        if days:
            return f"{retailer_name} regular price, median of {days} days tracked"
        return f"{retailer_name} regular price (90-day median)"
    if raw_kind == "CROSS_RETAILER_NEW":
        return "Lowest verified new price at another tracked retailer"
    if raw_kind == "CURRENT_NEW":
        return "Lowest current in-stock new price across tracked retailers"
    if raw_kind == "DATA_API_HISTORY":
        return "Third-party price history (data API)"
    return "Reference price"


def _reference_observed_days(raw_kind: str, detail: dict) -> int | None:
    if raw_kind != "OWN_HISTORY_MEDIAN_90D":
        return None
    days = detail.get("days") or detail.get("observed_days")
    return int(days) if days is not None else None


def build_deal(row: dict) -> dict:
    """One `deals` row (see `queries.fetch_active_deal_rows`) -> one entry
    in the published `deals.json`. Enforces the publish rules from the
    architecture doc / coding task at the row level:
    - condition is always present (guaranteed by the DB's NOT NULL, asserted
      here defensively since a silent None would ship a broken deal card).
    - a retailer-claimed price is never merged into the verified reference:
      `reference` and `claimed_reference` are always built independently.
    """
    condition = row["condition"]
    if not condition:
        raise ValueError(f"deal {row['deal_id']} has no condition -- refusing to publish")

    raw_reference_kind = row["reference_kind"]
    reference = None
    if raw_reference_kind != "RETAILER_CLAIMED":
        detail = row.get("reference_detail") or {}
        mapped_kind = _REFERENCE_KIND_MAP.get(raw_reference_kind)
        if mapped_kind is None:
            raise ValueError(
                f"deal {row['deal_id']}: unknown reference_kind {raw_reference_kind!r}"
            )
        reference = {
            "kind": mapped_kind,
            "cents": int(row["reference_cents"]),
            "label": _reference_label(mapped_kind, raw_reference_kind, row["retailer_name"], detail),
        }
        observed_days = _reference_observed_days(raw_reference_kind, detail)
        if observed_days is not None:
            reference["observed_days"] = observed_days

    claimed_reference = None
    if row.get("claimed_reference_cents") is not None:
        claimed_kind = _CLAIMED_KIND_MAP.get(row["claimed_reference_kind"], "LIST")
        claimed_reference = {
            "kind": claimed_kind,
            "cents": int(row["claimed_reference_cents"]),
            "inflated_vs_reference": bool(row.get("claimed_inflated")),
        }

    first_confirmed_at = row.get("confirmed_at") or row["detected_at"]
    last_confirmed_at = row.get("last_observed_at") or first_confirmed_at

    return {
        "deal_id": row["deal_id"],
        "lane": row["lane"],
        "rule": row["rule"],
        "product_slug": row["product_slug"],
        "product_name": row["product_name"],
        "brand": row["brand"],
        "category": row["category"],
        "variant_pub_id": row["variant_pub_id"],
        "variant_label": row["variant_label"],
        "retailer_slug": row["retailer_slug"],
        "retailer_name": row["retailer_name"],
        "retailer_url": row["retailer_url"],
        "condition": condition,
        "seller_name": row["seller_name"],
        "seller_type": _SELLER_TYPE_MAP.get(row["seller_type"], "MARKETPLACE"),
        "price_cents": int(row["price_cents"]),
        "shipping_cents": int(row["shipping_cents"]) if row.get("shipping_cents") is not None else None,
        "landed_price_cents": int(row["landed_price_cents"]) if row.get("landed_price_cents") is not None else None,
        "reference": reference,
        "claimed_reference": claimed_reference,
        "discount_pct": round(float(row["discount_pct"]), 2),
        "availability": _AVAILABILITY_MAP.get(row.get("availability"), "OUT_OF_STOCK"),
        "stock_qty": int(row["stock_qty"]) if row.get("stock_qty") is not None else None,
        "first_confirmed_at": _iso_z(first_confirmed_at),
        "last_confirmed_at": _iso_z(last_confirmed_at),
    }


def build_deals_feed(deal_rows: list[dict], *, generated_at: datetime) -> dict:
    return {
        "generated_at": _iso_z(generated_at),
        "deals": [build_deal(row) for row in deal_rows],
    }


def _as_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _retailer_health(row: dict, *, now: datetime) -> tuple[str, bool]:
    from datetime import timedelta

    now = _as_utc(now)
    last_ok = row.get("last_successful_fetch_at")
    stale = last_ok is None or (now - _as_utc(last_ok)) > timedelta(hours=STALE_AFTER_HOURS)

    breaker_open_until = row.get("breaker_open_until")
    breaker_open = breaker_open_until is not None and _as_utc(breaker_open_until) > now

    fetched_24h = row.get("fetched_24h") or 0
    blocked_24h = row.get("blocked_24h") or 0
    block_rate = (blocked_24h / fetched_24h) if fetched_24h else 0.0

    if breaker_open:
        return "FAILED", True
    if stale:
        return "DEGRADED", True
    if block_rate > DEGRADED_BLOCK_RATE:
        return "DEGRADED", False
    return "HEALTHY", False


def build_retailer_meta(row: dict, *, now: datetime) -> dict:
    health, stale = _retailer_health(row, now=now)
    return {
        "slug": row["slug"],
        "name": row["name"],
        "health": health,
        "last_successful_fetch_at": _iso_z(row.get("last_successful_fetch_at")) or _iso_z(now),
        "last_discovery_sweep_at": _iso_z(row.get("last_discovery_sweep_at")) or _iso_z(now),
        "stale": stale,
    }


@dataclass
class MetaCounts:
    products: int
    offers_tracked: int
    verified_deals: int
    claimed_deals: int
    used_deals: int


def counts_from_deals(deal_rows: list[dict], *, product_count: int, offers_tracked: int) -> MetaCounts:
    verified = sum(1 for r in deal_rows if r["lane"] == "VERIFIED")
    claimed = sum(1 for r in deal_rows if r["lane"] == "CLAIMED")
    used = sum(1 for r in deal_rows if r["lane"] == "USED")
    return MetaCounts(
        products=product_count,
        offers_tracked=offers_tracked,
        verified_deals=verified,
        claimed_deals=claimed,
        used_deals=used,
    )


def build_export_id(*, generated_at: datetime) -> str:
    return f"{_iso_z(generated_at)}-{secrets.token_hex(2)}"


def build_meta(
    *,
    retailer_rows: list[dict],
    counts: MetaCounts,
    generated_at: datetime,
    export_id: str,
) -> dict:
    return {
        "schema_version": 2,
        "export_id": export_id,
        "generated_at": _iso_z(generated_at),
        "retailers": [build_retailer_meta(row, now=generated_at) for row in retailer_rows],
        "counts": {
            "products": counts.products,
            "offers_tracked": counts.offers_tracked,
            "verified_deals": counts.verified_deals,
            "claimed_deals": counts.claimed_deals,
            "used_deals": counts.used_deals,
        },
        "thresholds": {"min_discount_pct": MIN_DISCOUNT_PCT},
    }


def build_variant_offer(row: dict) -> dict:
    return {
        "retailer_slug": row["retailer_slug"],
        "retailer_name": row["retailer_name"],
        "url": row["url"],
        "condition": row["condition"],
        "seller_name": row["seller_name"],
        "seller_type": _SELLER_TYPE_MAP.get(row["seller_type"], "MARKETPLACE"),
        "price_cents": int(row["price_cents"]) if row.get("price_cents") is not None else 0,
        "shipping_cents": int(row["shipping_cents"]) if row.get("shipping_cents") is not None else None,
        "availability": _AVAILABILITY_MAP.get(row.get("availability"), "OUT_OF_STOCK"),
        "on_clearance": bool(row.get("on_clearance")),
        "observed_at": _iso_z(row.get("observed_at")) or _iso_z(datetime.now(timezone.utc)),
    }


def build_history_point(row: dict) -> dict:
    day = row["day"]
    day_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return {
        "date": day_str,
        "retailer_slug": row["retailer_slug"],
        "condition_group": row["condition_group"],
        "price_cents": int(row["price_cents"]),
    }


@dataclass
class ProductBuildInputs:
    product_slug: str
    product_name: str
    brand: str
    category: str
    variants: list[dict] = field(default_factory=list)  # rows from fetch_products_with_variants for this product
    offers_by_variant: dict[int, list[dict]] = field(default_factory=dict)
    history_by_variant: dict[int, list[dict]] = field(default_factory=dict)


def build_product(inputs: ProductBuildInputs) -> dict:
    variants = []
    for v in inputs.variants:
        variant_id = v["variant_id"]
        variants.append(
            {
                "variant_pub_id": v["variant_pub_id"],
                "label": v["variant_label"],
                "attributes": v.get("attributes") or {},
                "offers": [build_variant_offer(r) for r in inputs.offers_by_variant.get(variant_id, [])],
                "history": [build_history_point(r) for r in inputs.history_by_variant.get(variant_id, [])],
            }
        )
    return {
        "slug": inputs.product_slug,
        "name": inputs.product_name,
        "brand": inputs.brand,
        "category": inputs.category,
        "variants": variants,
    }


def group_variant_rows_by_product(variant_rows: list[dict]) -> dict[str, ProductBuildInputs]:
    """Groups `fetch_products_with_variants` rows by product slug. Callers
    then attach `offers_by_variant` / `history_by_variant` before calling
    `build_product`."""
    by_slug: dict[str, ProductBuildInputs] = {}
    for row in variant_rows:
        slug = row["product_slug"]
        if slug not in by_slug:
            by_slug[slug] = ProductBuildInputs(
                product_slug=slug,
                product_name=row["product_name"],
                brand=row["brand"],
                category=row["category"],
            )
        by_slug[slug].variants.append(row)
    return by_slug


def group_rows_by_variant(rows: list[dict]) -> dict[int, list[dict]]:
    by_variant: dict[int, list[dict]] = {}
    for row in rows:
        by_variant.setdefault(row["variant_id"], []).append(row)
    return by_variant


def product_slugs_from_deals(deal_rows: list[dict]) -> list[str]:
    """Products get a products/<slug>.json page only if they currently have
    at least one ACTIVE deal -- matches the site's only off-ramp to a
    product page (a deal card's "view details" link) and keeps
    meta.counts.products meaningful (see sample-data: 6 products across 7
    deals, one product with both a VERIFIED and a USED entry). Documented
    as an implementation decision -- see HANDOFF."""
    seen: dict[str, None] = {}
    for row in deal_rows:
        seen.setdefault(row["product_slug"], None)
    return list(seen.keys())
