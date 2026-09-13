#!/usr/bin/env python3
"""Idempotent migration runner for db/migrations/*.up.sql.

    python deploy/migrate.py              # apply every pending migration
    python deploy/migrate.py --dry-run    # list what WOULD run, apply nothing
    python deploy/migrate.py --status     # show applied vs. pending, apply nothing

Design (kept intentionally small — this is not a migration framework):

- Migration identity is the numeric prefix of the filename
  (``001_extensions.up.sql`` -> version ``"001"``), not the whole filename,
  so a migration can be renamed for clarity without being re-applied.
- A ``schema_migrations`` tracking table (created on first run if missing)
  records which versions have been applied. Re-running this script applies
  only what is pending -- running it twice with nothing new is a no-op.
- Each migration file runs inside its own transaction: the SQL file's
  statements plus the bookkeeping INSERT into ``schema_migrations`` commit
  together, or neither does. A failure stops the run before touching any
  later migration; already-committed earlier migrations stay applied (that
  is the correct, expected partial-progress state -- re-running the script
  after fixing the failing migration resumes from there).
- Only ``*.up.sql`` files are applied. ``*.down.sql`` files exist in the
  repo for manual/emergency rollback and are never run by this script.
- DATABASE_URL is read from the environment and is NEVER written to
  stdout/stderr, logs, or exceptions raised by this module. On connection
  failure, psycopg's own exception text is what surfaces; it does not
  include the DSN in a way this script echoes back.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover - exercised only if requirements.txt drifts
    psycopg = None  # type: ignore[assignment]

DATABASE_URL_ENV_VAR = "DATABASE_URL"
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

_VERSION_RE = re.compile(r"^(\d+)_")


class MigrationError(Exception):
    pass


def discover_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    """Returns [(version, path), ...] for every *.up.sql file, sorted
    numerically by version. Raises MigrationError on a duplicate version
    (two files sharing the same numeric prefix) or a filename that doesn't
    start with digits followed by an underscore."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"migrations directory not found: {migrations_dir}")

    found: dict[str, Path] = {}
    for path in sorted(migrations_dir.glob("*.up.sql")):
        match = _VERSION_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"migration filename does not start with <digits>_: {path.name}"
            )
        version = match.group(1)
        if version in found:
            raise MigrationError(
                f"duplicate migration version {version!r}: "
                f"{found[version].name} and {path.name}"
            )
        found[version] = path

    return sorted(found.items(), key=lambda item: int(item[0]))


def ensure_tracking_table(conn: "psycopg.Connection") -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     text PRIMARY KEY,
                filename    text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def applied_versions(conn: "psycopg.Connection") -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_migration(conn: "psycopg.Connection", version: str, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s)",
                (version, path.name),
            )


def run(
    *,
    migrations_dir: Path,
    database_url: str | None,
    dry_run: bool,
    status_only: bool,
) -> int:
    migrations = discover_migrations(migrations_dir)

    if not migrations:
        print(f"No migrations found in {migrations_dir}")
        return 0

    if not database_url:
        print(
            f"{DATABASE_URL_ENV_VAR} is not set -- cannot connect to inspect "
            "applied migrations.",
            file=sys.stderr,
        )
        if dry_run:
            print("(--dry-run) migrations that exist on disk, in apply order:")
            for version, path in migrations:
                print(f"  {version}  {path.name}")
            return 0
        return 1

    if psycopg is None:  # pragma: no cover - exercised only if requirements.txt drifts
        print("psycopg is not installed", file=sys.stderr)
        return 1

    try:
        with psycopg.connect(database_url) as conn:
            ensure_tracking_table(conn)
            done = applied_versions(conn)
            pending = [(v, p) for v, p in migrations if v not in done]

            if status_only or dry_run:
                for version, path in migrations:
                    marker = "applied" if version in done else "pending"
                    print(f"  {version}  {path.name}  [{marker}]")
                if dry_run and pending:
                    print(f"\n(--dry-run) {len(pending)} migration(s) would be applied.")
                return 0

            if not pending:
                print("Database is up to date -- nothing to apply.")
                return 0

            for version, path in pending:
                print(f"Applying {version}  {path.name} ...")
                apply_migration(conn, version, path)
                print(f"  ok")

            print(f"Applied {len(pending)} migration(s).")
            return 0
    except MigrationError as exc:
        print(f"Migration error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - deliberately broad; see module docstring
        # str(exc) here is psycopg/postgres's own error text, not something
        # this script constructs from the DSN -- DATABASE_URL itself is
        # never interpolated into any message this script prints.
        print(f"Migration run failed: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Apply pending db/migrations/*.up.sql files in numeric order, "
            "tracked in a schema_migrations table. Safe to run repeatedly: "
            "already-applied migrations are skipped."
        )
    )
    parser.add_argument(
        "--migrations-dir",
        type=Path,
        default=DEFAULT_MIGRATIONS_DIR,
        help=f"Directory containing *.up.sql files (default: {DEFAULT_MIGRATIONS_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which migrations are applied/pending without applying anything.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        dest="status_only",
        help="Alias for --dry-run (kept for readability in ops runbooks).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    database_url = os.environ.get(DATABASE_URL_ENV_VAR)
    return run(
        migrations_dir=args.migrations_dir,
        database_url=database_url,
        dry_run=args.dry_run,
        status_only=args.status_only,
    )


if __name__ == "__main__":
    sys.exit(main())
