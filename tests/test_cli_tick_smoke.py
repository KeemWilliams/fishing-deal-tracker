"""Smoke test for `fpt tick`: exercises the full discover -> fetch ->
block-check -> parse -> validate pipeline end to end using a monkeypatched
fetcher (no real network calls), against the real Tackle Warehouse
adapter and registry.
"""

from datetime import datetime, timezone

import fpt.cli as cli_module
from fpt.core.models import FetchResponse


class _StubFetcher:
    """Serves fixture bytes keyed by URL substring instead of hitting the
    network, so this test never makes an HTTP request."""

    def __init__(self, body_by_url_substring: dict[str, bytes]):
        self._bodies = body_by_url_substring

    def fetch(self, request, egress, user_agent, timeout_s):
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
            snapshot_ref="",
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

    summary = cli_module.run_tick(max_discovery_pages=1)

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

    summary = cli_module.run_tick(max_discovery_pages=1)
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

    exit_code = cli_module.main(["tick", "--max-discovery-pages", "1"])
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "tackle_warehouse" in captured.out
