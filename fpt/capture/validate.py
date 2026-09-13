"""Validation and sanitization for browser-capture ingest payloads.

Pure, dependency-free (stdlib only) so this can be unit-tested without a
server or a database. `fpt/capture/server.py` is the only caller in
production; `fpt/store/captures.py` is the only thing that turns a
`ValidatedCapture` into a database row.

Threat model: the caller is a browser extension running in the context of
an untrusted, adversarial web page (the retailer's own page could contain
script that tries to reach our ingest endpoint with attacker-controlled
values, either directly or by tricking the content script's DOM read).
Every field here is treated as hostile input, per `security-and-hardening`
and `pact-security-patterns`:

  - `product_url` must be `https` and NEVER `javascript:`, `data:`,
    `file:`, or any other scheme -- and the retailer/host is ALWAYS
    DERIVED from this URL server-side, never trusted from the extension's
    self-reported `retailer_hint` (which is kept only for display/audit).
  - Prices are clamped to a sane positive-integer-cents range; a
    non-numeric, negative, zero, or absurdly large price is rejected
    rather than silently coerced (no `price or 0` -- see
    feedback_nullable_int_constraints in memory: never mask a bad value
    with a fallback zero).
  - Strings are length-capped and stripped of control characters before
    they ever reach jsonb/postgres or an email/HTML rendering path later.
  - The whole payload is capped by `MAX_BODY_BYTES` at the transport layer
    (`fpt/capture/server.py`) before this module ever sees it.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from fpt.core.models import Condition

# ---------------------------------------------------------------------------
# Limits (deliberately conservative -- this is a personal-use MVP endpoint,
# not a public API; tighten never loosen without re-reading this module).
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = 32 * 1024  # 32 KiB -- a captured product page payload is tiny JSON, not HTML
MAX_TITLE_LEN = 500
MAX_VARIANT_LEN = 300
MAX_URL_LEN = 2048
MAX_HINT_LEN = 100
MAX_RAW_FIELD_LEN = 2000  # per-value cap inside the `raw` passthrough blob
MAX_RAW_FIELDS = 20

MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 100_000_00  # $100,000 -- generous ceiling for boats/motors, still bounded

ALLOWED_SCHEMES = frozenset({"https"})

# captured_at must be plausible: not absurdly in the future (clock skew tolerance) and not
# absurdly old (an extension replaying a stale cached payload).
CAPTURED_AT_FUTURE_SKEW = timedelta(minutes=5)
CAPTURED_AT_MAX_AGE = timedelta(days=7)

_CONDITION_ALIASES = {
    "NEW": Condition.NEW,
    "NEW_OPEN_BOX": Condition.NEW_OPEN_BOX,
    "OPEN_BOX": Condition.NEW_OPEN_BOX,
    "REFURBISHED": Condition.REFURBISHED,
    "REFURB": Condition.REFURBISHED,
    "USED_LIKE_NEW": Condition.USED_LIKE_NEW,
    "USED_VERY_GOOD": Condition.USED_VERY_GOOD,
    "USED_GOOD": Condition.USED_GOOD,
    "USED_ACCEPTABLE": Condition.USED_ACCEPTABLE,
    "USED_UNGRADED": Condition.USED_UNGRADED,
    "USED": Condition.USED_UNGRADED,
}

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class CaptureValidationError(ValueError):
    """Raised with a machine-readable `code` for the structured error body
    (`fpt/capture/server.py` maps this straight to
    `{"error": {"code": ..., "message": ...}}`)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidatedCapture:
    product_url: str
    host: str
    retailer_hint: str | None
    title_raw: str
    variant_label_raw: str | None
    condition: Condition
    currency: str
    current_price_cents: int
    was_price_cents: int | None
    captured_at: datetime
    idempotency_key: str
    raw: dict[str, str]


def _require(data: dict, field: str, *, type_name: str = "string") -> object:
    if field not in data or data[field] is None:
        raise CaptureValidationError("VALIDATION_ERROR", f"{field} is required")
    value = data[field]
    if type_name == "string" and not isinstance(value, str):
        raise CaptureValidationError("VALIDATION_ERROR", f"{field} must be a string")
    return value


def _clean_string(value: str, *, max_len: int, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise CaptureValidationError("VALIDATION_ERROR", f"{field} must be a string")
    # NFC-normalize then strip control characters (defense against homoglyph/log-injection
    # tricks and against raw bytes that would break jsonb/display rendering downstream).
    cleaned = _CONTROL_CHARS_RE.sub("", unicodedata.normalize("NFC", value)).strip()
    if not cleaned and not allow_empty:
        raise CaptureValidationError("VALIDATION_ERROR", f"{field} must not be empty")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _validate_url(raw_url: object) -> tuple[str, str]:
    """Returns (normalized_url, host). Rejects anything that is not a
    well-formed https URL -- this is the single place `javascript:`,
    `data:`, `file:`, bare paths, and off-host tricks get rejected, so
    every other code path can trust `host` came from a real https URL."""
    if not isinstance(raw_url, str) or not raw_url:
        raise CaptureValidationError("VALIDATION_ERROR", "product_url is required")
    if len(raw_url) > MAX_URL_LEN:
        raise CaptureValidationError("VALIDATION_ERROR", "product_url is too long")

    # Reject control/whitespace characters that some URL parsers strip silently (a classic
    # bypass for scheme confusion, e.g. "java\tscript:alert(1)").
    if any(ord(ch) < 0x20 for ch in raw_url):
        raise CaptureValidationError("VALIDATION_ERROR", "product_url contains control characters")

    try:
        parsed = urlparse(raw_url)
    except ValueError as exc:
        raise CaptureValidationError("VALIDATION_ERROR", "product_url could not be parsed") from exc

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise CaptureValidationError(
            "UNSAFE_URL", f"product_url scheme must be https, got {parsed.scheme or '(none)'!r}"
        )
    host = (parsed.hostname or "").lower()
    if not host or "." not in host:
        raise CaptureValidationError("VALIDATION_ERROR", "product_url has no valid host")
    if parsed.username or parsed.password:
        raise CaptureValidationError("UNSAFE_URL", "product_url must not contain userinfo")

    return raw_url, host


def _validate_price_cents(value: object, *, field: str, required: bool) -> int | None:
    if value is None:
        if required:
            raise CaptureValidationError("VALIDATION_ERROR", f"{field} is required")
        return None
    # Booleans are ints in Python -- explicitly reject before the isinstance(int) check below
    # would otherwise accept `True`/`False` as 1/0.
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaptureValidationError("VALIDATION_ERROR", f"{field} must be a positive integer (cents)")
    if value < MIN_PRICE_CENTS or value > MAX_PRICE_CENTS:
        raise CaptureValidationError(
            "VALIDATION_ERROR", f"{field} must be between {MIN_PRICE_CENTS} and {MAX_PRICE_CENTS}"
        )
    return value


def _validate_condition(value: object) -> Condition:
    if value is None:
        return Condition.NEW
    if not isinstance(value, str):
        raise CaptureValidationError("VALIDATION_ERROR", "condition must be a string")
    mapped = _CONDITION_ALIASES.get(value.strip().upper())
    if mapped is None:
        raise CaptureValidationError("VALIDATION_ERROR", f"unrecognized condition: {value!r}")
    return mapped


def _validate_captured_at(value: object) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, str):
        raise CaptureValidationError("VALIDATION_ERROR", "captured_at must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CaptureValidationError("VALIDATION_ERROR", "captured_at is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    now = datetime.now(timezone.utc)
    if parsed > now + CAPTURED_AT_FUTURE_SKEW:
        raise CaptureValidationError("VALIDATION_ERROR", "captured_at is in the future")
    if parsed < now - CAPTURED_AT_MAX_AGE:
        raise CaptureValidationError("VALIDATION_ERROR", "captured_at is too old")
    return parsed


def _clean_raw_passthrough(value: object) -> dict[str, str]:
    """The extension may attach small extra fields (e.g. the JSON-LD
    fragment it matched against) for audit/debugging. Capped in both
    field count and per-value length so this can never be used to smuggle
    a large or malicious blob into jsonb."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CaptureValidationError("VALIDATION_ERROR", "raw must be an object")
    if len(value) > MAX_RAW_FIELDS:
        raise CaptureValidationError("VALIDATION_ERROR", f"raw must have at most {MAX_RAW_FIELDS} fields")
    cleaned: dict[str, str] = {}
    for key, val in value.items():
        if not isinstance(key, str) or len(key) > 100:
            raise CaptureValidationError("VALIDATION_ERROR", "raw keys must be short strings")
        str_val = val if isinstance(val, str) else repr(val)
        cleaned[_clean_string(key, max_len=100, field="raw key", allow_empty=False)] = _clean_string(
            str_val, max_len=MAX_RAW_FIELD_LEN, field=f"raw[{key}]", allow_empty=True
        )
    return cleaned


def compute_idempotency_key(
    *, host: str, product_url: str, variant_label_raw: str | None, current_price_cents: int, captured_at: datetime
) -> str:
    """Deterministic fallback key when the extension doesn't supply one:
    the same page, same variant text, same price, captured within the
    same minute collapses to one row. This makes a page reload or a
    retried POST from the extension's background worker structurally a
    no-op (unique constraint on `page_captures.idempotency_key`) rather
    than relying on the client behaving well."""
    bucket_minute = captured_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
    material = "|".join(
        [host, product_url, variant_label_raw or "", str(current_price_cents), bucket_minute]
    )
    return "auto_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]


def validate_capture_payload(data: object) -> ValidatedCapture:
    """Raises `CaptureValidationError` on any problem. Never returns a
    partially-valid result -- callers get a fully validated object or an
    exception, nothing in between."""
    if not isinstance(data, dict):
        raise CaptureValidationError("VALIDATION_ERROR", "request body must be a JSON object")

    product_url, host = _validate_url(_require(data, "product_url"))
    title_raw = _clean_string(str(_require(data, "title_raw")), max_len=MAX_TITLE_LEN, field="title_raw")

    variant_raw = data.get("variant_label_raw")
    variant_label_raw = (
        _clean_string(variant_raw, max_len=MAX_VARIANT_LEN, field="variant_label_raw", allow_empty=True)
        if variant_raw is not None
        else None
    )
    variant_label_raw = variant_label_raw or None

    retailer_hint_raw = data.get("retailer_hint")
    retailer_hint = (
        _clean_string(retailer_hint_raw, max_len=MAX_HINT_LEN, field="retailer_hint", allow_empty=True)
        if retailer_hint_raw is not None
        else None
    )
    retailer_hint = retailer_hint or None

    condition = _validate_condition(data.get("condition"))

    currency_raw = data.get("currency", "USD")
    if not isinstance(currency_raw, str) or not re.fullmatch(r"[A-Z]{3}", currency_raw.strip().upper()):
        raise CaptureValidationError("VALIDATION_ERROR", "currency must be a 3-letter ISO code")
    currency = currency_raw.strip().upper()

    current_price_cents = _validate_price_cents(
        data.get("current_price_cents"), field="current_price_cents", required=True
    )
    was_price_cents = _validate_price_cents(data.get("was_price_cents"), field="was_price_cents", required=False)
    if was_price_cents is not None and was_price_cents <= current_price_cents:
        # Not a hard rejection of the whole payload -- a bogus/nonsensical "was" price is
        # dropped rather than blocking a perfectly good capture of the current price.
        was_price_cents = None

    captured_at = _validate_captured_at(data.get("captured_at"))
    raw = _clean_raw_passthrough(data.get("raw"))

    idempotency_key_raw = data.get("idempotency_key")
    if idempotency_key_raw is not None:
        if not isinstance(idempotency_key_raw, str) or not (8 <= len(idempotency_key_raw) <= 128):
            raise CaptureValidationError("VALIDATION_ERROR", "idempotency_key must be a string of 8-128 chars")
        idempotency_key = _clean_string(idempotency_key_raw, max_len=128, field="idempotency_key")
    else:
        idempotency_key = compute_idempotency_key(
            host=host,
            product_url=product_url,
            variant_label_raw=variant_label_raw,
            current_price_cents=current_price_cents,
            captured_at=captured_at,
        )

    return ValidatedCapture(
        product_url=product_url,
        host=host,
        retailer_hint=retailer_hint,
        title_raw=title_raw,
        variant_label_raw=variant_label_raw,
        condition=condition,
        currency=currency,
        current_price_cents=current_price_cents,
        was_price_cents=was_price_cents,
        captured_at=captured_at,
        idempotency_key=idempotency_key,
        raw=raw,
    )
