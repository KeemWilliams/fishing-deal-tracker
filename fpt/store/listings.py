"""Listing/offer persistence (db/migrations 005_listings_offers).

`listings` = one retailer SKU = one variant at that retailer.
`offers` = listing x condition x seller -- the core unit of price per the
architecture's executive summary.
"""

from __future__ import annotations

import json
from typing import Mapping, Sequence

from fpt.core.models import CONDITION_GROUP, Condition, SellerType


def get_or_create_seller(
    cur, *, retailer_id: int, seller_key: str, seller_type: SellerType | str, name: str | None = None
) -> int:
    seller_type_value = seller_type.value if isinstance(seller_type, SellerType) else seller_type
    cur.execute(
        """
        INSERT INTO sellers (retailer_id, seller_key, name, seller_type)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (retailer_id, seller_key) DO UPDATE SET seller_type = EXCLUDED.seller_type
        RETURNING id
        """,
        (retailer_id, seller_key, name, seller_type_value),
    )
    return cur.fetchone()[0]


def upsert_listing(
    cur,
    *,
    retailer_id: int,
    retailer_sku: str,
    retailer_product_code: str | None,
    url: str,
    title_raw: str,
    brand_raw: str | None,
    model_raw: str | None,
    variant_label_raw: str,
    attributes_raw: Mapping[str, str],
    attributes_norm: dict,
    variant_key_observed: str | None,
    gtin14: Sequence[str],
    mpn_raw: str | None,
    unit_count: int | None,
    variant_id: int | None,
    match_status: str,
) -> int:
    """Idempotent upsert keyed on (retailer_id, retailer_sku) -- the same
    fetch re-parsed twice, or a later fetch of the same product page,
    updates the existing row and bumps last_seen_at rather than creating a
    duplicate listing."""
    cur.execute(
        """
        INSERT INTO listings (
            retailer_id, retailer_sku, retailer_product_code, url, title_raw,
            brand_raw, model_raw, variant_label_raw, attributes_raw, attributes_norm,
            variant_key_observed, gtin14, mpn_raw, unit_count, variant_id, match_status,
            last_seen_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (retailer_id, retailer_sku) DO UPDATE SET
            title_raw = EXCLUDED.title_raw,
            variant_label_raw = EXCLUDED.variant_label_raw,
            attributes_raw = EXCLUDED.attributes_raw,
            attributes_norm = EXCLUDED.attributes_norm,
            variant_key_observed = EXCLUDED.variant_key_observed,
            gtin14 = EXCLUDED.gtin14,
            unit_count = EXCLUDED.unit_count,
            variant_id = COALESCE(listings.variant_id, EXCLUDED.variant_id),
            match_status = CASE WHEN listings.variant_id IS NULL THEN EXCLUDED.match_status ELSE listings.match_status END,
            is_active = true,
            consecutive_absent = 0,
            last_seen_at = now()
        RETURNING id
        """,
        (
            retailer_id,
            retailer_sku,
            retailer_product_code,
            url,
            title_raw,
            brand_raw,
            model_raw,
            variant_label_raw,
            json.dumps(dict(attributes_raw)),
            json.dumps(attributes_norm),
            variant_key_observed,
            list(gtin14),
            mpn_raw,
            unit_count,
            variant_id,
            match_status,
        ),
    )
    return cur.fetchone()[0]


def upsert_offer(
    cur,
    *,
    listing_id: int,
    seller_id: int,
    offer_key: str,
    condition: Condition | str,
    condition_raw: str | None,
) -> int:
    condition_value = condition.value if isinstance(condition, Condition) else condition
    condition_group = CONDITION_GROUP[condition_value]
    cur.execute(
        """
        INSERT INTO offers (listing_id, seller_id, offer_key, condition, condition_group, condition_raw, last_seen_at)
        VALUES (%s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (listing_id, offer_key) DO UPDATE SET
            condition = EXCLUDED.condition,
            condition_group = EXCLUDED.condition_group,
            condition_raw = EXCLUDED.condition_raw,
            is_active = true,
            vanished_at = NULL,
            last_seen_at = now()
        RETURNING id
        """,
        (listing_id, seller_id, offer_key, condition_value, condition_group, condition_raw),
    )
    return cur.fetchone()[0]
