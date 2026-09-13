"""Smoke test for `fpt tick`: exercises the full discover -> fetch ->
block-check -> parse -> validate pipeline end to end using a monkeypatched
fetcher (no real network calls), against the real Tackle Warehouse
adapter and registry.

Security review follow-up (2026-09-13): `fpt tick` now checks robots.txt
admission and the SSRF allowlist/DNS guard before every fetch, and sleeps
`RetailerLimiter.next_delay_s()` between fetches. All three matter for a
FAST, NETWORK-FREE test suite:
  - the stub fetcher must also answer a `/robots.txt` request (an
    "unreachable robots.txt" fails closed -- nothing would ever be fetched)
  - `resolver=` is always passed as a fake DNS stand-in so no test ever
    performs a real DNS lookup
  - `sleep_fn=` (or a monkeypatched `time.sleep`, for the `main()` test
    which has no other way to inject it) is always a no-op so these tests
    run in milliseconds, not tens of real seconds
"""

from datetime import datetime, timezone

import fpt.cli as cli_module
import fpt.fetch.url_safety as url_safety_module
from fpt.core.models import FetchResponse

_ROBOTS_ALLOW_ALL = b"User-agent: *\nDisallow:\n"


def _fake_resolver(host: str) -> list[str]:
    return ["93.184.216.34"]  # a public-looking, non-reserved test address


class _StubFetcher:
    """Serves fixture bytes keyed by URL substring instead of hitting the
    network, so this test never makes an HTTP request. Always answers a
    robots.txt request with an allow-everything body so the new admission
    check (security review M2) never fails this test closed."""

    def __init__(self, body_by_url_substring: dict[str, bytes]):
        self._bodies = body_by_url_substring

    def fetch(self, request, egress, user_agent, timeout_s):
        if request.url.endswith("/robots.txt"):
            return FetchResponse(
                request=request, status=200, final_url=request.url, headers={},
                body=_ROBOTS_ALLOW_ALL, elapsed_ms=1, fetched_at=datetime.now(timezone.utc),
                egress_mode="DIRECT", snapshot_ref="robots-fixture",
            )
        body = b""
        for substring, content in self._bodies.items():
            if substring in request.url:
                body = content
                break
        return FetchResponse(
            request=request,
            status=200,
            final_url=request.url,
            headers={},
            body=body,
            elapsed_ms=10,
            fetched_at=datetime.now(timezone.utc),
            egress_mode="DIRECT",
            snapshot_ref=f"stub:{request.url}",
        )


def test_run_tick_dry_run_produces_observations(monkeypatch, tmp_path):
    fixtures_dir = (
        __file__.rsplit("/", 1)[0] if "/" in __file__ else __file__.rsplit("\\", 1)[0]
    )
    import pathlib

    fixtures = pathlib.Path(fixtures_dir) / "fixtures" / "tackle_warehouse"
    clearance_body = (fixtures / "clearance_rods.html").read_bytes()
    used_body = (fixtures / "used_listing.html").read_bytes()

    stub = _StubFetcher(
        {
            "CLEARRODSPP": clearance_body,
            "USED.html": used_body,
        }
    )
    monkeypatch.setattr(cli_module, "HttpFetcher", lambda use_stealth=False: stub)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    summary = cli_module.run_tick(
        max_discovery_pages=1, resolver=_fake_resolver, sleep_fn=lambda s: None
    )

    assert "tackle_warehouse" in [r["slug"] for r in summary["retailers"]]
    tw_summary = next(r for r in summary["retailers"] if r["slug"] == "tackle_warehouse")
    assert len(tw_summary["pages"]) >= 1
    first_page = tw_summary["pages"][0]
    assert first_page["outcome"] == "OK"
    assert first_page["discovered_count"] > 0
    assert summary["db"].startswith("dry-run")


def test_run_tick_handles_block_page_gracefully(monkeypatch):
    import pathlib

    fixtures = pathlib.Path(__file__).parent / "fixtures" / "tackle_warehouse"
    block_body = (fixtures / "block_page.html").read_bytes()

    stub = _StubFetcher({"CLEARRODSPP": block_body, "USED.html": block_body})
    monkeypatch.setattr(cli_module, "HttpFetcher", lambda use_stealth=False: stub)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    summary = cli_module.run_tick(
        max_discovery_pages=1, resolver=_fake_resolver, sleep_fn=lambda s: None
    )
    tw_summary = next(r for r in summary["retailers"] if r["slug"] == "tackle_warehouse")
    assert tw_summary["pages"][0]["outcome"] == "BLOCKED"
    assert tw_summary["pages"][0]["block_signature"] == "edgesuite"


def test_main_tick_exits_zero(monkeypatch, capsys):
    import pathlib

    fixtures = pathlib.Path(__file__).parent / "fixtures" / "tackle_warehouse"
    clearance_body = (fixtures / "clearance_rods.html").read_bytes()
    stub = _StubFetcher({"CLEARRODSPP": clearance_body, "USED.html": clearance_body})
    monkeypatch.setattr(cli_module, "HttpFetcher", lambda use_stealth=False: stub)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # main() exposes no CLI flag for resolver/sleep_fn (they are offline/
    # testing-only knobs, not production configuration) -- patch the real
    # DNS resolver and time.sleep directly so this end-to-end path stays
    # fast and network-free too.
    monkeypatch.setattr(url_safety_module, "default_resolver", _fake_resolver)
    monkeypatch.setattr(cli_module.time, "sleep", lambda s: None)

    exit_code = cli_module.main(["tick", "--max-discovery-pages", "1"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "tackle_warehouse" in captured.out
