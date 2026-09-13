"""Re-exports the adapter contract from `fpt.adapters.base`.

`fpt/adapters/base.py` is the canonical, first-committed interface file
other adapter authors build against (per architecture doc 3.1's literal
path). This module exists so internal pipeline/deal/core code can import
domain types via `fpt.core.models` without creating an import cycle
through the adapters package -- both names point at the exact same
classes.
"""

from __future__ import annotations

from fpt.adapters.base import (
    CONDITION_GROUP,
    AdapterCapabilities,
    Availability,
    ClaimedReferenceKind,
    Condition,
    CrawlTask,
    DiscoveredItem,
    FetchRequest,
    FetchResponse,
    PageType,
    ParsedListing,
    ParsedOffer,
    ParseResult,
    ResponseOutcome,
    RetailerAdapter,
    SellerType,
)

__all__ = [
    "CONDITION_GROUP",
    "AdapterCapabilities",
    "Availability",
    "ClaimedReferenceKind",
    "Condition",
    "CrawlTask",
    "DiscoveredItem",
    "FetchRequest",
    "FetchResponse",
    "PageType",
    "ParsedListing",
    "ParsedOffer",
    "ParseResult",
    "ResponseOutcome",
    "RetailerAdapter",
    "SellerType",
]
