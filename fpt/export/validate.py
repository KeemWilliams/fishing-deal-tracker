"""Structural validation of the built export against the site's contract
(`site/src/lib/types.ts`, schema_version 2). There is no committed
`contracts/export.schema.json` yet (the architecture doc names that path,
but the DB engineer/architect haven't produced it) -- this module is the
stand-in: a JSON Schema hand-derived from `site/src/lib/types.ts`, kept in
sync manually until that file exists. If the two ever diverge, the site's
TypeScript is the source of truth (see `site/README.md`'s own framing --
"a schema bump on the scraper side should fail this site's build loudly").

Used both as an export-time integrity gate (`fpt.export.runner`) and by
`tests/test_export_build.py` / `tests/test_export_integration.py`.
"""

from __future__ import annotations

from typing import Any

import jsonschema

LANE_ENUM = ["VERIFIED", "CLAIMED", "USED"]
DEAL_RULE_ENUM = ["DEEP_DISCOUNT_NEW", "DEEP_DISCOUNT_CLAIMED", "USED_VS_CURRENT_NEW"]
CONDITION_ENUM = [
    "NEW",
    "NEW_OPEN_BOX",
    "REFURBISHED",
    "USED_LIKE_NEW",
    "USED_VERY_GOOD",
    "USED_GOOD",
    "USED_ACCEPTABLE",
    "USED_UNGRADED",
]
CONDITION_GROUP_ENUM = ["NEW", "OPEN_BOX", "REFURB", "USED"]
CATEGORY_ENUM = ["rod", "reel", "combo", "soft_bait", "hard_bait", "line", "terminal", "other"]
AVAILABILITY_ENUM = ["IN_STOCK", "STORE_ONLY", "OUT_OF_STOCK"]
RETAILER_HEALTH_ENUM = ["HEALTHY", "DEGRADED", "FAILED"]
SELLER_TYPE_ENUM = ["FIRST_PARTY", "MARKETPLACE"]
REFERENCE_KIND_ENUM = ["OWN_HISTORY_MEDIAN_90D", "CROSS_RETAILER_NEW", "DATA_API_HISTORY"]
CLAIMED_KIND_ENUM = ["MSRP", "WAS", "LIST"]

_REFERENCE_DETAIL_SCHEMA = {
    "type": "object",
    "required": ["kind", "cents", "label"],
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": REFERENCE_KIND_ENUM},
        "cents": {"type": "integer", "exclusiveMinimum": 0},
        "label": {"type": "string", "minLength": 1},
        "observed_days": {"type": "integer"},
    },
}

_CLAIMED_REFERENCE_SCHEMA = {
    "type": "object",
    "required": ["kind", "cents", "inflated_vs_reference"],
    "additionalProperties": False,
    "properties": {
        "kind": {"enum": CLAIMED_KIND_ENUM},
        "cents": {"type": "integer", "exclusiveMinimum": 0},
        "inflated_vs_reference": {"type": "boolean"},
    },
}

DEAL_SCHEMA = {
    "type": "object",
    "required": [
        "deal_id", "lane", "rule", "product_slug", "product_name", "brand", "category",
        "variant_pub_id", "variant_label", "retailer_slug", "retailer_url", "condition",
        "seller_name", "seller_type", "price_cents", "shipping_cents", "landed_price_cents",
        "reference", "claimed_reference", "discount_pct", "availability", "stock_qty",
        "first_confirmed_at", "last_confirmed_at",
    ],
    "properties": {
        "deal_id": {"type": "string", "minLength": 1},
        "lane": {"enum": LANE_ENUM},
        "rule": {"enum": DEAL_RULE_ENUM},
        "product_slug": {"type": "string", "minLength": 1},
        "product_name": {"type": "string", "minLength": 1},
        "brand": {"type": "string", "minLength": 1},
        "category": {"enum": CATEGORY_ENUM},
        "variant_pub_id": {"type": "string", "minLength": 1},
        "variant_label": {"type": "string", "minLength": 1},
        "retailer_slug": {"type": "string", "minLength": 1},
        "retailer_name": {"type": "string"},
        "retailer_url": {"type": "string", "minLength": 1},
        "condition": {"enum": CONDITION_ENUM},
        "seller_name": {"type": "string", "minLength": 1},
        "seller_type": {"enum": SELLER_TYPE_ENUM},
        "price_cents": {"type": "integer", "exclusiveMinimum": 0},
        "shipping_cents": {"type": ["integer", "null"]},
        "landed_price_cents": {"type": ["integer", "null"]},
        "reference": {"anyOf": [_REFERENCE_DETAIL_SCHEMA, {"type": "null"}]},
        "claimed_reference": {"anyOf": [_CLAIMED_REFERENCE_SCHEMA, {"type": "null"}]},
        "discount_pct": {"type": "number", "minimum": 0, "maximum": 100},
        "availability": {"enum": AVAILABILITY_ENUM},
        "stock_qty": {"type": ["integer", "null"]},
        "first_confirmed_at": {"type": "string", "minLength": 1},
        "last_confirmed_at": {"type": "string", "minLength": 1},
    },
}

DEALS_FEED_SCHEMA = {
    "type": "object",
    "required": ["generated_at", "deals"],
    "properties": {
        "generated_at": {"type": "string", "minLength": 1},
        "deals": {"type": "array", "items": DEAL_SCHEMA},
    },
}

RETAILER_META_SCHEMA = {
    "type": "object",
    "required": ["slug", "name", "health", "last_successful_fetch_at", "last_discovery_sweep_at", "stale"],
    "properties": {
        "slug": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "health": {"enum": RETAILER_HEALTH_ENUM},
        "last_successful_fetch_at": {"type": "string", "minLength": 1},
        "last_discovery_sweep_at": {"type": "string", "minLength": 1},
        "stale": {"type": "boolean"},
    },
}

META_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "export_id", "generated_at", "retailers", "counts", "thresholds"],
    "properties": {
        "schema_version": {"const": 2},
        "export_id": {"type": "string", "minLength": 1},
        "generated_at": {"type": "string", "minLength": 1},
        "retailers": {"type": "array", "items": RETAILER_META_SCHEMA},
        "counts": {
            "type": "object",
            "required": ["products", "offers_tracked", "verified_deals", "claimed_deals", "used_deals"],
            "properties": {
                "products": {"type": "integer", "minimum": 0},
                "offers_tracked": {"type": "integer", "minimum": 0},
                "verified_deals": {"type": "integer", "minimum": 0},
                "claimed_deals": {"type": "integer", "minimum": 0},
                "used_deals": {"type": "integer", "minimum": 0},
            },
        },
        "thresholds": {
            "type": "object",
            "required": ["min_discount_pct"],
            "properties": {"min_discount_pct": {"type": "number"}},
        },
        "is_sample_data": {"type": "boolean"},
    },
}

_PRODUCT_OFFER_SCHEMA = {
    "type": "object",
    "required": [
        "retailer_slug", "url", "condition", "seller_name", "seller_type",
        "price_cents", "shipping_cents", "availability", "on_clearance", "observed_at",
    ],
    "properties": {
        "retailer_slug": {"type": "string", "minLength": 1},
        "retailer_name": {"type": "string"},
        "url": {"type": "string", "minLength": 1},
        "condition": {"enum": CONDITION_ENUM},
        "seller_name": {"type": "string", "minLength": 1},
        "seller_type": {"enum": SELLER_TYPE_ENUM},
        "price_cents": {"type": "integer", "minimum": 0},
        "shipping_cents": {"type": ["integer", "null"]},
        "availability": {"enum": AVAILABILITY_ENUM},
        "on_clearance": {"type": "boolean"},
        "observed_at": {"type": "string", "minLength": 1},
    },
}

_PRODUCT_HISTORY_POINT_SCHEMA = {
    "type": "object",
    "required": ["date", "retailer_slug", "condition_group", "price_cents"],
    "properties": {
        "date": {"type": "string", "minLength": 1},
        "retailer_slug": {"type": "string", "minLength": 1},
        "condition_group": {"enum": CONDITION_GROUP_ENUM},
        "price_cents": {"type": "integer", "exclusiveMinimum": 0},
    },
}

_PRODUCT_VARIANT_SCHEMA = {
    "type": "object",
    "required": ["variant_pub_id", "label", "attributes", "offers", "history"],
    "properties": {
        "variant_pub_id": {"type": "string", "minLength": 1},
        "label": {"type": "string", "minLength": 1},
        "attributes": {"type": "object"},
        "offers": {"type": "array", "items": _PRODUCT_OFFER_SCHEMA},
        "history": {"type": "array", "items": _PRODUCT_HISTORY_POINT_SCHEMA},
    },
}

PRODUCT_SCHEMA = {
    "type": "object",
    "required": ["slug", "name", "brand", "category", "variants"],
    "properties": {
        "slug": {"type": "string", "minLength": 1},
        "name": {"type": "string", "minLength": 1},
        "brand": {"type": "string", "minLength": 1},
        "category": {"enum": CATEGORY_ENUM},
        "variants": {"type": "array", "items": _PRODUCT_VARIANT_SCHEMA},
    },
}


class ExportValidationError(ValueError):
    def __init__(self, what: str, errors: list[str]):
        self.what = what
        self.errors = errors
        super().__init__(f"{what} failed schema validation:\n" + "\n".join(f"  - {e}" for e in errors))


def _validate(document: Any, schema: dict, what: str) -> None:
    validator = jsonschema.Draft202012Validator(schema)
    errors = [f"{'.'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in validator.iter_errors(document)]
    if errors:
        raise ExportValidationError(what, errors)


def validate_meta(meta: dict) -> None:
    _validate(meta, META_SCHEMA, "meta.json")


def validate_deals_feed(feed: dict) -> None:
    _validate(feed, DEALS_FEED_SCHEMA, "deals.json")
    # Publish-rule cross-check that a plain schema can't express: a
    # RETAILER_CLAIMED reference must never leak into `reference` -- CLAIMED
    # lane deals must have reference == null (was-price kept separate).
    for deal in feed["deals"]:
        if deal["lane"] == "CLAIMED" and deal["reference"] is not None:
            raise ExportValidationError(
                "deals.json",
                [f"deal {deal['deal_id']}: CLAIMED lane must not carry a verified reference"],
            )
        if deal["lane"] in ("VERIFIED", "USED") and deal["reference"] is None:
            raise ExportValidationError(
                "deals.json",
                [f"deal {deal['deal_id']}: {deal['lane']} lane requires a verified reference"],
            )


def validate_product(product: dict) -> None:
    _validate(product, PRODUCT_SCHEMA, f"products/{product.get('slug', '?')}.json")
