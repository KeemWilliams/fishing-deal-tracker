"""Ugly Stik (uglystik.com) adapter.

Pure Fishing, Inc. brand -- see `fpt/adapters/abugarcia.py` and
`fpt/adapters/_shopify_collection.py` for the shared family research and
implementation. Lowest clearance-item volume of the five Pure Fishing
brands (~4 items observed 2026-09-13).

Sale collection handle is `sale` (verified live 2026-09-13, robots.txt
allows `User-agent: *` on this host, no bot-specific disallow).

Canonical host is `www.uglystik.com`: a bare `uglystik.com` request
301-redirects there -- `config/retailers.yaml` `allowed_hosts` matches.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "uglystik"
SELLER_NAME = "Ugly Stik"
BASE_URL = "https://www.uglystik.com"
SALE_COLLECTION_HANDLE = "sale"


class UglyStikAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
