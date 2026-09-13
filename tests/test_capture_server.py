"""Smoke tests for fpt/capture/server.py: compile + run + happy path, plus
the security-relevant rejections (oversized body, bad token, bad JSON).
No real DB -- fpt.db.connect is monkeypatched to a fake in-memory cursor
so these run everywhere `python -m pytest` runs, no DATABASE_URL needed.
"""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager

import pytest

import fpt.capture.server as server_module
from fpt.capture.ratelimit import RateLimiter


class _FakeCursor:
    def __init__(self):
        self._last_row = None

    def execute(self, sql, params=None):
        if sql.strip().startswith("INSERT INTO page_captures"):
            self._last_row = (1, "cap_fake0000000000000000000000")

    def fetchone(self):
        return self._last_row

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@contextmanager
def _fake_connect():
    yield _FakeConn()


@pytest.fixture(autouse=True)
def _reset_rate_limiter(monkeypatch):
    # Each test gets a fresh limiter so tests don't interfere with each other's rate limits.
    monkeypatch.setattr(server_module, "_rate_limiter", RateLimiter(max_requests=30, window_s=60.0))
    yield


@pytest.fixture()
def running_server(monkeypatch):
    monkeypatch.setattr(server_module, "connect", _fake_connect)
    monkeypatch.delenv(server_module.TOKEN_ENV_VAR, raising=False)

    httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.CaptureRequestHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(port: int, body: bytes, headers: dict | None = None) -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", "/v1/captures", body=body, headers=headers or {"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        return resp.status, payload
    finally:
        conn.close()


def test_healthz(running_server):
    conn = http.client.HTTPConnection("127.0.0.1", running_server, timeout=5)
    conn.request("GET", "/healthz")
    resp = conn.getresponse()
    assert resp.status == 200
    assert json.loads(resp.read().decode("utf-8")) == {"status": "ok"}
    conn.close()


def test_happy_path_capture_accepted(running_server):
    body = json.dumps(
        {
            "product_url": "https://www.scheels.com/p/some-rod/1.html",
            "title_raw": "Some Rod",
            "current_price_cents": 4999,
        }
    ).encode("utf-8")
    status, payload = _post(running_server, body)
    assert status == 202
    assert payload["status"] == "accepted"
    assert payload["pub_id"].startswith("cap_")


def test_unknown_path_404(running_server):
    conn = http.client.HTTPConnection("127.0.0.1", running_server, timeout=5)
    conn.request("POST", "/v1/nonsense", body=b"{}", headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    assert resp.status == 404
    conn.close()


def test_invalid_json_rejected(running_server):
    status, payload = _post(running_server, b"{not json")
    assert status == 400
    assert payload["error"]["code"] == "VALIDATION_ERROR"


def test_javascript_scheme_url_rejected(running_server):
    body = json.dumps(
        {"product_url": "javascript:alert(1)", "title_raw": "x", "current_price_cents": 100}
    ).encode("utf-8")
    status, payload = _post(running_server, body)
    assert status == 400
    assert payload["error"]["code"] == "UNSAFE_URL"


def test_missing_content_length_rejected(running_server):
    # http.client always sets Content-Length for a bytes body, so simulate the missing-header
    # case with a raw socket instead of http.client's request/response state machine.
    import socket

    sock = socket.create_connection(("127.0.0.1", running_server), timeout=5)
    try:
        sock.sendall(b"POST /v1/captures HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        response = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
        status_line = response.split(b"\r\n", 1)[0].decode("ascii")
        assert " 400 " in status_line
    finally:
        sock.close()


def test_oversized_body_rejected(running_server):
    huge_url = "https://www.scheels.com/" + ("a" * (server_module.MAX_BODY_BYTES + 1000))
    body = json.dumps({"product_url": huge_url, "title_raw": "x", "current_price_cents": 100}).encode("utf-8")
    status, payload = _post(running_server, body)
    assert status == 413
    assert payload["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_bearer_token_enforced_when_configured(monkeypatch):
    monkeypatch.setattr(server_module, "connect", _fake_connect)
    monkeypatch.setattr(server_module, "_rate_limiter", RateLimiter(max_requests=30, window_s=60.0))
    monkeypatch.setenv(server_module.TOKEN_ENV_VAR, "dummy-auth-for-tests")

    httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.CaptureRequestHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(
            {"product_url": "https://www.scheels.com/p/x.html", "title_raw": "x", "current_price_cents": 100}
        ).encode("utf-8")

        # No token -> rejected
        status, payload = _post(port, body)
        assert status == 401

        # Correct token -> accepted
        status, payload = _post(
            port, body, headers={"Content-Type": "application/json", "Authorization": "Bearer dummy-auth-for-tests"}
        )
        assert status == 202
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_rate_limit_enforced(monkeypatch):
    monkeypatch.setattr(server_module, "connect", _fake_connect)
    monkeypatch.setattr(server_module, "_rate_limiter", RateLimiter(max_requests=2, window_s=60.0))
    monkeypatch.delenv(server_module.TOKEN_ENV_VAR, raising=False)

    httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module.CaptureRequestHandler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        body = json.dumps(
            {"product_url": "https://www.scheels.com/p/x.html", "title_raw": "x", "current_price_cents": 100}
        ).encode("utf-8")
        statuses = [_post(port, body)[0] for _ in range(3)]
        assert statuses == [202, 202, 429]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
