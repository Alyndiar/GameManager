from __future__ import annotations

from gamemanager.services.storefronts.base import StoreEntitlement
from gamemanager.services.storefronts.steam_connector import SteamConnector


def test_steam_connect_requires_api_key() -> None:
    connector = SteamConnector()
    failed = connector.connect({"account_id": "76561198000000000"})
    assert failed.success is False
    assert failed.status == "missing_api_key"

    ok = connector.connect(
        {
            "account_id": "76561198000000000",
            "steam_api_key": "abc123",
        }
    )
    assert ok.success is True
    assert ok.token_secret == "abc123"
    assert ok.auth_kind == "steam_openid_api_key"


def test_steam_refresh_merges_api_and_installed(monkeypatch) -> None:
    connector = SteamConnector()

    monkeypatch.setattr(
        "gamemanager.services.storefronts.steam_connector._owned_games_from_api",
        lambda _account, _key: [
            StoreEntitlement(
                entitlement_id="620",
                title="Portal 2",
                store_game_id="620",
                manifest_id="620",
                is_installed=False,
            )
        ],
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.steam_connector._owned_games_from_installed_manifests",
        lambda: [
            StoreEntitlement(
                entitlement_id="620",
                title="Portal 2",
                store_game_id="620",
                manifest_id="620",
                install_path="C:/Steam/common/Portal 2",
                is_installed=True,
            ),
            StoreEntitlement(
                entitlement_id="400",
                title="Portal",
                store_game_id="400",
                manifest_id="400",
                install_path="C:/Steam/common/Portal",
                is_installed=True,
            ),
        ],
    )
    rows = connector.refresh_entitlements(
        "76561198000000000",
        token_secret="abc123",
    )
    ids = {row.entitlement_id: row for row in rows}
    assert set(ids.keys()) == {"620", "400"}
    assert ids["620"].is_installed is True
    assert ids["620"].install_path == "C:/Steam/common/Portal 2"
