"""Small parsing helpers shared by the page-scraper adapters in this module.

Deliberately private (leading underscore) and adapter-scoped: this is NOT the
core `fetch/blocks.py` or `core/gtin.py` modules described in the repo layout
(architecture doc section 10.2) -- those belong to the fetch layer and
normalizer respectively, and are out of scope for the two adapters built
here. This module only avoids duplicating string-scraping logic between
`academy.py` and `jandh.py`.

Everything here is pure (no I/O) and never raises on malformed input, per the
adapter contract ("`parse` never raises for a malformed page").
"""

from __future__ import annotations

import json
import re
from typing import Any

# Case-insensitive block/challenge-page signatures, from architecture doc
# section 6.2 (observation quality guards). Kept here for the adapter's own
# BLOCKED classification -- see academy.py/jandh.py docstrings for why the
# body-size floor from that same table is NOT applied at this layer.
BLOCK_SIGNATURES: tuple[bytes, ...] = (
    b"edgesuite",
    b"access denied",
    b"site unavailable",
    b"cf-chl",
    b"captcha",
    b"press & hold",
    b"_abck",
)

_LDJSON_RE = re.compile(
    rb'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Shopify's canonical PDP data blob: <script type="application/json" id="ProductJson-...">
_PRODUCT_JSON_RE = re.compile(
    rb'<script[^>]*id=["\']ProductJson[^"\']*["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Any barcode-shaped field, field-name agnostic per architecture doc section
# 4.2 ("Field names are ignored (Academy labels a 13-digit value `gtin8`)").
# Checksum validation is explicitly the Normalizer's job (section 4.2), not
# the adapter's -- this only collects candidate digit strings.
_BARCODE_KEY_RE = re.compile(r"^(gtin\d*|barcode|upc\w*|ean\w*)$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d{6,14}$")

_PACK_COUNT_RE = re.compile(r"(\d+)\s*[-\s]?(?:pack|pk|ct|count)\b", re.IGNORECASE)


def is_blocked(body: bytes) -> str | None:
    """Return the matched block signature, or None if body looks clean."""
    lowered = body.lower()
    for sig in BLOCK_SIGNATURES:
        if sig in lowered:
            return sig.decode("ascii")
    return None


def extract_jsonld_objects(body: bytes) -> list[Any]:
    """Extract every parseable JSON-LD block from an HTML document.

    Malformed blocks are skipped silently (never raises) -- callers that
    want visibility into skipped blocks should compare
    `len(extract_jsonld_objects(body))` against the raw script-tag count
    themselves; this module intentionally keeps the happy-path signature
    simple for adapter code.
    """
    objects: list[Any] = []
    for match in _LDJSON_RE.finditer(body):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(parsed, list):
            objects.extend(parsed)
        else:
            objects.append(parsed)
    return objects


def extract_product_json(body: bytes) -> dict[str, Any] | None:
    """Extract Shopify's `<script id="ProductJson-...">` blob, if present.

    This is the authoritative per-variant source on J&H Tackle product
    pages: the `Product` JSON-LD block only carries an `AggregateOffer`
    (min/max price across all variants), while `ProductJson` carries each
    variant's own price, barcode, and availability.
    """
    match = _PRODUCT_JSON_RE.search(body)
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def find_barcodes(obj: Any) -> list[str]:
    """Recursively collect digit-string values under any barcode-shaped key.

    Field-name agnostic by design (architecture doc 4.2). Does not validate
    checksums or pad to GTIN-14 -- that is the Normalizer's job.
    """
    found: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if _BARCODE_KEY_RE.match(key) and isinstance(value, str):
                    digits = value.strip()
                    if _DIGITS_RE.match(digits):
                        found.append(digits)
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(obj)
    # Stable de-dupe, preserving first-seen order.
    seen: set[str] = set()
    deduped: list[str] = []
    for code in found:
        if code not in seen:
            seen.add(code)
            deduped.append(code)
    return deduped


def parse_pack_count(text: str | None) -> int | None:
    """Best-effort pack/unit count from free text, e.g. '13-Pack', '25 Pack'."""
    if not text:
        return None
    match = _PACK_COUNT_RE.search(text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def decode_html(body: bytes) -> str:
    """Best-effort text decode for title/redirect checks. Never raises."""
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("utf-8", errors="replace")


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


def extract_title(body: bytes) -> str:
    match = _TITLE_RE.search(decode_html(body))
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()
