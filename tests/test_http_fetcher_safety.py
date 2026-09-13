"""Unit tests for the robustness/security fixes in fpt/fetch/http_fetcher.py
(security review M3 body cap, M5 snapshot-failure handling, M8 redaction).
`_do_fetch` is monkeypatched so nothing here touches the network or scrapling.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fpt.core.models import FetchRequest, PageType
from fpt.fetch.fetcher import EgressConfig, FetchTransportError
from fpt.fetch.http_fetcher import MAX_BODY_BYTES, HttpFetcher


def _request(url="https://www.tacklewarehouse.com/x") -> FetchRequest:
    return FetchRequest(task_id=1, page_type=PageType.PRODUCT, url=url)


def _scrapling_response(body: bytes, url: str = "https://www.tacklewarehouse.com/x"):
    return SimpleNamespace(body=body, status=200, url=url, headers={}, elapsed_ms=5)


class TestBodySizeCap:
    def test_oversized_body_raises_transport_error(self, monkeypatch):
        fetcher = HttpFetcher()
        oversized = b"x" * (MAX_BODY_BYTES + 1)
        monkeypatch.setattr(fetcher, "_do_fetch", lambda *a, **k: _scrapling_response(oversized))

        with pytest.raises(FetchTransportError) as excinfo:
            fetcher.fetch(_request(), EgressConfig(mode="DIRECT"), "ua", 10.0)
        assert "MAX_BODY_BYTES" in str(excinfo.value)

    def test_body_at_the_cap_is_accepted(self, monkeypatch, tmp_path):
        fetcher = HttpFetcher(snapshot_dir=tmp_path)
        exactly_cap = b"x" * MAX_BODY_BYTES
        monkeypatch.setattr(fetcher, "_do_fetch", lambda *a, **k: _scrapling_response(exactly_cap))

        response = fetcher.fetch(_request(), EgressConfig(mode="DIRECT"), "ua", 10.0)
        assert len(response.body) == MAX_BODY_BYTES
        assert response.snapshot_ref  # never empty


class TestSnapshotFailureHandling:
    def test_snapshot_write_failure_is_a_failed_fetch_not_an_empty_ref(self, monkeypatch, tmp_path):
        """Security review M5: previously this swallowed the exception and
        returned snapshot_ref="" -- now it must raise, so the caller treats
        it exactly like any other transport failure and never stores an
        observation for it."""
        fetcher = HttpFetcher(snapshot_dir=tmp_path)
        monkeypatch.setattr(fetcher, "_do_fetch", lambda *a, **k: _scrapling_response(b"some body"))

        def _boom(*args, **kwargs):
            raise OSError("disk full")

        import fpt.fetch.http_fetcher as http_fetcher_module

        monkeypatch.setattr(http_fetcher_module, "write_snapshot", _boom)

        with pytest.raises(FetchTransportError) as excinfo:
            fetcher.fetch(_request(), EgressConfig(mode="DIRECT"), "ua", 10.0)
        assert "snapshot write failed" in str(excinfo.value)

    def test_two_write_failures_never_produce_the_same_empty_ref(self, monkeypatch, tmp_path):
        """Regression guard for the exact bug: two DIFFERENT fetches that
        both fail to snapshot must not look like the same observation to
        fpt/store/observations.py's dedupe (which keys on snapshot_ref)."""
        fetcher = HttpFetcher(snapshot_dir=tmp_path)
        monkeypatch.setattr(fetcher, "_do_fetch", lambda *a, **k: _scrapling_response(b"body one"))

        import fpt.fetch.http_fetcher as http_fetcher_module

        monkeypatch.setattr(http_fetcher_module, "write_snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))

        with pytest.raises(FetchTransportError):
            fetcher.fetch(_request(), EgressConfig(mode="DIRECT"), "ua", 10.0)
        with pytest.raises(FetchTransportError):
            fetcher.fetch(_request(), EgressConfig(mode="DIRECT"), "ua", 10.0)
        # Both raised -- neither ever reached the caller with a
        # snapshot_ref="" FetchResponse that could dedupe against the other.


class TestRedaction:
    def test_proxy_credential_error_is_redacted(self, monkeypatch):
        fetcher = HttpFetcher()

        def _boom(*a, **k):
            raise RuntimeError("failed to connect via http://user:hunter2@proxy.internal:8080")

        monkeypatch.setattr(fetcher, "_do_fetch", _boom)

        with pytest.raises(FetchTransportError) as excinfo:
            fetcher.fetch(_request(), EgressConfig(mode="DIRECT"), "ua", 10.0)
        assert "hunter2" not in str(excinfo.value)
        assert "REDACTED" in str(excinfo.value)
