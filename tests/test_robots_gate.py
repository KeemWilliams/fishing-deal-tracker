"""Unit tests for fpt/fetch/robots.py's RobotsGate (security review M2:
robots.txt admission cached and checked before every fetch). No network --
a fake Fetcher stands in for the HTTP layer."""

from __future__ import annotations

from datetime import datetime, timezone

from fpt.core.models import FetchResponse
from fpt.fetch.fetcher import EgressConfig
from fpt.fetch.robots import RobotsGate

_ALLOW_ALL = b"User-agent: *\nDisallow:\n"
_DISALLOW_ALL = b"User-agent: *\nDisallow: /\n"
_CRAWL_DELAY_30 = b"User-agent: *\nCrawl-delay: 30\nDisallow:\n"


def _resolver(host: str) -> list[str]:
    return ["93.184.216.34"]


class _FakeFetcher:
    def __init__(self, robots_body: bytes):
        self._robots_body = robots_body
        self.calls = 0

    def fetch(self, request, egress, user_agent, timeout_s):
        self.calls += 1
        return FetchResponse(
            request=request, status=200, final_url=request.url, headers={},
            body=self._robots_body, elapsed_ms=1, fetched_at=datetime.now(timezone.utc),
            egress_mode="DIRECT", snapshot_ref="robots",
        )


def test_disallowed_path_is_denied():
    gate = RobotsGate()
    fetcher = _FakeFetcher(_DISALLOW_ALL)
    check = gate.admits(
        retailer_slug="tw", base_url="https://www.tacklewarehouse.com", path="/catpage-CLEARRODSPP.html",
        fetcher=fetcher, egress=EgressConfig(mode="DIRECT"), user_agent="FishPriceBot/0.1", timeout_s=1,
        allowed_hosts=["www.tacklewarehouse.com"], resolver=_resolver,
    )
    assert check.allowed is False


def test_allowed_path_is_admitted():
    gate = RobotsGate()
    fetcher = _FakeFetcher(_ALLOW_ALL)
    check = gate.admits(
        retailer_slug="tw", base_url="https://www.tacklewarehouse.com", path="/catpage-CLEARRODSPP.html",
        fetcher=fetcher, egress=EgressConfig(mode="DIRECT"), user_agent="FishPriceBot/0.1", timeout_s=1,
        allowed_hosts=["www.tacklewarehouse.com"], resolver=_resolver,
    )
    assert check.allowed is True


def test_robots_txt_is_fetched_only_once_per_retailer():
    gate = RobotsGate()
    fetcher = _FakeFetcher(_ALLOW_ALL)
    for path in ("/a", "/b", "/c"):
        gate.admits(
            retailer_slug="tw", base_url="https://www.tacklewarehouse.com", path=path,
            fetcher=fetcher, egress=EgressConfig(mode="DIRECT"), user_agent="FishPriceBot/0.1", timeout_s=1,
            allowed_hosts=["www.tacklewarehouse.com"], resolver=_resolver,
        )
    assert fetcher.calls == 1


def test_unreachable_robots_txt_fails_closed():
    class _FailingFetcher:
        def fetch(self, *a, **k):
            raise RuntimeError("connection refused")

    gate = RobotsGate()
    check = gate.admits(
        retailer_slug="tw", base_url="https://www.tacklewarehouse.com", path="/x",
        fetcher=_FailingFetcher(), egress=EgressConfig(mode="DIRECT"), user_agent="FishPriceBot/0.1", timeout_s=1,
        allowed_hosts=["www.tacklewarehouse.com"], resolver=_resolver,
    )
    assert check.allowed is False


def test_crawl_delay_is_read_back():
    gate = RobotsGate()
    fetcher = _FakeFetcher(_CRAWL_DELAY_30)
    delay = gate.crawl_delay(
        retailer_slug="tw", base_url="https://www.tacklewarehouse.com", fetcher=fetcher,
        egress=EgressConfig(mode="DIRECT"), user_agent="FishPriceBot/0.1", timeout_s=1,
        allowed_hosts=["www.tacklewarehouse.com"], resolver=_resolver,
    )
    assert delay == 30.0
