"""Pflueger (pfluegerfishing.com) adapter.

Pure Fishing, Inc. brand -- see `fpt/adapters/abugarcia.py` and
`fpt/adapters/_shopify_collection.py` for the shared family research and
implementation.

Sale collection handle is `sale` (verified live 2026-09-13, robots.txt
allows `User-agent: *` on this host, no bot-specific disallow).

Canonical host is the bare apex `pfluegerfishing.com` -- unlike the other
four Pure Fishing brands in this family, this one does NOT redirect from
bare apex to `www.`; `robots.txt` and `/collections/...` both serve
directly from `pfluegerfishing.com` (confirmed live 2026-09-13; the `www.`
host 301-redirects instead). `config/retailers.yaml` `allowed_hosts`
matches the bare apex only.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "pflueger"
SELLER_NAME = "Pflueger"
BASE_URL = "https://pfluegerfishing.com"
SALE_COLLECTION_HANDLE = "sale"


class PfluegerAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
