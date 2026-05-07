from __future__ import annotations

import json

from gamemanager.services.storefronts.base import StoreEntitlement
from gamemanager.services.storefronts.epic_connector import (
    EpicConnector,
    _decode_json,
    _epic_service_domains,
)


def test_epic_connect_requires_authorization_code() -> None:
    connector = EpicConnector()
    failed = connector.connect({})
    assert failed.success is False
    assert failed.status == "missing_authorization_input"


def test_epic_connect_exchanges_code_and_reads_profile(monkeypatch) -> None:
    connector = EpicConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        lambda payload: {
            "access_token": "acc",
            "refresh_token": "ref",
            "token_type": "bearer",
            "account_id": "acc-123",
            "expires_at": "2030-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_account",
        lambda account_id, token_type, access_token: {
            "id": account_id,
            "displayName": "Epic User",
        },
    )
    result = connector.connect({"authorization_code": "code-abc"})
    assert result.success is True
    assert result.account_id == "acc-123"
    assert result.display_name == "Epic User"
    assert result.token_secret == "ref"


def test_epic_connect_accepts_redirect_url_and_json_payload(monkeypatch) -> None:
    connector = EpicConnector()

    calls: list[dict[str, str]] = []

    def _oauth(payload: dict[str, str]) -> dict[str, object]:
        calls.append(dict(payload))
        return {
            "access_token": "acc",
            "refresh_token": "ref",
            "token_type": "bearer",
            "account_id": "acc-123",
        }

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        _oauth,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_account",
        lambda account_id, token_type, access_token: {
            "id": account_id,
            "displayName": "Epic User",
        },
    )

    result_url = connector.connect(
        {
            "authorization_code": "https://localhost/launcher/authorized?code=code-from-url"
        }
    )
    assert result_url.success is True
    assert calls[-1]["code"] == "code-from-url"

    result_json = connector.connect(
        {
            "authorization_code": '{"authorizationCode":"code-from-json","sid":"x"}'
        }
    )
    assert result_json.success is True
    assert calls[-1]["code"] == "code-from-json"


def test_epic_connect_accepts_json_sid_and_uses_exchange_code(monkeypatch) -> None:
    connector = EpicConnector()
    oauth_calls: list[dict[str, str]] = []

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_exchange_code_from_sid",
        lambda sid: "exchange-from-sid",
    )

    def _oauth(payload: dict[str, str]) -> dict[str, object]:
        oauth_calls.append(dict(payload))
        return {
            "access_token": "acc",
            "refresh_token": "ref",
            "token_type": "bearer",
            "account_id": "acc-123",
        }

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        _oauth,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_account",
        lambda account_id, token_type, access_token: {
            "id": account_id,
            "displayName": "Epic User",
        },
    )
    result = connector.connect(
        {
            "authorization_code": (
                '{"warning":"Do not share this code with any 3rd party service.",'
                '"redirectUrl":"https://epicgames.com/account/personal",'
                '"authorizationCode":null,"exchangeCode":null,'
                '"sid":"a7aed8a5d798419c8226d919eeae63d0"}'
            )
        }
    )
    assert result.success is True
    assert oauth_calls[-1]["grant_type"] == "exchange_code"
    assert oauth_calls[-1]["exchange_code"] == "exchange-from-sid"


def test_epic_connect_json_redirect_url_code_is_used_even_if_authorization_code_null(monkeypatch) -> None:
    connector = EpicConnector()
    calls: list[dict[str, str]] = []

    def _oauth(payload: dict[str, str]) -> dict[str, object]:
        calls.append(dict(payload))
        return {
            "access_token": "acc",
            "refresh_token": "ref",
            "token_type": "bearer",
            "account_id": "acc-123",
        }

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        _oauth,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_account",
        lambda account_id, token_type, access_token: {
            "id": account_id,
            "displayName": "Epic User",
        },
    )
    result = connector.connect(
        {
            "authorization_code": (
                '{"redirectUrl":"https://localhost/launcher/authorized?code=code-from-redirect",'
                '"authorizationCode":null,"exchangeCode":null,"sid":"sid-legacy"}'
            )
        }
    )
    assert result.success is True
    assert calls[-1]["grant_type"] == "authorization_code"
    assert calls[-1]["code"] == "code-from-redirect"


def test_epic_connect_falls_back_to_exchange_grant_when_auth_code_not_found(monkeypatch) -> None:
    connector = EpicConnector()
    calls: list[dict[str, str]] = []

    def _oauth(payload: dict[str, str]) -> dict[str, object]:
        calls.append(dict(payload))
        if payload.get("grant_type") == "authorization_code":
            raise RuntimeError(
                "Epic request failed: auth code missing "
                "(errors.com.epicgames.account.oauth.authorization_code_not_found)"
            )
        return {
            "access_token": "acc",
            "refresh_token": "ref",
            "token_type": "bearer",
            "account_id": "acc-123",
        }

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        _oauth,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_account",
        lambda account_id, token_type, access_token: {
            "id": account_id,
            "displayName": "Epic User",
        },
    )
    result = connector.connect({"authorization_code": "copied-code-token"})
    assert result.success is True
    assert len(calls) == 2
    assert calls[0]["grant_type"] == "authorization_code"
    assert calls[1]["grant_type"] == "exchange_code"
    assert calls[1]["exchange_code"] == "copied-code-token"


def test_epic_connect_falls_back_to_exchange_grant_on_generic_auth_code_failure(monkeypatch) -> None:
    connector = EpicConnector()
    calls: list[dict[str, str]] = []

    def _oauth(payload: dict[str, str]) -> dict[str, object]:
        calls.append(dict(payload))
        if payload.get("grant_type") == "authorization_code":
            raise RuntimeError("HTTP Error 400: Bad Request")
        return {
            "access_token": "acc",
            "refresh_token": "ref",
            "token_type": "bearer",
            "account_id": "acc-123",
        }

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        _oauth,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_account",
        lambda account_id, token_type, access_token: {
            "id": account_id,
            "displayName": "Epic User",
        },
    )
    result = connector.connect({"authorization_code": "copied-code-token"})
    assert result.success is True
    assert len(calls) == 2
    assert calls[0]["grant_type"] == "authorization_code"
    assert calls[1]["grant_type"] == "exchange_code"
    assert calls[1]["exchange_code"] == "copied-code-token"


def test_epic_connect_explicit_sid_token_uses_sid_exchange(monkeypatch) -> None:
    connector = EpicConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_exchange_code_from_sid",
        lambda sid: "exchange-from-sid",
    )
    calls: list[dict[str, str]] = []

    def _oauth(payload: dict[str, str]) -> dict[str, object]:
        calls.append(dict(payload))
        return {
            "access_token": "acc",
            "refresh_token": "ref",
            "token_type": "bearer",
            "account_id": "acc-123",
        }

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        _oauth,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_account",
        lambda account_id, token_type, access_token: {
            "id": account_id,
            "displayName": "Epic User",
        },
    )
    result = connector.connect({"sid": "a7aed8a5d798419c8226d919eeae63d0"})
    assert result.success is True
    assert calls[-1]["grant_type"] == "exchange_code"
    assert calls[-1]["exchange_code"] == "exchange-from-sid"


def test_decode_json_tolerates_empty_or_invalid_payload() -> None:
    assert _decode_json(b"") == {}
    assert _decode_json(b"  \n\t ") == {}
    assert _decode_json(b"<html>not-json</html>") == {}


def test_epic_service_domains_loads_portal_regions_ini(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "DefaultPortalRegions.ini"
    cfg.write_text(
        "\n".join(
            [
                "[Portal.OnlineSubsystemMcp.OnlineIdentityMcp Prod]",
                "Domain=https://identity.example.test/",
                "",
                "[Portal.OnlineSubsystemMcp.OnlineLibraryServiceMcp Prod]",
                "Domain=https://library.example.test/",
                "",
                "[Portal.OnlineSubsystemMcp.OnlineCatalogServiceMcp Prod]",
                "Domain=https://catalog.example.test/",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_launcher_config_candidates",
        lambda: [cfg],
    )
    _epic_service_domains.cache_clear()
    try:
        identity, library, catalog = _epic_service_domains()
        assert identity == "identity.example.test"
        assert library == "library.example.test"
        assert catalog == "catalog.example.test"
    finally:
        _epic_service_domains.cache_clear()


def test_epic_refresh_merges_api_and_installed(monkeypatch) -> None:
    connector = EpicConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        lambda payload: {
            "access_token": "acc",
            "refresh_token": "refresh-rotated",
            "token_type": "bearer",
            "account_id": "acc-123",
        },
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_library_records",
        lambda **kwargs: [
            {
                "namespace": "base",
                "catalogItemId": "catalog-portal2",
                "appName": "Portal2",
                "assetId": "asset-1",
                "sandboxType": "LIVE",
            },
            {
                "namespace": "base",
                "catalogItemId": "catalog-dlc",
                "appName": "Portal2Dlc",
                "assetId": "asset-2",
                "sandboxType": "LIVE",
            },
            {
                "namespace": "ue",
                "catalogItemId": "catalog-ue",
                "appName": "UE_5",
                "assetId": "asset-3",
                "sandboxType": "LIVE",
            },
        ],
    )

    def _catalog_parallel(missing_keys, **kwargs) -> dict[tuple[str, str], dict[str, object]]:
        _ = kwargs
        out: dict[tuple[str, str], dict[str, object]] = {}
        for namespace, catalog_item_id in missing_keys:
            if catalog_item_id == "catalog-portal2":
                out[(namespace, catalog_item_id)] = {
                    "title": "Portal 2",
                    "categories": [{"path": "games"}],
                }
            elif catalog_item_id == "catalog-dlc":
                out[(namespace, catalog_item_id)] = {
                    "title": "Portal 2 DLC",
                    "categories": [{"path": "games"}],
                    "mainGameItem": {"id": "catalog-portal2"},
                }
        return out

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_catalog_items_parallel",
        _catalog_parallel,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._installed_entitlements_from_manifests",
        lambda: [
            StoreEntitlement(
                entitlement_id="catalog-portal2",
                title="Portal 2",
                store_game_id="catalog-portal2",
                manifest_id="Portal2",
                install_path="C:/Games/Portal2",
                is_installed=True,
            ),
            StoreEntitlement(
                entitlement_id="catalog-installed-only",
                title="Installed Only",
                store_game_id="catalog-installed-only",
                manifest_id="InstalledOnly",
                install_path="C:/Games/InstalledOnly",
                is_installed=True,
            ),
        ],
    )

    rows = connector.refresh_entitlements("acc-123", token_secret="refresh-abc")
    by_id = {row.entitlement_id: row for row in rows}
    assert set(by_id.keys()) == {"catalog-portal2", "catalog-installed-only"}
    assert by_id["catalog-portal2"].is_installed is True
    assert by_id["catalog-portal2"].install_path == "C:/Games/Portal2"
    payload = json.loads(by_id["catalog-portal2"].metadata_json)
    assert payload["catalog_item_id"] == "catalog-portal2"
    assert connector.updated_token_secret() == "refresh-rotated"


def test_epic_refresh_without_token_returns_installed_only(monkeypatch) -> None:
    connector = EpicConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._installed_entitlements_from_manifests",
        lambda: [
            StoreEntitlement(
                entitlement_id="catalog-installed",
                title="Installed",
                store_game_id="catalog-installed",
                manifest_id="InstalledGame",
                install_path="C:/Games/Installed",
                is_installed=True,
            ),
        ],
    )
    rows = connector.refresh_entitlements("acc-123", token_secret="")
    assert len(rows) == 1
    assert rows[0].entitlement_id == "catalog-installed"


def test_epic_refresh_uses_persistent_catalog_cache(monkeypatch, tmp_path) -> None:
    connector = EpicConnector()
    calls: list[list[tuple[str, str]]] = []

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._epic_oauth_token",
        lambda payload: {
            "access_token": "acc",
            "refresh_token": "refresh-rotated",
            "token_type": "bearer",
            "account_id": "acc-123",
        },
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_library_records",
        lambda **kwargs: [
            {
                "namespace": "base",
                "catalogItemId": "catalog-portal2",
                "appName": "Portal2",
                "assetId": "asset-1",
                "sandboxType": "LIVE",
            },
        ],
    )

    def _catalog_parallel(missing_keys, **kwargs) -> dict[tuple[str, str], dict[str, object]]:
        _ = kwargs
        calls.append(list(missing_keys))
        return {
            (namespace, catalog_id): {
                "title": "Portal 2",
                "categories": [{"path": "games"}],
            }
            for namespace, catalog_id in missing_keys
        }

    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._fetch_epic_catalog_items_parallel",
        _catalog_parallel,
    )
    monkeypatch.setattr(
        "gamemanager.services.storefronts.epic_connector._installed_entitlements_from_manifests",
        lambda: [],
    )

    payload = {"project_data_dir": str(tmp_path)}
    rows_1 = connector.refresh_entitlements("acc-123", token_secret="refresh-abc", auth_payload=payload)
    rows_2 = connector.refresh_entitlements("acc-123", token_secret="refresh-abc", auth_payload=payload)

    assert len(rows_1) == 1
    assert len(rows_2) == 1
    assert len(calls) == 1
    assert calls[0] == [("base", "catalog-portal2")]
