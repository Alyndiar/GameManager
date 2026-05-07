from __future__ import annotations

from pathlib import Path

from gamemanager.app_state import AppState
from gamemanager.models import StoreOwnedGame
from gamemanager.services.storefronts.store_urls import store_game_url


def test_store_game_url_prefers_steam_appid() -> None:
    assert (
        store_game_url("Steam", store_game_id="620", title="Portal 2")
        == "https://store.steampowered.com/app/620/"
    )


def test_store_game_url_falls_back_to_store_search() -> None:
    url = store_game_url("EGS", store_game_id="", title="Control")
    assert "store.epicgames.com" in url
    assert "Control" in url


def test_store_game_url_egs_internal_id_prefers_title_search() -> None:
    url = store_game_url(
        "EGS",
        store_game_id="281504e8e6e44b5dab3a072f9ee21c21",
        title="Control",
    )
    assert "q=Control" in url
    assert "281504e8e6e44b5dab3a072f9ee21c21" not in url


def test_store_game_url_egs_slug_uses_direct_product_page() -> None:
    assert (
        store_game_url("EGS", store_game_id="p/control", title="")
        == "https://store.epicgames.com/en-US/p/control"
    )


def test_store_game_url_gog_slug_uses_direct_product_page() -> None:
    assert (
        store_game_url("GOG", store_game_id="portal_2", title="")
        == "https://www.gog.com/en/game/portal_2"
    )


def test_store_game_url_itch_direct_url_is_preserved() -> None:
    assert (
        store_game_url(
            "Itch.io",
            store_game_id="https://foo.itch.io/portal-2",
            title="",
        )
        == "https://foo.itch.io/portal-2"
    )


def test_store_game_url_itch_domain_path_is_normalized_to_https() -> None:
    assert (
        store_game_url(
            "Itch.io",
            store_game_id="foo.itch.io/portal-2",
            title="",
        )
        == "https://foo.itch.io/portal-2"
    )


def test_app_state_store_page_url_uses_linked_entitlement(tmp_path: Path) -> None:
    app = AppState(tmp_path / "db.sqlite3")
    inventory_dir = tmp_path / "Portal 2"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    app.db.upsert_store_account(
        "Steam",
        "76561198000000000",
        "test",
        "steam_openid_api_key",
    )
    app.db.replace_store_owned_games_for_account(
        "Steam",
        "76561198000000000",
        [
            StoreOwnedGame(
                store_name="Steam",
                account_id="76561198000000000",
                entitlement_id="620",
                title="Portal 2",
                store_game_id="620",
                manifest_id="620",
            )
        ],
    )
    app.db.upsert_store_link(
        inventory_path=str(inventory_dir),
        store_name="Steam",
        account_id="76561198000000000",
        entitlement_id="620",
        match_method="strong_id",
        confidence=1.0,
        verified=True,
    )
    assert (
        app.store_page_url_for_inventory(
            str(inventory_dir),
            store_name="Steam",
            game_title="Portal 2",
        )
        == "https://store.steampowered.com/app/620/"
    )
