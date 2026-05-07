from __future__ import annotations

from typing import Callable

from gamemanager.services.storefronts.base import (
    StoreAuthResult,
    StoreConnector,
    StoreConnectorStatus,
    StoreEntitlement,
)


class StubLauncherConnector(StoreConnector):
    store_name: str = ""

    def connect(self, auth_payload: dict[str, str] | None = None) -> StoreAuthResult:
        payload = dict(auth_payload or {})
        account_id = str(payload.get("account_id", "")).strip()
        display_name = str(payload.get("display_name", "")).strip()
        if not account_id:
            return StoreAuthResult(
                success=False,
                status="missing_account_id",
                message="Launcher import connector requires account_id in auth payload.",
            )
        return StoreAuthResult(
            success=True,
            account_id=account_id,
            display_name=display_name or account_id,
            auth_kind="launcher_import",
            status="connected",
            message="Launcher import connector linked.",
        )

    def disconnect(self, account_id: str) -> bool:
        return bool(str(account_id or "").strip())

    def refresh_entitlements(
        self,
        account_id: str,
        *,
        token_secret: str = "",
        auth_payload: dict[str, str] | None = None,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[StoreEntitlement]:
        _ = (account_id, token_secret, auth_payload, progress_cb, should_cancel)
        return []

    def status(self, account_id: str = "") -> StoreConnectorStatus:
        connected = bool(str(account_id or "").strip())
        return StoreConnectorStatus(
            available=True,
            connected=connected,
            auth_kind="launcher_import",
            message="Launcher import connector scaffold (no live entitlement parser yet).",
            metadata={"store_name": self.store_name},
        )
