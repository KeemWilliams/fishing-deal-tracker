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
    from fpt.adapters.tackle_warehouse import TackleWarehouseAdapter

    register(TackleWarehouseAdapter())


_register_builtin_adapters()
