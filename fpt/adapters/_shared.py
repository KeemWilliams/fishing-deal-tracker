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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

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


# ---------------------------------------------------------------------------
# URL sanitizing (security review H1): a listing/offer URL is parsed from
# untrusted retailer page content (JSON-LD, HTML). Before it is ever
# persisted or published, it must be an absolute https URL whose host is
# one this retailer actually serves -- otherwise a compromised or
# malformed page (or a malicious redirect target landing in
# `response.final_url`) could inject `javascript:`, `data:`, a
# protocol-relative `//evil.com`, plain `http://`, or an entirely
# different host into a link we show to visitors or store as canonical.
#
# `config/retailers.yaml` is the source of truth for each retailer's
# `allowed_hosts` (added by the scheduler/config engineer, security review
# M3, also consumed by fpt/fetch/url_safety.py's SSRF guard). If that key
# is ever missing for a given retailer (a config regression, or a new
# retailer added before its own config entry lands), `FALLBACK_ALLOWED_
# HOSTS` below supplies a conservative local default matching the SAME
# single canonical host each retailer's `allowed_hosts`/`base_url` actually
# uses, so this guard is never silently disabled by a missing config key
# and never admits a bare-apex/www variant the real config doesn't.

_RETAILERS_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "retailers.yaml"

FALLBACK_ALLOWED_HOSTS: dict[str, frozenset[str]] = {
    "tackle_warehouse": frozenset({"www.tacklewarehouse.com"}),
    "academy": frozenset({"www.academy.com"}),
    "jandh": frozenset({"www.jandh.com"}),
    "fishusa": frozenset({"www.fishusa.com"}),
    "tackledirect": frozenset({"www.tackledirect.com"}),
    "alltackle": frozenset({"alltackle.com"}),
}


def load_allowed_hosts(retailer_slug: str, *, config_path: Path | None = None) -> frozenset[str]:
    """Per-retailer allowed hostnames for URL sanitizing. Reads
    `config/retailers.yaml`'s `allowed_hosts` key for the retailer when
    present; falls back to `FALLBACK_ALLOWED_HOSTS` otherwise. Never raises
    on a missing/malformed config file -- falls back instead, since this is
    a security guard that must degrade to "still enforced" not "silently
    skipped"."""
    path = config_path or _RETAILERS_CONFIG_PATH
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        for entry in data.get("retailers") or []:
            if isinstance(entry, dict) and entry.get("slug") == retailer_slug:
                hosts = entry.get("allowed_hosts")
                if hosts:
                    return frozenset(str(h).strip().lower() for h in hosts if h)
                break
    return FALLBACK_ALLOWED_HOSTS.get(retailer_slug, frozenset())


def _is_safe_https_url(url: str, allowed_hosts: frozenset[str]) -> bool:
    if not allowed_hosts:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").strip().lower()
    return bool(host) and host in allowed_hosts


def sanitize_offer_url(
    candidate: str | None,
    *,
    allowed_hosts: frozenset[str],
    fallback_url: str | None = None,
) -> str | None:
    """Returns a URL safe to persist/publish as a listing/offer URL:
    absolute https, host in `allowed_hosts`. Tries `candidate` (parsed from
    untrusted page content) first, then `fallback_url` -- which callers
    MUST pass as the URL *we requested* (`FetchResponse.request.url`),
    never `FetchResponse.final_url`: a malicious or misconfigured redirect
    can land `final_url` on an arbitrary host, and using it as a "trusted"
    fallback would defeat this guard entirely. Returns None if neither
    passes; the caller must not persist/publish a URL built from this
    response."""
    for url in (candidate, fallback_url):
        if url and _is_safe_https_url(url, allowed_hosts):
            return url
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
