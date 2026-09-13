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
from fpt.fetch.redact import redact
from fpt.fetch.snapshots import write_snapshot

# Security review M3: cap how many bytes a single response body may carry.
# scrapling's plain `Fetcher.get` downloads the full body before returning
# control here, so this is a post-download guard rather than a true
# streaming cap (flagged as a HANDOFF limitation) -- but it still stops an
# oversized/hostile response from ever reaching the adapter parser, the
# snapshot writer, or the DB.
MAX_BODY_BYTES = 5 * 1024 * 1024  # 5 MB

# Security review M3: cap redirect hops scrapling's underlying HTTP client
# will follow for one request.
MAX_REDIRECTS = 5


class HttpFetcher:
    """Fetcher implementation for page_scraper adapters (architecture doc 3.2).

    `use_stealth` and `proxy_url_env` are read from EgressConfig at call
    time so the same instance serves every retailer with per-retailer
    policy; neither is exercised in the MVP admission set (tackle_warehouse
    is DIRECT, no stealth -- see architecture doc 7.1/7.3).

    Security review M2: `use_stealth=True` on this constructor is
    necessary but never sufficient on its own -- the CLI (`--use-stealth`)
    must ALSO be combined with the target retailer's own
    `stealth_approved: true` config flag (default false for every
    retailer) before a stealth-capable instance is ever constructed for
    that retailer. This class does not read config itself (it has no
    retailer context); `fpt/cli.py` is the single place that computes the
    AND of both conditions before instantiating one of these per retailer.
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
            raise FetchTransportError("connection", redact(str(exc))) from exc

        fetched_at = datetime.now(timezone.utc)
        body = bytes(getattr(response, "body", None) or b"")
        status = int(getattr(response, "status", 0))
        final_url = str(getattr(response, "url", request.url) or request.url)
        raw_headers = dict(getattr(response, "headers", {}) or {})
        elapsed_ms = int(getattr(response, "elapsed_ms", 0) or 0)

        # Security review M3: an oversized (or hostile) body never reaches
        # the adapter parser, the snapshot writer, or the DB -- treat it
        # exactly like any other transport failure.
        if len(body) > MAX_BODY_BYTES:
            raise FetchTransportError(
                "connection", f"response body exceeds MAX_BODY_BYTES ({len(body)} > {MAX_BODY_BYTES})"
            )

        # Security review M5: a snapshot write failure must be treated as a
        # FAILED FETCH, never as a fetch that "succeeded with no snapshot"
        # -- the previous behavior (swallow to snapshot_ref="") meant every
        # write failure silently produced the SAME empty ref, and
        # fpt/store/observations.py dedupes new observations against an
        # existing one with the same (offer_id, snapshot_ref); an empty ref
        # would have made every subsequent write-failed fetch for an offer
        # look like a replay of the first one and get silently dropped.
        try:
            snapshot_ref = write_snapshot(
                body,
                task_id=request.task_id,
                fetched_at=fetched_at,
                snapshot_dir=self.snapshot_dir,
            )
        except Exception as exc:  # noqa: BLE001
            raise FetchTransportError("connection", f"snapshot write failed: {redact(str(exc))}") from exc

        if not snapshot_ref:  # pragma: no cover - defensive; write_snapshot never returns falsy today
            raise FetchTransportError("connection", "snapshot write returned an empty ref")

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

        # Security review M3: cap redirect hops. Combined with
        # fpt/fetch/url_safety.py's post-fetch check of `final_url`, a
        # malicious or misconfigured redirect chain can neither run away
        # indefinitely nor land somewhere off the retailer's allowlist.
        kwargs = {"headers": headers, "timeout": timeout_s, "max_redirects": MAX_REDIRECTS}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        return Fetcher.get(request.url, **kwargs)
