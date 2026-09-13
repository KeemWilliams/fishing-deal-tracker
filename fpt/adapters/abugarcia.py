"""Abu Garcia (abugarcia.com) adapter.

Pure Fishing, Inc. brand -- Abu Garcia, Penn, Berkley, Pflueger, and Ugly
Stik are all Pure Fishing brands on identical Shopify storefronts (same
`agents.md`/robots.txt boilerplate, same theme, same `/collections/sale`
URL pattern). See `fpt/adapters/_shopify_collection.py` for the shared
implementation and knowledge/research/fishing-manufacturer-sites-
2026-09-13.md for the family-wide research this was built on.

Sale collection handle is `sale` (verified live 2026-09-13,
robots.txt-allowed for `User-agent: *`, no ClaudeBot/Claude-User specific
disallow rule on this host).

Canonical host is `www.abugarcia.com`: a bare `abugarcia.com` request
(both `/robots.txt` and `/collections/...`) 301-redirects there --
`config/retailers.yaml` `allowed_hosts` for this retailer matches.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "abugarcia"
SELLER_NAME = "Abu Garcia"
BASE_URL = "https://www.abugarcia.com"
SALE_COLLECTION_HANDLE = "sale"


class AbuGarciaAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
