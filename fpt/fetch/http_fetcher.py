"""HTTP fetch layer built on scrapling's plain `Fetcher`.

Per architecture doc A4 / 7.2: plain HTTP only, identified user agent, no
stealth or bypass tooling by default. `HttpFetcher` accepts a
`use_stealth` flag (default False) so a future, explicitly-approved
per-retailer escalation (Q3/Q4 owner decision) is a config change, not a
code change -- but nothing in this codebase turns it on today. The proxy
slot (`EgressConfig.proxy_url_env`) is likewise wired through and left
unset; DIRECT is the only mode exercised by tackle_warehouse.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from fpt.core.models import FetchRequest, FetchResponse
from fpt.fetch.fetcher import EgressConfig, FetchTransportError
from fpt.fetch.snapshots import write_snapshot


class HttpFetcher:
    """Fetcher implementation for page_scraper adapters (architecture doc 3.2).

    `use_stealth` and `proxy_url_env` are read from EgressConfig at call
    time so the same instance serves every retailer with per-retailer
    policy; neither is exercised in the MVP admission set (tackle_warehouse
    is DIRECT, no stealth -- see architecture doc 7.1/7.3).
    """

    def __init__(self, *, use_stealth: bool = False, snapshot_dir=None):
        self.use_stealth = use_stealth
        self.snapshot_dir = snapshot_dir

    def fetch(
        self,
        request: FetchRequest,
        egress: EgressConfig,
        user_agent: str,
        timeout_s: float,
    ) -> FetchResponse:
        if egress.mode == "API":
            raise FetchTransportError(
                "connection", "HttpFetcher does not handle API egress mode"
            )

        proxy_url = None
        if egress.mode == "PROXY":
            if not egress.proxy_url_env:
                raise FetchTransportError(
                    "proxy", "PROXY egress mode requires proxy_url_env"
                )
            proxy_url = os.environ.get(egress.proxy_url_env)
            if not proxy_url:
                raise FetchTransportError(
                    "proxy", f"proxy env var {egress.proxy_url_env} is unset"
                )

        headers = {"User-Agent": user_agent}

        try:
            response = self._do_fetch(request, headers, proxy_url, timeout_s)
        except FetchTransportError:
            raise
        except Exception as exc:  # noqa: BLE001 - classify anything else as connection
            raise FetchTransportError("connection", str(exc)) from exc

        fetched_at = datetime.now(timezone.utc)
        body = bytes(getattr(response, "body", None) or b"")
        status = int(getattr(response, "status", 0))
        final_url = str(getattr(response, "url", request.url) or request.url)
        raw_headers = dict(getattr(response, "headers", {}) or {})
        elapsed_ms = int(getattr(response, "elapsed_ms", 0) or 0)

        snapshot_ref = ""
        try:
            snapshot_ref = write_snapshot(
                body,
                task_id=request.task_id,
                fetched_at=fetched_at,
                snapshot_dir=self.snapshot_dir,
            )
        except Exception:  # noqa: BLE001 - snapshot failure must not block the pipeline
            snapshot_ref = ""

        return FetchResponse(
            request=request,
            status=status,
            final_url=final_url,
            headers=raw_headers,
            body=body,
            elapsed_ms=elapsed_ms,
            fetched_at=fetched_at,
            egress_mode=egress.mode,
            snapshot_ref=snapshot_ref,
        )

    def _do_fetch(self, request: FetchRequest, headers: dict, proxy_url: str | None, timeout_s: float):
        """Isolated for testability: tests monkeypatch this rather than the
        network. Uses scrapling's plain `Fetcher` (never StealthyFetcher)
        unless `use_stealth` was explicitly set on this instance."""
        if self.use_stealth:
            from scrapling.fetchers import StealthyFetcher

            kwargs = {"headers": headers, "timeout": timeout_s}
            if proxy_url:
                kwargs["proxy"] = proxy_url
            return StealthyFetcher.fetch(request.url, **kwargs)

        from scrapling.fetchers import Fetcher

        kwargs = {"headers": headers, "timeout": timeout_s}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return Fetcher.get(request.url, **kwargs)
