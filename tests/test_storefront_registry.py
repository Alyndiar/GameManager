from __future__ import annotations

from gamemanager.services.storefronts.base import StorePlugin
from gamemanager.services.storefronts.registry import (
    available_store_names,
    connector_for_store,
    plugin_for_store,
    store_plugins,
)


def test_registry_exposes_plugins_per_store() -> None:
    names = available_store_names()
    assert "Steam" in names
    assert "EGS" in names
    assert "Itch.io" in names

    plugin = plugin_for_store("steam")
    assert plugin is not None
    assert isinstance(plugin, StorePlugin)
    assert plugin.store_name == "Steam"


def test_each_registered_store_resolves_connector() -> None:
    plugins = store_plugins()
    assert plugins
    for plugin in plugins:
        connector = connector_for_store(plugin.store_name)
        assert connector is not None
        assert connector.store_name == plugin.store_name
