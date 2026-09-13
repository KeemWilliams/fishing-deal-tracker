"""Postgres connection helper.

DB access is via the `DATABASE_URL` environment variable ONLY -- no
hardcoded connection strings, no alternate env var names. This module is
intentionally thin: it does not know the schema (that's the database
engineer's migrations, owned separately). Callers that need real
persistence should catch `DatabaseUnavailable` and fall back to a
dry-run/print mode, which is what `fpt.cli tick` does when no tables exist
yet or `DATABASE_URL` is unset.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    import psycopg

DATABASE_URL_ENV_VAR = "DATABASE_URL"


class DatabaseUnavailable(Exception):
    pass


def get_database_url() -> str | None:
    return os.environ.get(DATABASE_URL_ENV_VAR)


@contextmanager
def connect() -> Iterator["psycopg.Connection"]:
    """Yields a psycopg connection. Raises DatabaseUnavailable if
    DATABASE_URL is unset or the connection fails -- callers decide
    whether that's fatal or a reason to fall back to dry-run mode."""
    url = get_database_url()
    if not url:
        raise DatabaseUnavailable(f"{DATABASE_URL_ENV_VAR} is not set")

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - requirements.txt pins this
        raise DatabaseUnavailable("psycopg is not installed") from exc

    try:
        conn = psycopg.connect(url, connect_timeout=10)
    except Exception as exc:  # noqa: BLE001
        raise DatabaseUnavailable(f"could not connect: {exc}") from exc

    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s)",
            (table_name,),
        )
        row = cur.fetchone()
        return bool(row and row[0])
