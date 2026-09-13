"""Persistence layer: turns the pure domain objects in fpt/ (ParsedListing,
ParsedOffer, ValidationResult, DealCandidate, ConfirmResult) into rows in
the schema under db/migrations/, using parameterized SQL only (psycopg).

This package is the CRITICAL-1 fix flagged by the test engineer: before it
existed, fpt/cli.py's `_try_db_persist` only checked `price_observations`
existed and never wrote a row anywhere. See `fpt/store/pipeline.py` for the
single entrypoint the tick path calls.
"""

from __future__ import annotations

from fpt.store.pipeline import ingest_parsed_listing, IngestOutcome

__all__ = ["ingest_parsed_listing", "IngestOutcome"]
