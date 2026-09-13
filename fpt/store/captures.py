"""page_captures persistence (db/migrations/020_page_captures).

Deliberately tiny and separate from every other store module: a capture
is a CLAIM (see the migration's docstring and
knowledge/architecture/fishing-deal-quality-ai-scoring.md's non-fetched-
data-is-a-claim rule), never a `price_observations` row, so this module
must never be reached from `fpt/store/observations.py` or
`fpt/store/pipeline.py`, and it must never write to `offers` or
`price_observations` itself.

Idempotency: `page_captures.idempotency_key` is UNIQUE. `insert_capture`
uses `ON CONFLICT (idempotency_key) DO NOTHING` and reports whether a new
row was actually created, so a retried POST (extension retry, page
reload) never double-inserts and the caller can still return a consistent
202-style response either way.

TODO (documented pipeline-integration seam, explicitly out of scope for
this task -- see the ingest task's HANDOFF): a capture graduates from
PENDING_REVIEW to LINKED by being matched to a `listings`/`product_variants`
row and surfaced as a candidate the deal engine can consider. No code path
here does that matching; `list_pending_captures` exists only so a future
reviewer (human or job) has a read seam to build on.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fpt.capture.validate import ValidatedCapture

if TYPE_CHECKING:
    import psycopg


def insert_capture(cur, capture: ValidatedCapture) -> tuple[int | None, str, bool]:
    """Returns (id, pub_id, created). `created` is False when the row
    already existed (idempotent replay) -- `id`/`pub_id` are still
    returned in that case by re-selecting, so the caller always has
    something to log/return even on a duplicate."""
    cur.execute(
        """
        INSERT INTO page_captures (
            host, retailer_hint, product_url, title_raw, variant_label_raw,
            condition, currency, current_price_cents, was_price_cents,
            captured_at, idempotency_key, raw
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id, pub_id
        """,
        (
            capture.host,
            capture.retailer_hint,
            capture.product_url,
            capture.title_raw,
            capture.variant_label_raw,
            capture.condition.value,
            capture.currency,
            capture.current_price_cents,
            capture.was_price_cents,
            capture.captured_at,
            capture.idempotency_key,
            json.dumps(capture.raw),
        ),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0], row[1], True

    cur.execute(
        "SELECT id, pub_id FROM page_captures WHERE idempotency_key = %s",
        (capture.idempotency_key,),
    )
    existing = cur.fetchone()
    if existing is None:
        # Should be unreachable (we just conflicted on this key), but never invent a value --
        # surface the inconsistency rather than fabricating an id.
        raise RuntimeError(f"capture insert conflicted but no row found for idempotency_key={capture.idempotency_key!r}")
    return existing[0], existing[1], False


def list_pending_captures(cur, *, limit: int = 100) -> list[dict]:
    """Read-only seam for a future reviewer/pipeline step. Not used by
    the ingest server; exists for manual inspection (`psql`, a notebook,
    or a future CLI command) and as the documented extension point."""
    cur.execute(
        """
        SELECT id, pub_id, host, retailer_hint, product_url, title_raw, variant_label_raw,
               condition, currency, current_price_cents, was_price_cents,
               captured_at, received_at, status
        FROM page_captures
        WHERE status = 'PENDING_REVIEW'
        ORDER BY captured_at DESC
        LIMIT %s
        """,
        (limit,),
    )
    columns = [
        "id", "pub_id", "host", "retailer_hint", "product_url", "title_raw", "variant_label_raw",
        "condition", "currency", "current_price_cents", "was_price_cents",
        "captured_at", "received_at", "status",
    ]
    return [dict(zip(columns, row)) for row in cur.fetchall()]
