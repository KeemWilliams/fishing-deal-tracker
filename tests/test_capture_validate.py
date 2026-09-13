"""Unit tests for fpt/capture/validate.py -- no DB, no network, no server."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fpt.capture.validate import (
    CaptureValidationError,
    compute_idempotency_key,
    validate_capture_payload,
)
from fpt.core.models import Condition


def _payload(**overrides) -> dict:
    base = {
        "product_url": "https://www.scheels.com/p/some-rod/12345.html",
        "title_raw": "St. Croix Triumph Spinning Rod",
        "current_price_cents": 6497,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_minimal_payload_parses():
    result = validate_capture_payload(_payload())
    assert result.host == "www.scheels.com"
    assert result.product_url == "https://www.scheels.com/p/some-rod/12345.html"
    assert result.title_raw == "St. Croix Triumph Spinning Rod"
    assert result.current_price_cents == 6497
    assert result.was_price_cents is None
    assert result.condition == Condition.NEW
    assert result.currency == "USD"
    assert result.idempotency_key.startswith("auto_")


def test_valid_full_payload_parses():
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    result = validate_capture_payload(
        _payload(
            was_price_cents=12999,
            variant_label_raw="7'6\" Medium, Fast",
            retailer_hint="Scheels",
            condition="used_good",
            currency="usd",
            captured_at=recent,
            raw={"note": "matched via JSON-LD"},
            idempotency_key="ext-provided-key-001",
        )
    )
    assert result.was_price_cents == 12999
    assert result.variant_label_raw == "7'6\" Medium, Fast"
    assert result.retailer_hint == "Scheels"
    assert result.condition == Condition.USED_GOOD
    assert result.currency == "USD"
    assert result.idempotency_key == "ext-provided-key-001"
    assert result.raw == {"note": "matched via JSON-LD"}


# ---------------------------------------------------------------------------
# URL / scheme rejection -- the core security boundary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        "http://plain-http-not-https.example.com/product",
        "ftp://example.com/product",
        "",
        "not-a-url-at-all",
        "https://",
        "https:///no-host",
    ],
)
def test_rejects_unsafe_or_malformed_urls(bad_url):
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(product_url=bad_url))


def test_rejects_url_with_userinfo():
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(product_url="https://user:pass@scheels.com/p/x"))


def test_rejects_url_with_control_characters():
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(product_url="https://scheels.com/p/\tjavascript:alert(1)"))


def test_host_is_derived_from_url_not_hint():
    """retailer_hint must never influence host/identity -- it's audit-only."""
    result = validate_capture_payload(
        _payload(product_url="https://www.scheels.com/p/x/1.html", retailer_hint="totally-different-retailer.com")
    )
    assert result.host == "www.scheels.com"
    assert result.retailer_hint == "totally-different-retailer.com"


def test_rejects_oversized_url():
    huge = "https://www.scheels.com/" + ("a" * 3000)
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(product_url=huge))


# ---------------------------------------------------------------------------
# Price validation / clamping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_price", [0, -1, -6497, 100_000_01, "6497", 64.97, None, True, False])
def test_rejects_bad_current_price(bad_price):
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(current_price_cents=bad_price))


def test_missing_current_price_rejected():
    payload = _payload()
    del payload["current_price_cents"]
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(payload)


def test_was_price_below_current_is_dropped_not_rejected():
    """A nonsensical was-price shouldn't block an otherwise-good capture --
    it's just dropped (per architecture.md's ClaimedReferenceKind
    guard pattern: 'claimed_not_above_price' is SUSPECT, not fatal)."""
    result = validate_capture_payload(_payload(was_price_cents=100, current_price_cents=6497))
    assert result.was_price_cents is None
    assert result.current_price_cents == 6497


def test_was_price_equal_to_current_is_dropped():
    result = validate_capture_payload(_payload(current_price_cents=6497, was_price_cents=6497))
    assert result.was_price_cents is None


@pytest.mark.parametrize("bad_was", ["free", -1, True])
def test_rejects_malformed_was_price(bad_was):
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(was_price_cents=bad_was))


# ---------------------------------------------------------------------------
# String sanitization / length caps
# ---------------------------------------------------------------------------


def test_title_is_required_and_nonempty():
    payload = _payload(title_raw="   ")
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(payload)


def test_title_control_characters_stripped():
    result = validate_capture_payload(_payload(title_raw="Bad\x00Title\x1fHere"))
    assert "\x00" not in result.title_raw
    assert "\x1f" not in result.title_raw
    assert result.title_raw == "BadTitleHere"


def test_title_is_length_capped():
    result = validate_capture_payload(_payload(title_raw="x" * 10_000))
    assert len(result.title_raw) == 500


def test_variant_label_optional():
    result = validate_capture_payload(_payload())
    assert result.variant_label_raw is None


# ---------------------------------------------------------------------------
# condition mapping
# ---------------------------------------------------------------------------


def test_unrecognized_condition_rejected():
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(condition="pristine_mint"))


def test_condition_defaults_to_new():
    result = validate_capture_payload(_payload())
    assert result.condition == Condition.NEW


def test_currency_must_be_three_letter_code():
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(currency="US Dollars"))


# ---------------------------------------------------------------------------
# captured_at bounds
# ---------------------------------------------------------------------------


def test_captured_at_defaults_to_now():
    before = datetime.now(timezone.utc)
    result = validate_capture_payload(_payload())
    after = datetime.now(timezone.utc)
    assert before <= result.captured_at <= after


def test_captured_at_far_future_rejected():
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(captured_at=future))


def test_captured_at_too_old_rejected():
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(captured_at=old))


def test_captured_at_malformed_rejected():
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(captured_at="not-a-date"))


# ---------------------------------------------------------------------------
# raw passthrough field caps
# ---------------------------------------------------------------------------


def test_raw_field_count_capped():
    huge_raw = {f"field_{i}": "value" for i in range(50)}
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(raw=huge_raw))


def test_raw_must_be_object():
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(raw="not-an-object"))


def test_raw_value_length_capped():
    result = validate_capture_payload(_payload(raw={"note": "x" * 5000}))
    assert len(result.raw["note"]) == 2000


# ---------------------------------------------------------------------------
# top-level body shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_body", [None, [], "a string", 42, True])
def test_non_object_body_rejected(bad_body):
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(bad_body)


# ---------------------------------------------------------------------------
# idempotency key
# ---------------------------------------------------------------------------


def test_idempotency_key_is_deterministic_per_minute():
    ts = datetime(2026, 9, 13, 12, 0, 30, tzinfo=timezone.utc)
    key_a = compute_idempotency_key(
        host="www.scheels.com", product_url="https://www.scheels.com/p/x", variant_label_raw=None,
        current_price_cents=6497, captured_at=ts,
    )
    ts2 = datetime(2026, 9, 13, 12, 0, 59, tzinfo=timezone.utc)
    key_b = compute_idempotency_key(
        host="www.scheels.com", product_url="https://www.scheels.com/p/x", variant_label_raw=None,
        current_price_cents=6497, captured_at=ts2,
    )
    assert key_a == key_b  # same minute bucket -> same key


def test_idempotency_key_differs_on_price():
    ts = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    key_a = compute_idempotency_key(
        host="h", product_url="https://h/p", variant_label_raw=None, current_price_cents=100, captured_at=ts
    )
    key_b = compute_idempotency_key(
        host="h", product_url="https://h/p", variant_label_raw=None, current_price_cents=200, captured_at=ts
    )
    assert key_a != key_b


def test_explicit_idempotency_key_too_short_rejected():
    with pytest.raises(CaptureValidationError):
        validate_capture_payload(_payload(idempotency_key="short"))
