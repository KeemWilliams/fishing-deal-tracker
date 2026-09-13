"""A `Fetcher` (fpt/fetch/fetcher.py Protocol) implementation that serves
local fixture bytes instead of making any network call.

This is the first-class, documented no-network path for `fpt tick`
(`--fixtures-manifest`), flagged as missing by the TEST phase. It reads a
JSON manifest mapping URL -> file path (relative to the manifest's own
directory, or absolute) and returns whatever bytes are at that path for a
matching request URL. `fetched_at` is controlled by the caller-supplied
`now`, not wall-clock time, so a test (or a scripted offline run) can
advance it deterministically between calls to simulate the passage of
several ticks without ever sleeping -- this is what makes the confirmation
gap (architecture 6.4 C1, >= 10 real minutes) testable without faking it:
the SAME fetcher instance is reused across two `run_tick()` calls with
`now` advanced by more than the gap in between.

Unmatched URLs return a 404 FetchResponse (empty body) rather than raising,
matching `HttpFetcher`'s contract that HTTP status is data, never an
exception.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fpt.core.models import FetchRequest, FetchResponse
from fpt.fetch.fetcher import EgressConfig


class FixtureFetcher:
    def __init__(self, url_to_path: dict[str, Path | str], *, base_dir: Path | None = None):
        self._url_to_path = dict(url_to_path)
        self._base_dir = base_dir

    @classmethod
    def from_manifest(cls, manifest_path: Path | str) -> "FixtureFetcher":
        """`manifest_path` is a JSON file: `{"<url>": "<relative-or-absolute-path>", ...}`.
        Relative paths resolve against the manifest's own directory."""
        manifest_path = Path(manifest_path)
        with open(manifest_path, "r", encoding="utf-8") as fh:
            mapping = json.load(fh)
        return cls(mapping, base_dir=manifest_path.parent)

    def _resolve(self, path_str: str) -> Path:
        path = Path(path_str)
        if path.is_absolute() or self._base_dir is None:
            return path
        return self._base_dir / path

    def fetch(
        self,
        request: FetchRequest,
        egress: EgressConfig,
        user_agent: str,
        timeout_s: float,
        *,
        now: datetime | None = None,
    ) -> FetchResponse:
        fetched_at = now or datetime.now(timezone.utc)
        path_str = self._url_to_path.get(request.url)
        if path_str is None:
            return FetchResponse(
                request=request, status=404, final_url=request.url, headers={}, body=b"",
                elapsed_ms=0, fetched_at=fetched_at, egress_mode=egress.mode,
                snapshot_ref=f"fixture-missing:{request.url}",
            )
        body = self._resolve(path_str).read_bytes()
        return FetchResponse(
            request=request, status=200, final_url=request.url, headers={}, body=body,
            elapsed_ms=0, fetched_at=fetched_at, egress_mode=egress.mode,
            snapshot_ref=f"fixture:{request.url}:{fetched_at.isoformat()}",
        )


class ClockedFixtureFetcher:
    """Wraps `FixtureFetcher` to satisfy the plain `Fetcher.fetch(request,
    egress, user_agent, timeout_s)` signature (no `now` kwarg) while still
    stamping every response with a caller-controlled clock -- this is the
    object `fpt.cli.run_tick(fetcher=...)` actually receives, since the
    Fetcher Protocol has no `now` parameter."""

    def __init__(self, fixture_fetcher: FixtureFetcher, *, now: datetime):
        self._fixture_fetcher = fixture_fetcher
        self.now = now

    def fetch(self, request: FetchRequest, egress: EgressConfig, user_agent: str, timeout_s: float) -> FetchResponse:
        return self._fixture_fetcher.fetch(request, egress, user_agent, timeout_s, now=self.now)
