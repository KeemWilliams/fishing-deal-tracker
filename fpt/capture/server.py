"""Minimal ingest HTTP endpoint for the browser-capture extension.

stdlib-only (`http.server`) -- the project's requirements.txt does not
carry a web framework (no FastAPI/Flask), and the architecture doc's `fpt
api` component (section 3.5) is not built yet, so this is a small,
self-contained app under `fpt/capture/` per the task brief, not an
addition to a nonexistent `fpt api` process. If/when `fpt api` is built,
`POST /v1/captures` can be lifted into it wholesale -- all the actual
logic (validation, sanitization, persistence) lives in
`fpt/capture/validate.py` and `fpt/store/captures.py`, not here; this
module is only transport plumbing.

Security posture (mandatory gate: this is a new public-ish endpoint --
see pact-security-patterns and the SACROSANCT rules):
  - Body size is capped BEFORE reading (Content-Length check), independent
    of `MAX_BODY_BYTES` re-checked after the read.
  - JSON parsing failures never leak parser internals to the caller.
  - An optional shared-secret token (`FPT_CAPTURE_TOKEN` env var) gates
    writes when set; never hardcoded, never logged. When unset, the
    server is open -- acceptable ONLY because this is meant to run on
    localhost/LAN for personal use; `run_server()` binds to 127.0.0.1 by
    default and documents in its docstring that exposing this beyond
    localhost without setting the token is a misconfiguration.
  - CORS is intentionally NOT enabled for arbitrary page-context fetches:
    the extension's background service worker (not its content script)
    is the one making this request, so no `Access-Control-Allow-Origin`
    is needed for the intended client. If a page's own script tries to
    reach this endpoint directly, the browser's default same-origin
    policy plus the missing CORS headers block it from reading the
    response (though not from sending the request -- token auth is the
    real defense against a hostile page abusing this endpoint from a
    victim's LAN, not CORS).
  - Every field is validated/sanitized in `fpt/capture/validate.py`
    before it ever reaches SQL (parameterized queries only, see
    `fpt/store/captures.py`) or gets echoed back in a response.
"""

from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from fpt.capture.ratelimit import RateLimiter
from fpt.capture.validate import MAX_BODY_BYTES, CaptureValidationError, validate_capture_payload
from fpt.db import DatabaseUnavailable, connect
from fpt.store.captures import insert_capture

logger = logging.getLogger("fpt.capture.server")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
CAPTURE_PATH = "/v1/captures"
HEALTH_PATH = "/healthz"

TOKEN_ENV_VAR = "FPT_CAPTURE_TOKEN"

# Rate limit: blunt a malfunctioning extension (e.g. an infinite retry loop), not a real
# multi-tenant abuse defense -- see ratelimit.py docstring.
_rate_limiter = RateLimiter(max_requests=30, window_s=60.0)


def _error_body(code: str, message: str) -> bytes:
    return json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")


def _expected_token() -> str | None:
    token = os.environ.get(TOKEN_ENV_VAR)
    return token if token else None


class CaptureRequestHandler(BaseHTTPRequestHandler):
    server_version = "fpt-capture/0.1"

    # Silence the default noisy per-request stderr logging; use the logging module instead so
    # this behaves consistently whether run standalone or embedded in a larger process.
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        logger.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        body = _error_body(code, message)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = _expected_token()
        if expected is None:
            return True
        auth_header = self.headers.get("Authorization", "")
        return auth_header == f"Bearer {expected}"

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        if self.path == HEALTH_PATH:
            self._send_json(200, {"status": "ok"})
            return
        self._send_error_json(404, "NOT_FOUND", "unknown path")

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        if self.path != CAPTURE_PATH:
            self._send_error_json(404, "NOT_FOUND", "unknown path")
            return

        if not self._authorized():
            self._send_error_json(401, "UNAUTHORIZED", "missing or invalid bearer token")
            return

        client_ip = self.client_address[0] if self.client_address else "unknown"
        if not _rate_limiter.allow(client_ip):
            self._send_error_json(429, "RATE_LIMITED", "too many capture requests, slow down")
            return

        content_length_header = self.headers.get("Content-Length")
        if content_length_header is None:
            self._send_error_json(400, "VALIDATION_ERROR", "Content-Length header is required")
            return
        try:
            content_length = int(content_length_header)
        except ValueError:
            self._send_error_json(400, "VALIDATION_ERROR", "Content-Length is not a valid integer")
            return
        if content_length < 0 or content_length > MAX_BODY_BYTES:
            self._send_error_json(413, "PAYLOAD_TOO_LARGE", f"body must be at most {MAX_BODY_BYTES} bytes")
            return

        raw_body = self.rfile.read(content_length)
        if len(raw_body) > MAX_BODY_BYTES:
            self._send_error_json(413, "PAYLOAD_TOO_LARGE", f"body must be at most {MAX_BODY_BYTES} bytes")
            return

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error_json(400, "VALIDATION_ERROR", "body must be valid UTF-8 JSON")
            return

        try:
            capture = validate_capture_payload(data)
        except CaptureValidationError as exc:
            self._send_error_json(400, exc.code, exc.message)
            return

        try:
            with connect() as conn:
                with conn.cursor() as cur:
                    _, pub_id, created = insert_capture(cur, capture)
        except DatabaseUnavailable as exc:
            logger.error("capture DB write failed: %s", exc)
            self._send_error_json(503, "DATABASE_UNAVAILABLE", "could not persist capture")
            return

        self._send_json(202, {"status": "accepted", "pub_id": pub_id, "created": created})


def run_server(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    handler_cls: Callable[..., BaseHTTPRequestHandler] = CaptureRequestHandler,
) -> None:
    """Blocking call -- run under a process manager / `python -m
    fpt.capture.server`, not imported into the tick/CLI process.

    Binds to 127.0.0.1 by default. If you need LAN access (e.g. the
    browser is on the same machine as the Coolify host but not
    localhost), set host="0.0.0.0" explicitly AND set FPT_CAPTURE_TOKEN --
    running open on a non-loopback interface without a token is a
    misconfiguration, not a supported mode.
    """
    if host != DEFAULT_HOST and not _expected_token():
        logger.warning(
            "fpt-capture server binding to %s without %s set -- this exposes an "
            "unauthenticated write endpoint beyond localhost",
            host,
            TOKEN_ENV_VAR,
        )
    server = ThreadingHTTPServer((host, port), handler_cls)
    logger.info("fpt-capture listening on %s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    env_port = os.environ.get("FPT_CAPTURE_PORT")
    env_host = os.environ.get("FPT_CAPTURE_HOST", DEFAULT_HOST)
    run_server(host=env_host, port=int(env_port) if env_port else DEFAULT_PORT)
