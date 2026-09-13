"""Berkley (berkley-fishing.com) adapter.

Pure Fishing, Inc. brand -- see `fpt/adapters/abugarcia.py` and
`fpt/adapters/_shopify_collection.py` for the shared family research and
implementation.

CAUTION (per scout research, knowledge/research/fishing-manufacturer-
sites-2026-09-13.md): most of Berkley's sale-collection SKUs are low-price
bait/lures ($3-$16). Some of those may carry an unchanged everyday-low
price rather than a real markdown. The shared adapter's
`compare_at_price > price` guard (in `_shopify_collection.py`) already
protects against surfacing a `compare_at_price` that is equal to (not
strictly greater than) `price` as a claimed discount -- verified against a
live 2026-09-13 sample of ~90 variants across the first page of this
brand's `/collections/sale` collection, all of which showed a real
strikethrough discount (`compare_at_price > price`); no live example of
the equal-price case was observed in that sample, so the "not a real
discount" case for this brand is exercised in this adapter's tests via a
synthesized (not live-fetched) fixture variant, documented as such at the
point it's used.

Sale collection handle is `sale` (verified live 2026-09-13, robots.txt
allows `User-agent: *` on this host, no bot-specific disallow).

Canonical host is `www.berkley-fishing.com`: a bare `berkley-fishing.com`
request 301-redirects there -- `config/retailers.yaml` `allowed_hosts`
matches.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "berkley"
SELLER_NAME = "Berkley"
BASE_URL = "https://www.berkley-fishing.com"
SALE_COLLECTION_HANDLE = "sale"


class BerkleyAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
