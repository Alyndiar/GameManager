from gamemanager.services.storefronts.priority import (
    normalize_store_name,
    primary_store,
    sort_stores,
)


def test_sort_stores_uses_priority_order() -> None:
    stores = ["ubisoft connect", "gog", "steam", "itch", "egs"]
    assert sort_stores(stores) == ["Steam", "EGS", "GOG", "Itch.io", "Ubisoft"]


def test_primary_store_returns_highest_priority() -> None:
    assert primary_store(["GOG", "Steam", "Humble"]) == "Steam"
    assert primary_store([]) is None


def test_normalize_store_name_maps_aliases() -> None:
    assert normalize_store_name("epic games store") == "EGS"
    assert normalize_store_name("BattleNet") == "Battle.net"
