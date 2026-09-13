"""Fixtures for DB integration tests.

These tests require a real Postgres reachable via DATABASE_URL, with all
db/migrations/*.up.sql applied (see test-engineer HANDOFF for the disposable
Docker command used to build the instance these were verified against).

Every test runs inside a transaction that is ROLLED BACK in teardown, so the
throwaway database is left at zero added rows after each test and after the
whole run -- no test here ever commits.

Collection-skips (not failures) when DATABASE_URL is unset or unreachable,
so `pytest tests/` from a machine without Docker still passes the other 143
tests untouched.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    collect_ignore_glob = ["*"]


@pytest.fixture()
def db(request):
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; integration tests require a live Postgres")
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=5, autocommit=False)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"could not connect to DATABASE_URL: {exc}")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
