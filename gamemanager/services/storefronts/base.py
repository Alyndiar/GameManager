from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable


@dataclass(slots=True)
class StoreAuthResult:
    success: bool
    account_id: str = ""
    display_name: str = ""
    auth_kind: str = ""
    token_secret: str = ""
    expires_utc: str = ""
    scopes: str = ""
    status: str = ""
    message: str = ""


@dataclass(slots=True)
class StoreEntitlement:
    entitlement_id: str
    title: str
    store_game_id: str = ""
    manifest_id: str = ""
    launch_uri: str = ""
    install_path: str = ""
    is_installed: bool = False
    metadata_json: str = ""


@dataclass(slots=True)
class StoreConnectorStatus:
    available: bool
    connected: bool
    auth_kind: str
    message: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


class StoreConnector(ABC):
    store_name: str = ""

    @abstractmethod
    def connect(self, auth_payload: dict[str, str] | None = None) -> StoreAuthResult:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self, account_id: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def refresh_entitlements(
        self,
        account_id: str,
        *,
        token_secret: str = "",
        auth_payload: dict[str, str] | None = None,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[StoreEntitlement]:
        raise NotImplementedError

    @abstractmethod
    def status(self, account_id: str = "") -> StoreConnectorStatus:
        raise NotImplementedError

    # Optional hook: connectors that rotate refresh/access secrets during sync
    # can expose the latest persisted secret to the coordinator.
    def updated_token_secret(self) -> str:
        return ""


@dataclass(slots=True, frozen=True)
class StorePlugin:
    store_name: str
    connector_cls: type[StoreConnector]
    auth_kind: str = "launcher_import"
    supports_full_library_sync: bool = False
    description: str = ""

    def create_connector(self) -> StoreConnector:
        return self.connector_cls()
