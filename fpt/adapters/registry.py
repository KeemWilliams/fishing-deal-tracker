"""Adapter registry.

Per architecture doc 3.1: "Unregistered `adapter_slug` on an enabled
retailer makes `fpt tick` exit non-zero (fail loud)." This module is the
single place adapters are wired in; adding a retailer means adding one
entry here (plus its config block) per the "Adding a retailer" note in
the architecture doc's repo-layout section.
"""

from __future__ import annotations

from fpt.adapters.base import RetailerAdapter


class UnknownAdapterError(Exception):
    pass


_REGISTRY: dict[str, RetailerAdapter] = {}


def register(adapter: RetailerAdapter) -> None:
    _REGISTRY[adapter.slug] = adapter


def get_adapter(slug: str) -> RetailerAdapter:
    try:
        return _REGISTRY[slug]
    except KeyError as exc:
        raise UnknownAdapterError(f"no adapter registered for slug={slug!r}") from exc


def registered_slugs() -> list[str]:
    return sorted(_REGISTRY.keys())


def _register_builtin_adapters() -> None:
    # Imported lazily to avoid import cost/cycles for callers that only
    # need the registry API surface (e.g. tests exercising a single
    # adapter directly without loading every retailer's dependencies).
    from fpt.adapters.abugarcia import AbuGarciaAdapter
    from fpt.adapters.academy import AcademyAdapter
    from fpt.adapters.alltackle import AlltackleAdapter
    from fpt.adapters.basspro import BassProAdapter
    from fpt.adapters.berkley import BerkleyAdapter
    from fpt.adapters.cabelas import CabelasAdapter
    from fpt.adapters.dicks import DicksAdapter
    from fpt.adapters.discounttackle import DiscountTackleAdapter
    from fpt.adapters.fishingonline import FishingOnlineAdapter
    from fpt.adapters.fishusa import FishUSAAdapter
    from fpt.adapters.gloomis import GLoomisAdapter
    from fpt.adapters.jackall import JackallAdapter
    from fpt.adapters.jandh import JandhAdapter
    from fpt.adapters.penn import PennAdapter
    from fpt.adapters.pflueger import PfluegerAdapter
    from fpt.adapters.powerpro import PowerProAdapter
    from fpt.adapters.rodlocker import RodLockerAdapter
    from fpt.adapters.shimano import ShimanoAdapter
    from fpt.adapters.sportsmans_guide import SportsmansGuideAdapter
    from fpt.adapters.sportsmans_warehouse import SportsmansWarehouseAdapter
    from fpt.adapters.tackle_warehouse import TackleWarehouseAdapter
    from fpt.adapters.tackledirect import TackleDirectAdapter
    from fpt.adapters.uglystik import UglyStikAdapter

    register(TackleWarehouseAdapter())
    # Academy and J&H were committed earlier but were never wired into this
    # registry, so `fpt tick` silently never ran them even though the
    # database seed (012) and config/retailers.yaml marked them enabled --
    # found and fixed while adding the three retailers below (2026-09-12).
    register(AcademyAdapter())
    register(JandhAdapter())
    register(FishUSAAdapter())
    register(TackleDirectAdapter())
    register(AlltackleAdapter())
    # Fishing Online, Discount Tackle, Rod Locker -- added in this task
    # (2026-09-12), all Shopify storefronts read via
    # fpt/adapters/_shopify_collection.py's shared public-JSON-endpoint
    # adapter. See knowledge/research/fishing-additional-retailers-
    # 2026-09-12.md for the retailer research this was built on.
    register(FishingOnlineAdapter())
    register(DiscountTackleAdapter())
    register(RodLockerAdapter())
    # Bass Pro Shops -- added in this task (2026-09-13). Coveo search-JSON
    # parser only; the Akamai-aware real-browser fetch mechanism for this
    # retailer is a separate, out-of-scope concern (see
    # fpt/adapters/basspro.py module docstring).
    register(BassProAdapter())
    # Cabela's -- added in this task (2026-09-13). Same Coveo search-JSON
    # backend/org as Bass Pro Shops; parses via the shared
    # fpt/adapters/_coveo.py helper. Dick's Sporting Goods -- added in this
    # task, a different `prod-catalog-product-api` v2/search JSON shape
    # with its own MAP-restriction handling (see fpt/adapters/dicks.py
    # module docstring). Both are parser-only, same fetch-mechanism scope
    # boundary as Bass Pro.
    register(CabelasAdapter())
    register(DicksAdapter())
    # Manufacturer/brand clearance storefronts -- added in this task
    # (2026-09-13). Two Shopify-platform families, both read via the same
    # shared fpt/adapters/_shopify_collection.py adapter as fishingonline/
    # discounttackle/rodlocker above: Pure Fishing, Inc. (Abu Garcia, Penn,
    # Pflueger, Ugly Stik, Berkley) each on their own domain, and Shimano
    # North America Fishing (Shimano, G. Loomis, PowerPro, Jackall Lures)
    # sharing one storefront domain (fishshop.shimano.com) via four
    # distinct sale-collection handles. See knowledge/research/fishing-
    # manufacturer-sites-2026-09-13.md and each adapter's module docstring.
    register(AbuGarciaAdapter())
    register(PennAdapter())
    register(PfluegerAdapter())
    register(UglyStikAdapter())
    register(BerkleyAdapter())
    register(ShimanoAdapter())
    register(GLoomisAdapter())
    register(PowerProAdapter())
    register(JackallAdapter())
    # Sportsman's Warehouse (SAP Hybris) and Sportsman's Guide (legacy
    # ATG-style storefront) -- added in this task (2026-09-13). Both are
    # plain server-rendered HTML, parsed with scrapling's Selector
    # (matching tackle_warehouse.py's approach) rather than a JSON
    # endpoint. See knowledge/research/fishing-big-outdoor-retailers-
    # 2026-09-13.md and each adapter's module docstring.
    register(SportsmansWarehouseAdapter())
    register(SportsmansGuideAdapter())


_register_builtin_adapters()
