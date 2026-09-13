"""Block / silent-failure detection (architecture doc 6.2, last row).

Catches the two failure shapes that look nothing like an HTTP error:
- a 200 response carrying a bot-mitigation challenge page instead of
  content ("Access Denied", "cf-chl", captcha, etc.)
- a 200 response that is technically valid HTML but empty of the content
  the adapter needs (missing sentinels, tiny body, redirected to home)
"""

from __future__ import annotations

from dataclasses import dataclass

from fpt.core.models import ResponseOutcome

# Signature substrings observed in bot-mitigation / block pages across the
# retailers in scope (architecture doc 6.2). Matched case-insensitively
# against the response body.
BLOCK_SIGNATURES: tuple[bytes, ...] = (
    b"edgesuite",
    b"access denied",
    b"site unavailable",
    b"cf-chl",
    b"captcha",
    b"press & hold",
    b"press and hold",
    b"_abck",
)

MIN_BODY_BYTES = 20_000


@dataclass(frozen=True)
class BlockCheckResult:
    outcome: ResponseOutcome
    block_signature: str | None


def detect_block_or_empty(
    *,
    status: int,
    body: bytes,
    final_url: str,
    request_url: str,
    sentinels: tuple[bytes, ...],
) -> BlockCheckResult:
    """Classify a fetch response as OK / BLOCKED / EMPTY.

    Order of checks matters: an explicit block signature always wins over
    "just looks small", since a challenge page can legitimately be small.
    Sentinel absence is checked last and only flags EMPTY when the body is
    otherwise plausible-sized HTML with no signature match -- a genuinely
    sold-out or restructured page, not necessarily a block.
    """
    lowered = body.lower()

    for sig in BLOCK_SIGNATURES:
        if sig in lowered:
            return BlockCheckResult(ResponseOutcome.BLOCKED, sig.decode("ascii"))

    if status in (403, 429):
        return BlockCheckResult(ResponseOutcome.BLOCKED, f"http_{status}")

    if status == 404:
        return BlockCheckResult(ResponseOutcome.NOT_FOUND, None)

    if len(body) < MIN_BODY_BYTES:
        # Small bodies are common for TW listing pages with few items; only
        # treat as EMPTY when sentinels are also absent (checked below).
        pass

    if sentinels and not any(s in body for s in sentinels):
        return BlockCheckResult(ResponseOutcome.EMPTY, None)

    if not body.strip():
        return BlockCheckResult(ResponseOutcome.EMPTY, None)

    return BlockCheckResult(ResponseOutcome.OK, None)
