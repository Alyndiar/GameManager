from __future__ import annotations

from pathlib import Path

from gamemanager.db import Database
from gamemanager.models import StoreOwnedGame
from gamemanager.services.storefront_sync import StorefrontSyncCoordinator
from gamemanager.services.storefronts.base import StoreConnectorStatus, StoreEntitlement


class _FakeTokenRotatingConnector:
    store_name = "EGS"

    def __init__(self, updated_token: str) -> None:
        self._updated_token = str(updated_token or "").strip()

    def connect(self, auth_payload=None):
        raise NotImplementedError

    def disconnect(self, account_id: str) -> bool:
        return True

    def refresh_entitlements(
        self,
        account_id: str,
        *,
        token_secret: str = "",
        auth_payload=None,
        progress_cb=None,
        should_cancel=None,
    ) -> list[StoreEntitlement]:
        _ = (account_id, token_secret, auth_payload, progress_cb, should_cancel)
        return [
            StoreEntitlement(
                entitlement_id="catalog-portal2",
                title="Portal 2",
                store_game_id="catalog-portal2",
                manifest_id="Portal2",
                is_installed=False,
            )
        ]

    def status(self, account_id: str = "") -> StoreConnectorStatus:
        return StoreConnectorStatus(
            available=True,
            connected=bool(str(account_id or "").strip()),
            auth_kind="test",
        )

    def updated_token_secret(self) -> str:
        return self._updated_token


class _FakeUnavailableEmptyConnector:
    store_name = "Itch.io"

    def connect(self, auth_payload=None):
        raise NotImplementedError

    def disconnect(self, account_id: str) -> bool:
        return True

    def refresh_entitlements(
        self,
        account_id: str,
        *,
        token_secret: str = "",
        auth_payload=None,
        progress_cb=None,
        should_cancel=None,
    ) -> list[StoreEntitlement]:
        _ = (account_id, token_secret, auth_payload, progress_cb, should_cancel)
        return []

    def status(self, account_id: str = "") -> StoreConnectorStatus:
        _ = account_id
        return StoreConnectorStatus(
            available=False,
            connected=False,
            auth_kind="launcher_import",
            message="Launcher runtime unavailable.",
        )

    def updated_token_secret(self) -> str:
        return ""


class _FakeLaunchAwareConnector:
    store_name = "Itch.io"

    def __init__(self) -> None:
        self.last_payload = {}

    def connect(self, auth_payload=None):
        raise NotImplementedError

    def disconnect(self, account_id: str) -> bool:
        return True

    def refresh_entitlements(
        self,
        account_id: str,
        *,
        token_secret: str = "",
        auth_payload=None,
        progress_cb=None,
        should_cancel=None,
    ) -> list[StoreEntitlement]:
        _ = (account_id, token_secret, progress_cb, should_cancel)
        self.last_payload = dict(auth_payload or {})
        return []

    def status(self, account_id: str = "") -> StoreConnectorStatus:
        _ = account_id
        return StoreConnectorStatus(
            available=False,
            connected=False,
            auth_kind="launcher_import",
            message="Launcher runtime unavailable.",
        )

    def updated_token_secret(self) -> str:
        return ""


def test_sync_account_persists_rotated_token(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_store_account("EGS", "acc-123", "Epic User", "browser_oauth", enabled=True)
    account = db.list_store_accounts(enabled_only=True)[0]

    connector = _FakeTokenRotatingConnector("refresh-new")
    monkeypatch.setattr(
        "gamemanager.services.storefront_sync.connector_for_store",
        lambda _store_name: connector,
    )

    persisted: list[tuple[str, str, str]] = []

    coordinator = StorefrontSyncCoordinator(
        db,
        token_loader=lambda _store, _account: "refresh-old",
        token_saver=lambda store, account_id, token: (
            persisted.append((store, account_id, token)) or True
        ),
    )

    result = coordinator.sync_account(account)
    assert result["status"] == "ok"
    assert persisted == [("EGS", "acc-123", "refresh-new")]
    rows = db.list_store_owned_games("EGS", "acc-123")
    assert len(rows) == 1
    assert rows[0].title == "Portal 2"


def test_sync_account_skips_token_persist_when_unchanged(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_store_account("EGS", "acc-123", "Epic User", "browser_oauth", enabled=True)
    account = db.list_store_accounts(enabled_only=True)[0]

    connector = _FakeTokenRotatingConnector("refresh-same")
    monkeypatch.setattr(
        "gamemanager.services.storefront_sync.connector_for_store",
        lambda _store_name: connector,
    )

    persisted: list[tuple[str, str, str]] = []

    coordinator = StorefrontSyncCoordinator(
        db,
        token_loader=lambda _store, _account: "refresh-same",
        token_saver=lambda store, account_id, token: (
            persisted.append((store, account_id, token)) or True
        ),
    )

    result = coordinator.sync_account(account)
    assert result["status"] == "ok"
    assert persisted == []


def test_sync_account_keeps_existing_when_source_unreachable(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_store_account("Itch.io", "acc-1", "Itch User", "launcher_import", enabled=True)
    db.replace_store_owned_games_for_account(
        "Itch.io",
        "acc-1",
        [
            StoreOwnedGame(
                store_name="Itch.io",
                account_id="acc-1",
                entitlement_id="100",
                title="Existing Game",
                store_game_id="100",
            )
        ],
    )
    account = db.list_store_accounts(enabled_only=True)[0]

    connector = _FakeUnavailableEmptyConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefront_sync.connector_for_store",
        lambda _store_name: connector,
    )
    coordinator = StorefrontSyncCoordinator(db)

    result = coordinator.sync_account(account, keep_existing_on_unreachable=True)

    assert result["status"] == "kept_existing"
    rows = db.list_store_owned_games("Itch.io", "acc-1")
    assert len(rows) == 1
    assert rows[0].title == "Existing Game"


def test_sync_enabled_accounts_passes_launch_client_preference(monkeypatch, tmp_path: Path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    db.upsert_store_account("Itch.io", "acc-1", "Itch User", "launcher_import", enabled=True)
    connector = _FakeLaunchAwareConnector()
    monkeypatch.setattr(
        "gamemanager.services.storefront_sync.connector_for_store",
        lambda _store_name: connector,
    )
    coordinator = StorefrontSyncCoordinator(db)

    result = coordinator.sync_enabled_accounts(
        launch_client_by_store={"Itch.io": True},
        keep_existing_on_unreachable=True,
    )

    assert len(result) == 1
    assert connector.last_payload.get("launch_client") == "1"
