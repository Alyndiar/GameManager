from __future__ import annotations

import importlib

from gamemanager.services.storefronts.base import StoreConnector, StorePlugin
from gamemanager.services.storefronts.priority import normalize_store_name
_PLUGIN_MODULES = (
    "gamemanager.services.storefronts.steam_connector",
    "gamemanager.services.storefronts.epic_connector",
    "gamemanager.services.storefronts.gog_connector",
    "gamemanager.services.storefronts.itch_connector",
    "gamemanager.services.storefronts.humble_connector",
    "gamemanager.services.storefronts.ubisoft_connector",
    "gamemanager.services.storefronts.battlenet_connector",
    "gamemanager.services.storefronts.amazon_connector",
)


def _load_plugins() -> dict[str, StorePlugin]:
    plugins: dict[str, StorePlugin] = {}
    for module_name in _PLUGIN_MODULES:
        module = importlib.import_module(module_name)
        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            continue
        if not isinstance(plugin, StorePlugin):
            raise TypeError(
                f"{module_name}.PLUGIN must be a StorePlugin, got {type(plugin).__name__}."
            )
        canonical = normalize_store_name(plugin.store_name)
        if not canonical:
            continue
        if canonical in plugins:
            raise ValueError(
                f"Duplicate storefront plugin for {canonical}: {module_name}"
            )
        plugins[canonical] = plugin
    return plugins


_PLUGINS_BY_STORE: dict[str, StorePlugin] = _load_plugins()


def connector_for_store(store_name: str) -> StoreConnector | None:
    canonical = normalize_store_name(store_name)
    plugin = _PLUGINS_BY_STORE.get(canonical)
    if plugin is None:
        return None
    return plugin.create_connector()


def plugin_for_store(store_name: str) -> StorePlugin | None:
    canonical = normalize_store_name(store_name)
    if not canonical:
        return None
    return _PLUGINS_BY_STORE.get(canonical)


def store_plugins() -> list[StorePlugin]:
    return list(_PLUGINS_BY_STORE.values())


def available_store_names() -> list[str]:
    return sorted(_PLUGINS_BY_STORE.keys())
