"""G. Loomis (fishshop.shimano.com) adapter.

One of four brand adapters sharing the Shimano North America Fishing
storefront -- see `fpt/adapters/shimano.py` for the full family
docstring/modeling rationale (one retailer per brand, shared
`base_url`/`allowed_hosts`).

G. Loomis's own domain (gloomis.com) returns a Shopify "password" splash
page ("temporarily offline while we make some improvements") as of
2026-09-13; product sales have moved into this shared storefront.

Sale collection handle is `last-cast-savings-g-loomis` (verified live
2026-09-13). High-value brand -- premium fly/spey rods commonly
$300-$1,700+.
"""

from __future__ import annotations

from fpt.adapters._shopify_collection import ShopifyCollectionAdapter

SELLER_KEY = "gloomis"
SELLER_NAME = "G. Loomis"
BASE_URL = "https://fishshop.shimano.com"
SALE_COLLECTION_HANDLE = "last-cast-savings-g-loomis"


class GLoomisAdapter(ShopifyCollectionAdapter):
    def __init__(self) -> None:
        super().__init__(
            slug=SELLER_KEY,
            name=SELLER_NAME,
            base_url=BASE_URL,
            sale_collection_handle=SALE_COLLECTION_HANDLE,
        )
