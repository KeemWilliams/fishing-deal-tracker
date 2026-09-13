"""DB integration tests for fpt/store/captures.py against page_captures
(db/migrations/020_page_captures). Uses the same skip-when-no-DATABASE_URL
harness as the rest of tests/integration/ -- see tests/integration/conftest.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fpt.capture.validate import validate_capture_payload
from fpt.store.captures import insert_capture, list_pending_captures


def _payload(**overrides) -> dict:
    base = {
        "product_url": "https://www.scheels.com/p/some-rod/12345.html",
        "title_raw": "St. Croix Triumph Spinning Rod",
        "current_price_cents": 6497,
    }
    base.update(overrides)
    return base


def test_insert_capture_creates_row(db):
    capture = validate_capture_payload(_payload(idempotency_key="test-insert-row-0001"))
    with db.cursor() as cur:
        capture_id, pub_id, created = insert_capture(cur, capture)
        assert created is True
        assert pub_id.startswith("cap_")

        cur.execute(
            "SELECT host, product_url, title_raw, current_price_cents, was_price_cents, "
            "condition, source, status FROM page_captures WHERE id = %s",
            (capture_id,),
        )
        row = cur.fetchone()
        assert row == (
            "www.scheels.com",
            "https://www.scheels.com/p/some-rod/12345.html",
            "St. Croix Triumph Spinning Rod",
            6497,
            None,
            "NEW",
            "PAGE_CAPTURE",
            "PENDING_REVIEW",
        )


def test_insert_capture_is_idempotent_on_replay(db):
    capture = validate_capture_payload(_payload(idempotency_key="test-idempotent-0001"))
    with db.cursor() as cur:
        id_1, pub_id_1, created_1 = insert_capture(cur, capture)
        id_2, pub_id_2, created_2 = insert_capture(cur, capture)

        assert created_1 is True
        assert created_2 is False
        assert id_1 == id_2
        assert pub_id_1 == pub_id_2

        cur.execute("SELECT count(*) FROM page_captures WHERE idempotency_key = %s", (capture.idempotency_key,))
        assert cur.fetchone()[0] == 1


def test_insert_capture_never_touches_price_observations(db):
    """The core CLAIM-vs-observation guarantee: inserting a capture must
    not create any row in price_observations or offers."""
    idem = "test-no-observation-0001"
    capture = validate_capture_payload(_payload(idempotency_key=idem))
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM price_observations")
        obs_before = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM offers")
        offers_before = cur.fetchone()[0]

        insert_capture(cur, capture)

        cur.execute("SELECT count(*) FROM price_observations")
        obs_after = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM offers")
        offers_after = cur.fetchone()[0]

        assert obs_after == obs_before
        assert offers_after == offers_before


def test_was_price_stored_when_present(db):
    capture = validate_capture_payload(
        _payload(
            current_price_cents=6497,
            was_price_cents=12999,
            idempotency_key="test-was-price-0001",
        )
    )
    with db.cursor() as cur:
        capture_id, _, _ = insert_capture(cur, capture)
        cur.execute("SELECT current_price_cents, was_price_cents FROM page_captures WHERE id = %s", (capture_id,))
        assert cur.fetchone() == (6497, 12999)


def test_list_pending_captures_returns_inserted_row(db):
    capture = validate_capture_payload(_payload(idempotency_key="test-list-pending-0001"))
    with db.cursor() as cur:
        capture_id, pub_id, _ = insert_capture(cur, capture)
        pending = list_pending_captures(cur, limit=10)
        matching = [row for row in pending if row["id"] == capture_id]
        assert len(matching) == 1
        assert matching[0]["pub_id"] == pub_id
        assert matching[0]["status"] == "PENDING_REVIEW"


def test_unique_constraint_enforced_at_db_level_even_bypassing_helper(db):
    """Defense in depth: even a direct INSERT (not through insert_capture)
    cannot create two rows with the same idempotency_key."""
    idem = "test-unique-constraint-0001"
    capture = validate_capture_payload(_payload(idempotency_key=idem))
    with db.cursor() as cur:
        insert_capture(cur, capture)
        import psycopg

        try:
            cur.execute(
                "INSERT INTO page_captures (host, product_url, title_raw, current_price_cents, "
                "captured_at, idempotency_key) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    capture.host,
                    capture.product_url,
                    capture.title_raw,
                    capture.current_price_cents,
                    capture.captured_at,
                    capture.idempotency_key,
                ),
            )
            raised = False
        except psycopg.errors.UniqueViolation:
            raised = True
        assert raised is True
