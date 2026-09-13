"""Catalog persistence: brands, products, product_variants (db/migrations
002_reference_tables, 003_catalog).

Matching here is deliberately the MVP subset of architecture doc section 8:
only step 1 (GTIN equality -> `auto_gtin`) and step 5 (`auto_created`,
no match) are implemented. Steps 2-4 (MPN, brand+category+model_key exact,
fuzzy trigram) are the full product-matching engine and are explicitly out
of this task's scope -- see HANDOFF. Every listing still gets a
product_variant row either way ("the offer is still tracked", architecture
4.2), so nothing here can block ingestion for lack of a full matcher.
"""

from __future__ import annotations

import json
import re
import secrets
from typing import Sequence

import psycopg

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    return slug or "item"


def get_or_create_brand(cur, name: str | None) -> int:
    """Brand is required (products.brand_id NOT NULL); an unknown/missing
    brand_raw becomes the literal 'Unknown' brand rather than failing the
    whole listing -- a parser gap in one field should never block tracking
    the rest of the offer."""
    canonical = (name or "Unknown").strip() or "Unknown"
    key = slugify(canonical).replace("-", "_")
    cur.execute(
        """
        INSERT INTO brands (name, name_key) VALUES (%s, %s)
        ON CONFLICT (name_key) DO UPDATE SET name = brands.name
        RETURNING id
        """,
        (canonical, key),
    )
    return cur.fetchone()[0]


def find_variant_by_gtin(cur, gtin14: Sequence[str]) -> int | None:
    """Architecture 8, step 1: any listing barcode equal to a variant
    barcode is `auto_gtin` regardless of retailer -- this is what makes
    CROSS_RETAILER_NEW (R2) reference resolution work "from MVP day one"
    per the architecture doc, without waiting for the full matcher."""
    if not gtin14:
        return None
    cur.execute(
        "SELECT id FROM product_variants WHERE gtin14 && %s::char(14)[] AND merged_into_id IS NULL LIMIT 1",
        (list(gtin14),),
    )
    row = cur.fetchone()
    return row[0] if row else None


def merge_gtins_into_variant(cur, variant_id: int, gtin14: Sequence[str]) -> None:
    if not gtin14:
        return
    cur.execute(
        """
        UPDATE product_variants
        SET gtin14 = (
            SELECT array_agg(DISTINCT g) FROM unnest(gtin14 || %s::char(14)[]) AS g
        )
        WHERE id = %s
        """,
        (list(gtin14), variant_id),
    )


def get_or_create_product(
    cur,
    *,
    brand_id: int,
    category: str,
    model_key: str,
    name: str,
) -> int:
    """Security/robustness review M4: `products.slug` is UNIQUE globally,
    independently of the `(brand_id, category, model_key)` unique
    constraint this INSERT's `ON CONFLICT` already targets -- two DIFFERENT
    products (different brand or category) can perfectly legitimately
    slugify to the same string (e.g. two brands both named "Pro Series" in
    different categories), which would raise a bare `UniqueViolation` on
    `products.slug` that the `ON CONFLICT` clause above does not catch
    (it only catches the OTHER unique key). On that specific conflict,
    retry with a short random suffix appended to the slug -- the slug is a
    display/URL convenience, not an identity column, so a few extra
    characters cost nothing.
    """
    slug = slugify(f"{name}-{model_key}")
    for attempt in range(5):
        candidate_slug = slug if attempt == 0 else f"{slug}-{secrets.token_hex(3)}"
        cur.execute("SAVEPOINT before_product_insert")
        try:
            cur.execute(
                """
                INSERT INTO products (slug, brand_id, name, model_key, category, origin)
                VALUES (%s, %s, %s, %s, %s, 'auto_created')
                ON CONFLICT (brand_id, category, model_key) DO UPDATE SET name = products.name
                RETURNING id
                """,
                (candidate_slug, brand_id, name, model_key, category),
            )
            product_id = cur.fetchone()[0]
            cur.execute("RELEASE SAVEPOINT before_product_insert")
            return product_id
        except psycopg.errors.UniqueViolation:
            cur.execute("ROLLBACK TO SAVEPOINT before_product_insert")
            continue
    raise RuntimeError(f"could not allocate a unique products.slug for {slug!r} after 5 attempts")


def get_or_create_variant(
    cur,
    *,
    product_id: int,
    variant_key: str,
    label: str,
    attributes: dict,
    unit_count: int | None,
    gtin14: Sequence[str],
) -> int:
    cur.execute(
        """
        INSERT INTO product_variants (product_id, gtin14, attributes, variant_key, label, unit_count)
        VALUES (%s, %s, %s::jsonb, %s, %s, %s)
        ON CONFLICT (product_id, variant_key) DO UPDATE SET label = product_variants.label
        RETURNING id
        """,
        (product_id, list(gtin14), json.dumps(attributes), variant_key, label, unit_count),
    )
    variant_id = cur.fetchone()[0]
    if gtin14:
        merge_gtins_into_variant(cur, variant_id, gtin14)
    return variant_id


def resolve_or_create_variant(
    cur,
    *,
    brand_raw: str | None,
    category: str,
    model_key: str,
    variant_key: str,
    label: str,
    attributes: dict,
    unit_count: int | None,
    gtin14: Sequence[str],
) -> tuple[int, int, str]:
    """Returns (product_id, variant_id, match_status).

    `match_status` is 'auto_gtin' when an existing variant (from any
    retailer/brand-guess) already carries one of these barcodes, else
    'auto_created'. Architecture 8's `gtin_attr_conflict` review path
    (contradicting required attributes on a GTIN match) is not evaluated
    here -- flagged as a HANDOFF uncertainty, since building the full
    conflict detector is matcher-engine scope.
    """
    existing_variant_id = find_variant_by_gtin(cur, gtin14)
    if existing_variant_id is not None:
        merge_gtins_into_variant(cur, existing_variant_id, gtin14)
        cur.execute("SELECT product_id FROM product_variants WHERE id = %s", (existing_variant_id,))
        product_id = cur.fetchone()[0]
        return product_id, existing_variant_id, "auto_gtin"

    brand_id = get_or_create_brand(cur, brand_raw)
    product_id = get_or_create_product(cur, brand_id=brand_id, category=category, model_key=model_key, name=label)
    variant_id = get_or_create_variant(
        cur,
        product_id=product_id,
        variant_key=variant_key,
        label=label,
        attributes=attributes,
        unit_count=unit_count,
        gtin14=gtin14,
    )
    return product_id, variant_id, "auto_created"
