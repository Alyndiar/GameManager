from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Callable

from gamemanager.db import Database
from gamemanager.models import StoreAccount, StoreOwnedGame
from gamemanager.services.storefronts.base import StoreEntitlement
from gamemanager.services.storefronts.priority import normalize_store_name
from gamemanager.services.storefronts.registry import connector_for_store


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StorefrontSyncCoordinator:
    def __init__(
        self,
        db: Database,
        *,
        token_loader: Callable[[str, str], str] | None = None,
        token_saver: Callable[[str, str, str], bool] | None = None,
    ):
        self._db = db
        self._token_loader = token_loader
        self._token_saver = token_saver

    @staticmethod
    def _status_indicates_unreachable(status: object) -> bool:
        if status is None:
            return False
        available = bool(getattr(status, "available", True))
        connected = bool(getattr(status, "connected", True))
        if not available or not connected:
            return True
        message = str(getattr(status, "message", "") or "").strip().casefold()
        if not message:
            return False
        keywords = (
            "not found",
            "not installed",
            "missing",
            "sign in",
            "login",
            "launcher",
            "runtime",
            "unavailable",
            "profile",
        )
        return any(token in message for token in keywords)

    @staticmethod
    def _to_owned_game(
        store_name: str,
        account_id: str,
        entitlement: StoreEntitlement,
        *,
        seen_utc: str,
    ) -> StoreOwnedGame:
        return StoreOwnedGame(
            store_name=store_name,
            account_id=account_id,
            entitlement_id=str(entitlement.entitlement_id or "").strip(),
            title=str(entitlement.title or "").strip(),
            store_game_id=str(entitlement.store_game_id or "").strip(),
            manifest_id=str(entitlement.manifest_id or "").strip(),
            launch_uri=str(entitlement.launch_uri or "").strip(),
            install_path=str(entitlement.install_path or "").strip(),
            is_installed=bool(entitlement.is_installed),
            metadata_json=str(entitlement.metadata_json or "").strip(),
            last_seen_utc=seen_utc,
        )

    def sync_account(
        self,
        account: StoreAccount,
        *,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        launch_client: bool | None = None,
        keep_existing_on_unreachable: bool = True,
    ) -> dict[str, object]:
        store_name = normalize_store_name(account.store_name)
        started_utc = _utc_now_iso()
        t0 = time.perf_counter()
        connector = connector_for_store(store_name)
        if connector is None:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            self._db.add_store_sync_run(
                store_name=store_name,
                account_id=account.account_id,
                started_utc=started_utc,
                completed_utc=_utc_now_iso(),
                status="unsupported",
                duration_ms=duration_ms,
                imported_count=0,
                error_summary="No connector for store.",
            )
            return {
                "store_name": store_name,
                "account_id": account.account_id,
                "status": "unsupported",
                "imported_count": 0,
                "error": "No connector for store.",
            }

        existing_rows = self._db.list_store_owned_games(store_name, account.account_id)
        existing_count = len(existing_rows)
        launcher_auth = "launcher" in str(account.auth_kind or "").strip().casefold()
        status_before = None
        try:
            status_before = connector.status(account.account_id)
        except Exception:
            status_before = None
        unreachable_before = self._status_indicates_unreachable(status_before)
        explicit_launch_denied = launcher_auth and launch_client is False

        try:
            token_secret = ""
            if self._token_loader is not None:
                token_secret = str(
                    self._token_loader(store_name, account.account_id) or ""
                ).strip()
            auth_payload = {
                "account_id": account.account_id,
                "display_name": account.display_name,
                "username": account.display_name,
                "project_data_dir": str(self._db.db_path.parent),
            }
            if launcher_auth and launch_client is not None:
                auth_payload["launch_client"] = "1" if launch_client else "0"
            entitlements = connector.refresh_entitlements(
                account.account_id,
                token_secret=token_secret,
                auth_payload=auth_payload,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )
            updated_token_secret = str(connector.updated_token_secret() or "").strip()
            if (
                self._token_saver is not None
                and updated_token_secret
                and updated_token_secret != token_secret
            ):
                if not bool(
                    self._token_saver(store_name, account.account_id, updated_token_secret)
                ):
                    raise RuntimeError(
                        f"Could not persist refreshed token for {store_name} account {account.account_id}."
                    )
            seen_utc = _utc_now_iso()
            rows = [
                self._to_owned_game(
                    store_name,
                    account.account_id,
                    entitlement,
                    seen_utc=seen_utc,
                )
                for entitlement in entitlements
                if str(entitlement.entitlement_id or "").strip()
                and str(entitlement.title or "").strip()
            ]

            status_after = None
            try:
                status_after = connector.status(account.account_id)
            except Exception:
                status_after = None
            unreachable_after = self._status_indicates_unreachable(status_after)
            if (
                keep_existing_on_unreachable
                and not rows
                and existing_count > 0
                and (unreachable_before or unreachable_after or explicit_launch_denied)
            ):
                duration_ms = int((time.perf_counter() - t0) * 1000)
                self._db.add_store_sync_run(
                    store_name=store_name,
                    account_id=account.account_id,
                    started_utc=started_utc,
                    completed_utc=_utc_now_iso(),
                    status="kept_existing",
                    duration_ms=duration_ms,
                    imported_count=0,
                    error_summary=(
                        "Source unavailable; preserved existing entitlements."
                        if (unreachable_before or unreachable_after)
                        else "Launcher start denied; preserved existing entitlements."
                    ),
                )
                return {
                    "store_name": store_name,
                    "account_id": account.account_id,
                    "status": "kept_existing",
                    "imported_count": 0,
                    "kept_existing_count": existing_count,
                    "error": "",
                }

            self._db.replace_store_owned_games_for_account(
                store_name,
                account.account_id,
                rows,
            )
            duration_ms = int((time.perf_counter() - t0) * 1000)
            self._db.add_store_sync_run(
                store_name=store_name,
                account_id=account.account_id,
                started_utc=started_utc,
                completed_utc=_utc_now_iso(),
                status="ok",
                duration_ms=duration_ms,
                imported_count=len(rows),
                error_summary="",
            )
            return {
                "store_name": store_name,
                "account_id": account.account_id,
                "status": "ok",
                "imported_count": len(rows),
                "error": "",
            }
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            self._db.add_store_sync_run(
                store_name=store_name,
                account_id=account.account_id,
                started_utc=started_utc,
                completed_utc=_utc_now_iso(),
                status="failed",
                duration_ms=duration_ms,
                imported_count=0,
                error_summary=str(exc),
            )
            return {
                "store_name": store_name,
                "account_id": account.account_id,
                "status": "failed",
                "imported_count": 0,
                "error": str(exc),
            }

    def sync_enabled_accounts(
        self,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        launch_client_by_store: dict[str, bool] | None = None,
        keep_existing_on_unreachable: bool = True,
    ) -> list[dict[str, object]]:
        accounts = self._db.list_store_accounts(enabled_only=True)
        total_accounts = len(accounts)
        total = max(1, total_accounts * 100)
        results: list[dict[str, object]] = []
        if progress_cb is not None:
            progress_cb("Store sync", 0, total)
        for idx, account in enumerate(accounts, start=1):
            if should_cancel is not None and should_cancel():
                break
            account_store = normalize_store_name(account.store_name)
            account_label = f"{account_store} ({account.account_id})"
            account_base = (idx - 1) * 100

            def _account_progress(stage: str, current: int, stage_total: int) -> None:
                if progress_cb is None:
                    return
                label = str(stage or "").strip() or f"{account_label} sync"
                try:
                    cur = int(current)
                except Exception:
                    cur = 0
                try:
                    tot = int(stage_total)
                except Exception:
                    tot = 0
                if tot <= 0:
                    account_pct = 0
                else:
                    account_pct = int(round((max(0, cur) / max(1, tot)) * 100.0))
                global_current = min(total, account_base + max(0, min(100, account_pct)))
                progress_cb(f"{account_label}: {label}", global_current, total)

            results.append(
                self.sync_account(
                    account,
                    progress_cb=_account_progress,
                    should_cancel=should_cancel,
                    launch_client=(
                        launch_client_by_store.get(account_store)
                        if launch_client_by_store is not None
                        else None
                    ),
                    keep_existing_on_unreachable=keep_existing_on_unreachable,
                )
            )
            if progress_cb is not None:
                progress_cb(f"{account_label}: done", min(total, idx * 100), total)
        return results
