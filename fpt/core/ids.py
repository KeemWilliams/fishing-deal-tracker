"""Opaque public ID generation.

Architecture doc section 4 calls for "Opaque ULID-based `pub_id` for
anything public." We don't pull in an extra dependency for a full ULID
implementation; this generates a lexicographically-sortable-enough,
timestamp-prefixed opaque ID that is safe to expose publicly (no sequential
integers leaked). Swappable for a real ULID library without changing
callers if that becomes a requirement.
"""

from __future__ import annotations

import os
import time

_CROCKFORD32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_crockford(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        chars.append(_CROCKFORD32[rem])
    return "".join(reversed(chars))


def new_pub_id(prefix: str) -> str:
    """Generate an opaque public id like 'dl_01J8Z3F9K2Q7X8N4M6P0R5T1WC'.

    `prefix` should be a short lowercase tag identifying the entity type
    (e.g. 'dl' for deals, 'vr' for product_variants, 'wt' for watches).
    """
    timestamp_ms = int(time.time() * 1000)
    randomness = int.from_bytes(os.urandom(10), "big")
    time_part = _encode_crockford(timestamp_ms, 10)
    random_part = _encode_crockford(randomness, 16)
    return f"{prefix}_{time_part}{random_part}"
