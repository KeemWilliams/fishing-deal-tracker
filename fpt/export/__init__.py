"""Feed export: builds the public deals feed (meta.json, deals.json,
products/<slug>.json) from Postgres and publishes it to a storage backend,
per architecture doc section 3.4.

Entry point: `fpt.export.runner.run_export()`, wired to `fpt export` in
fpt/cli.py.
"""

from __future__ import annotations

from fpt.export.runner import ExportOutcome, run_export

__all__ = ["ExportOutcome", "run_export"]
