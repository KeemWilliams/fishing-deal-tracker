"""Fetch layer contract (architecture doc 3.2).

`ApiFetcher` (Phase 2) adds credentials from `api_key_env` at call time;
credentials never appear in FetchRequest.url, crawl_tasks, fetch_log, or
snapshots -- the snapshot stores the response body only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from fpt.core.models import FetchRequest, FetchResponse


class FetchTransportError(Exception):
    """Raised for anything that never reached a real HTTP response.

    kind is one of: "timeout", "dns", "tls", "connection", "proxy".
    HTTP status is data (lives on FetchResponse.status), never an
    exception -- a 403/500/etc. is a normal return value, not a raise.
    """

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        super().__init__(message or kind)


@dataclass(frozen=True)
class EgressConfig:
    mode: Literal["DIRECT", "PROXY", "API"]
    proxy_url_env: str | None = None  # env var name, value from Infisical via Coolify
    api_key_env: str | None = None  # data API only, e.g. "FPT_KEEPA_API_KEY"
    sticky_session_minutes: int | None = None


class Fetcher(Protocol):
    def fetch(
        self,
        request: FetchRequest,
        egress: EgressConfig,
        user_agent: str,
        timeout_s: float,
    ) -> FetchResponse:
        """Raises FetchTransportError. HTTP status is data, never an
        exception."""
        ...
