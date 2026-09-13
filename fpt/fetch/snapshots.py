"""Raw response snapshot storage (14-day retention per architecture doc 10.1).

Snapshots are gzip'd raw bodies keyed by a content-addressed-ish ref so
that a parser change can be validated against exact historical bytes
without re-fetching. Never stores credentials -- callers pass only the
response body, which by construction (fetch layer contract) never
contains API keys added at call time.
"""

from __future__ import annotations

import gzip
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SNAPSHOT_DIR = Path(os.environ.get("FPT_SNAPSHOT_DIR", "/var/lib/fpt/snapshots"))


def snapshot_ref_for(task_id: int, fetched_at: datetime, body: bytes) -> str:
    """Content-hash + task id + microsecond timestamp (security review M5:
    "snapshot_ref from uuid4 (or content hash + retailer + timestamp),
    never empty"). The previous version truncated to whole seconds and
    carried no content component, so two fetches of the same task within
    one second collided; a content hash also means two BYTE-IDENTICAL
    responses fetched moments apart still get distinguishable refs because
    the timestamp differs, while making a collision on genuinely different
    bytes astronomically unlikely even at the same microsecond."""
    stamp = fetched_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(body).hexdigest()[:16]
    return f"{stamp}-task{task_id}-{digest}.gz"


def write_snapshot(
    body: bytes,
    *,
    task_id: int,
    fetched_at: datetime,
    snapshot_dir: Path | None = None,
) -> str:
    """Write a gzip'd snapshot and return its ref (relative filename).

    Directory creation and writes are best-effort: a snapshot write
    failure must never block the pipeline from storing the parsed
    observation, so callers should treat exceptions here as non-fatal and
    log rather than propagate. This function itself raises on failure;
    it is the caller's job to decide how tolerant to be.
    """
    directory = snapshot_dir or DEFAULT_SNAPSHOT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    ref = snapshot_ref_for(task_id, fetched_at, body)
    with gzip.open(directory / ref, "wb") as fh:
        fh.write(body)
    return ref


def read_snapshot(ref: str, *, snapshot_dir: Path | None = None) -> bytes:
    directory = snapshot_dir or DEFAULT_SNAPSHOT_DIR
    with gzip.open(directory / ref, "rb") as fh:
        return fh.read()
