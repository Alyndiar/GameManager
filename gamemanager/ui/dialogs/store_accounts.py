from __future__ import annotations

from collections.abc import Callable
import json
import queue
import re
import threading
import time
import webbrowser

from PySide6.QtCore import Qt, QUrl
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from gamemanager.services.storefronts.priority import normalize_store_name
from gamemanager.services.storefronts.epic_connector import (
    EPIC_LOGIN_AUTH_CODE_URL,
    EPIC_SECURITY_SETTINGS_URL,
)
from gamemanager.services.storefronts.gog_connector import (
    GOG_ACCOUNT_BASIC_URL,
    GOG_ACCOUNT_PAGE_URL,
)
from gamemanager.services.storefronts.registry import connector_for_store
from gamemanager.services.storefronts.steam_auth import authenticate_steam_openid


_EPIC_CODE_HTML_RE = re.compile(
    r"localhost\/launcher\/authorized\?code=([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_EPIC_AUTH_CODE_RE = re.compile(r'"authorizationCode"\s*:\s*"([^"]+)"', re.IGNORECASE)
_EPIC_EXCHANGE_CODE_RE = re.compile(r'"exchangeCode"\s*:\s*"([^"]+)"', re.IGNORECASE)
_EPIC_SID_RE = re.compile(r'"sid"\s*:\s*"([^"]+)"', re.IGNORECASE)


def _gog_auth_payload_from_text(raw: str) -> dict[str, str]:
    token = str(raw or "").strip()
    if not token:
        return {}
    if token.startswith("{") and token.endswith("}"):
        try:
            parsed = json.loads(token)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            out: dict[str, str] = {"account_basic_json": token}
            username = str(parsed.get("username") or "").strip()
            user_id = str(parsed.get("userId") or parsed.get("user_id") or "").strip()
            access_token = str(parsed.get("accessToken") or parsed.get("access_token") or "").strip()
            if username:
                out["username"] = username
            if user_id:
                out["account_id"] = user_id
            if access_token:
                out["access_token"] = access_token
            return out
    # Public account-name mode.
    return {"username": token, "account_id": token}


def _epic_auth_payload_from_text(
    raw: str,
) -> dict[str, str]:
    def _non_null_token(value: object) -> str:
        token = str(value or "").strip()
        if not token:
            return ""
        if token.casefold() in {"null", "none", "undefined"}:
            return ""
        return token

    token = str(raw or "").strip()
    if not token:
        return {}
    payload: dict[str, str] = {}
    if token.startswith("http://") or token.startswith("https://"):
        try:
            parsed = QUrl(token)
            query = parsed.query()
            values = dict(
                pair.split("=", 1) if "=" in pair else (pair, "")
                for pair in query.split("&")
                if pair
            )
            for key in ("authorizationCode", "code"):
                value = _non_null_token(values.get(key, ""))
                if value:
                    payload["authorization_code"] = value
                    break
            for key in ("exchangeCode", "exchange_code"):
                value = _non_null_token(values.get(key, ""))
                if value:
                    payload["exchange_code"] = value
                    break
            sid = _non_null_token(values.get("sid", ""))
            if sid:
                payload["sid"] = sid
            if payload:
                return payload
        except Exception:
            pass
    if token.startswith("{") and token.endswith("}"):
        try:
            parsed_json = json.loads(token)
            if isinstance(parsed_json, dict):
                auth_code = str(
                    parsed_json.get("authorizationCode")
                    or parsed_json.get("code")
                    or ""
                )
                exchange_code = str(
                    parsed_json.get("exchangeCode")
                    or parsed_json.get("exchange_code")
                    or ""
                )
                auth_code = _non_null_token(auth_code)
                exchange_code = _non_null_token(exchange_code)
                sid = _non_null_token(parsed_json.get("sid"))
                redirect_url = _non_null_token(parsed_json.get("redirectUrl"))
                if redirect_url:
                    try:
                        parsed_redirect = QUrl(redirect_url)
                        query = parsed_redirect.query()
                        values = dict(
                            pair.split("=", 1) if "=" in pair else (pair, "")
                            for pair in query.split("&")
                            if pair
                        )
                        if not auth_code:
                            for key in ("authorizationCode", "code"):
                                value = _non_null_token(values.get(key, ""))
                                if value:
                                    auth_code = value
                                    break
                        if not exchange_code:
                            for key in ("exchangeCode", "exchange_code"):
                                value = _non_null_token(values.get(key, ""))
                                if value:
                                    exchange_code = value
                                    break
                        if not sid:
                            sid = _non_null_token(values.get("sid", ""))
                    except Exception:
                        pass
                if auth_code:
                    payload["authorization_code"] = auth_code
                if exchange_code:
                    payload["exchange_code"] = exchange_code
                if sid:
                    payload["sid"] = sid
                if payload:
                    return payload
        except Exception:
            pass
    for key, pattern in (
        ("authorization_code", _EPIC_AUTH_CODE_RE),
        ("exchange_code", _EPIC_EXCHANGE_CODE_RE),
        ("sid", _EPIC_SID_RE),
    ):
        match = pattern.search(token)
        if match is not None:
            value = _non_null_token(match.group(1))
            if value:
                payload[key] = value
    if payload:
        return payload
    match = _EPIC_CODE_HTML_RE.search(token)
    if match is not None:
        value = str(match.group(1) or "").strip()
        if value:
            return {"authorization_code": value}
    return {"authorization_code": token}


class StoreAccountsDialog(QDialog):
    def __init__(
        self,
        state,
        *,
        after_sync_callback: Callable[[], None] | None = None,
        parent: QDialog | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Storefront Accounts")
        self.state = state
        self._after_sync_callback = after_sync_callback

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Link store accounts, run entitlement sync, and rebuild strict ownership links."
            )
        )

        self.table = QTableWidget(0, 4, self)
        self.table.setHorizontalHeaderLabels(["Store", "Accounts", "Last Sync", "Auth"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table)

        row_buttons = QHBoxLayout()
        self.connect_btn = QPushButton("Connect Selected", self)
        self.disconnect_btn = QPushButton("Disconnect Selected", self)
        self.sync_btn = QPushButton("Sync Enabled", self)
        self.rebuild_btn = QPushButton("Rebuild Links", self)
        self.force_rebuild_chk = QCheckBox("Force Rebuild All", self)
        self.force_rebuild_chk.setToolTip(
            "Recompute ownership links for every game/store even if local IDs and names are unchanged."
        )
        raw_force = str(self.state.get_ui_pref("store_force_rebuild_all", "0") or "").strip().casefold()
        self.force_rebuild_chk.setChecked(raw_force not in {"0", "false", "no", "off", ""})
        row_buttons.addWidget(self.connect_btn)
        row_buttons.addWidget(self.disconnect_btn)
        row_buttons.addWidget(self.sync_btn)
        row_buttons.addWidget(self.rebuild_btn)
        row_buttons.addWidget(self.force_rebuild_chk)
        row_buttons.addStretch(1)
        layout.addLayout(row_buttons)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close_btn is not None:
            close_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self.connect_btn.clicked.connect(self._on_connect_selected)
        self.disconnect_btn.clicked.connect(self._on_disconnect_selected)
        self.sync_btn.clicked.connect(self._on_sync_enabled)
        self.rebuild_btn.clicked.connect(self._on_rebuild_links)
        self.force_rebuild_chk.toggled.connect(
            lambda checked: self.state.set_ui_pref(
                "store_force_rebuild_all",
                "1" if checked else "0",
            )
        )

        self._reload()

    def _success_popups_enabled(self) -> bool:
        raw = str(self.state.get_ui_pref("show_success_popups", "1") or "").strip().casefold()
        return raw not in {"0", "false", "no", "off"}

    def _set_success_popups_enabled(self, enabled: bool) -> None:
        self.state.set_ui_pref("show_success_popups", "1" if enabled else "0")

    def _show_success_popup(self, title: str, message: str) -> None:
        if not self._success_popups_enabled():
            return
        box = QMessageBox(self)
        box.setWindowTitle(str(title or "").strip() or "Operation Complete")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(str(message or "").strip())
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        dont_show = QCheckBox("Don't show success confirmations again", box)
        box.setCheckBox(dont_show)
        box.exec()
        if dont_show.isChecked():
            self._set_success_popups_enabled(False)

    @staticmethod
    def _sync_result_account_name(row: dict[str, object]) -> str:
        store = normalize_store_name(str(row.get("store_name", "")).strip())
        account = str(row.get("account_id", "")).strip()
        if store and account:
            return f"{store} ({account})"
        if store:
            return store
        if account:
            return account
        return "(unknown account)"

    def _sync_result_account_groups(
        self,
        results: list[dict[str, object]],
    ) -> tuple[list[str], list[str], list[str]]:
        successful: list[str] = []
        failed: list[str] = []
        other: list[str] = []
        for row in list(results or []):
            status = str(row.get("status", "")).strip().casefold()
            label = self._sync_result_account_name(row)
            if status in {"ok", "kept_existing"}:
                successful.append(label)
            elif status == "failed":
                failed.append(label)
            else:
                other.append(label)
        return successful, failed, other

    def _collect_launcher_start_preferences(self) -> dict[str, bool]:
        accounts = list(self.state.list_store_accounts(enabled_only=True))
        launcher_accounts: dict[str, object] = {}
        for account in accounts:
            auth_kind = str(getattr(account, "auth_kind", "") or "").strip().casefold()
            if "launcher" not in auth_kind:
                continue
            canonical = normalize_store_name(str(getattr(account, "store_name", "") or ""))
            if not canonical or canonical in launcher_accounts:
                continue
            launcher_accounts[canonical] = account

        prefs: dict[str, bool] = {}
        for store_name, account in sorted(launcher_accounts.items()):
            connector = connector_for_store(store_name)
            if connector is None:
                continue
            needs_prompt = False
            reason = ""
            try:
                status = connector.status(str(getattr(account, "account_id", "") or ""))
                needs_prompt = not bool(status.available)
                reason = str(status.message or "").strip()
            except Exception as exc:
                needs_prompt = True
                reason = str(exc)
            if not needs_prompt:
                continue
            lines = [
                f"{store_name} source appears unavailable right now.",
                "Start its launcher now before sync?",
                "",
                "If you choose No (or start fails), existing ownership data will be kept.",
            ]
            if reason:
                lines.extend(["", f"Details: {reason}"])
            answer = QMessageBox.question(
                self,
                f"{store_name} Launcher",
                "\n".join(lines),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            prefs[store_name] = answer == QMessageBox.StandardButton.Yes
        return prefs

    def _run_modal_background(self, title: str, label: str, fn):
        progress = QProgressDialog(label, "", 0, 0, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.show()
        QApplication.processEvents()

        result_box: dict[str, object] = {}

        def _worker() -> None:
            try:
                result_box["value"] = fn()
            except Exception as exc:
                result_box["error"] = exc

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        while thread.is_alive():
            QApplication.processEvents()
            time.sleep(0.03)
        progress.close()

        err = result_box.get("error")
        if isinstance(err, Exception):
            raise err
        return result_box.get("value")

    def _run_modal_background_with_progress(self, title: str, label: str, fn):
        progress = QProgressDialog(label, "Cancel", 0, 0, self)
        progress.setWindowTitle(title)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.show()
        QApplication.processEvents()

        updates: queue.SimpleQueue[tuple[str, int, int]] = queue.SimpleQueue()
        cancel_event = threading.Event()
        result_box: dict[str, object] = {}
        started_at = time.monotonic()
        last_heartbeat = -1
        stage_text = str(label or "").strip() or title
        stage_current = 0
        stage_total = 0

        def _emit_progress(stage: str, current: int, total: int) -> None:
            text = str(stage or "").strip() or stage_text
            try:
                cur = int(current)
            except Exception:
                cur = 0
            try:
                tot = int(total)
            except Exception:
                tot = 0
            updates.put((text, cur, tot))

        def _update_dialog(stage: str, current: int, total: int, *, include_elapsed: bool = False) -> None:
            nonlocal stage_text, stage_current, stage_total
            stage_text = str(stage or "").strip() or stage_text
            stage_current = max(0, int(current))
            stage_total = max(0, int(total))
            if stage_total > 0:
                progress.setRange(0, stage_total)
                progress.setValue(min(stage_current, stage_total))
                base = f"{stage_text} ({min(stage_current, stage_total)}/{stage_total})"
            else:
                progress.setRange(0, 0)
                base = stage_text
            if include_elapsed:
                elapsed_s = int(max(0, time.monotonic() - started_at))
                if cancel_event.is_set():
                    progress.setLabelText(f"{base} | Canceling...")
                else:
                    progress.setLabelText(f"{base} | Elapsed: {elapsed_s}s")
            else:
                progress.setLabelText(base)

        def _worker() -> None:
            try:
                result_box["value"] = fn(_emit_progress, lambda: cancel_event.is_set())
            except Exception as exc:
                result_box["error"] = exc

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        while thread.is_alive():
            QApplication.processEvents()
            if progress.wasCanceled() and not cancel_event.is_set():
                cancel_event.set()
            drained = False
            while True:
                try:
                    stage, current, total = updates.get_nowait()
                except queue.Empty:
                    break
                _update_dialog(stage, current, total)
                drained = True
            if not drained:
                heartbeat = int(max(0, time.monotonic() - started_at))
                if heartbeat != last_heartbeat:
                    _update_dialog(stage_text, stage_current, stage_total, include_elapsed=True)
                    last_heartbeat = heartbeat
            time.sleep(0.03)

        while True:
            try:
                stage, current, total = updates.get_nowait()
            except queue.Empty:
                break
            _update_dialog(stage, current, total)
        progress.close()

        err = result_box.get("error")
        if isinstance(err, Exception):
            raise err
        return result_box.get("value"), bool(cancel_event.is_set())

    def _selected_store_name(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        if item is None:
            return None
        token = normalize_store_name(item.text())
        return token or None

    def _accounts_by_store(self) -> dict[str, list[object]]:
        grouped: dict[str, list[object]] = {}
        for account in self.state.list_store_accounts():
            store = normalize_store_name(account.store_name)
            grouped.setdefault(store, []).append(account)
        return grouped

    def _reload(self) -> None:
        accounts_by_store = self._accounts_by_store()
        stores = self.state.available_store_names()
        self.table.setRowCount(len(stores))
        for row, store_name in enumerate(stores):
            linked = accounts_by_store.get(store_name, [])
            account_text = ", ".join(account.account_id for account in linked) if linked else "Not linked"
            last_sync = ""
            auth_kind = "launcher_import"
            if linked:
                last_sync = max((str(account.last_sync_utc or "") for account in linked), default="")
                auth_kind = linked[0].auth_kind
            self.table.setItem(row, 0, QTableWidgetItem(store_name))
            self.table.setItem(row, 1, QTableWidgetItem(account_text))
            self.table.setItem(row, 2, QTableWidgetItem(last_sync))
            self.table.setItem(row, 3, QTableWidgetItem(auth_kind))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        if stores:
            self.table.selectRow(0)

    def _on_connect_selected(self) -> None:
        store_name = self._selected_store_name()
        if not store_name:
            QMessageBox.information(self, "Storefront Accounts", "Select a store row first.")
            return
        canonical_store = normalize_store_name(store_name)
        if canonical_store == "Steam":
            self._on_connect_steam()
            return
        if canonical_store == "EGS":
            self._on_connect_egs()
            return
        if canonical_store == "GOG":
            self._on_connect_gog()
            return
        if canonical_store == "Itch.io":
            self._on_connect_itch()
            return
        account_id, ok = QInputDialog.getText(
            self,
            "Connect Store Account",
            f"{store_name} account id:",
        )
        if not ok:
            return
        account_id = account_id.strip()
        if not account_id:
            QMessageBox.warning(self, "Connect Store Account", "Account id cannot be empty.")
            return
        display_name, ok2 = QInputDialog.getText(
            self,
            "Connect Store Account",
            f"{store_name} display name (optional):",
            text=account_id,
        )
        if not ok2:
            return
        account = self.state.connect_store_account(
            store_name,
            {
                "account_id": account_id,
                "display_name": display_name.strip(),
            },
        )
        if account is None:
            QMessageBox.warning(
                self,
                "Connect Store Account",
                f"Could not connect {store_name}.",
            )
            return
        self._show_connect_and_sync_result(store_name, account.account_id)
        self._reload()

    def _on_connect_steam(self) -> None:
        progress = QProgressDialog(
            "Waiting for Steam sign-in callback...",
            "Cancel",
            0,
            0,
            self,
        )
        progress.setWindowTitle("Steam Sign-In")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.show()
        result_box: dict[str, object] = {}

        def _worker() -> None:
            result_box["result"] = authenticate_steam_openid(timeout_s=300)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        canceled = False
        while thread.is_alive():
            QApplication.processEvents()
            if progress.wasCanceled():
                canceled = True
                break
            time.sleep(0.05)
        progress.close()
        if canceled:
            QMessageBox.information(self, "Steam Sign-In", "Steam sign-in canceled.")
            return

        result = result_box.get("result")
        steam_id = str(getattr(result, "steam_id", "") or "").strip()
        if not bool(getattr(result, "success", False)) or not steam_id:
            err = str(getattr(result, "error", "") or "Steam authentication failed.")
            QMessageBox.warning(self, "Steam Sign-In", err)
            return

        open_key_page = QMessageBox.question(
            self,
            "Steam API Key",
            (
                "Steam sign-in succeeded.\n\n"
                "Enter your Steam Web API key so GameManager can import your full owned Steam library."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if open_key_page == QMessageBox.StandardButton.Yes:
            try:
                webbrowser.open("https://steamcommunity.com/dev/apikey", new=2)
            except Exception:
                pass
        api_key, ok = QInputDialog.getText(
            self,
            "Steam API Key",
            "Steam Web API key:",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return
        api_key = str(api_key or "").strip()
        if not api_key:
            QMessageBox.warning(
                self,
                "Steam API Key",
                "API key is required to import the full owned Steam library.",
            )
            return
        account = self.state.connect_store_account(
            "Steam",
            {
                "account_id": steam_id,
                "display_name": steam_id,
                "steam_api_key": api_key,
            },
        )
        if account is None:
            QMessageBox.warning(
                self,
                "Steam Sign-In",
                "Could not connect Steam account.",
            )
            return
        self._show_connect_and_sync_result("Steam", account.account_id)
        self._reload()

    def _on_connect_egs(self) -> None:
        payload = self._collect_egs_auth_payload()
        if payload is None:
            return
        try:
            account = self._run_modal_background(
                "Epic Sign-In",
                "Linking Epic account...",
                lambda: self.state.connect_store_account(
                    "EGS",
                    payload,
                ),
            )
        except Exception as exc:
            self._handle_egs_connect_exception(exc)
            err_type = type(exc).__name__
            err_msg = str(exc)
            cause = getattr(exc, "__cause__", None)
            if cause is not None:
                err_msg = f"{err_msg}\nCaused by ({type(cause).__name__}): {cause}"
            QMessageBox.warning(
                self,
                "Epic Sign-In",
                f"Epic authentication failed ({err_type}):\n{err_msg}",
            )
            return
        if account is None:
            QMessageBox.warning(
                self,
                "Epic Sign-In",
                "Could not connect Epic account.",
            )
            return
        self._show_connect_and_sync_result("EGS", account.account_id)
        self._reload()

    def _on_connect_gog(self) -> None:
        try:
            webbrowser.open(GOG_ACCOUNT_PAGE_URL, new=2)
            webbrowser.open(GOG_ACCOUNT_BASIC_URL, new=2)
        except Exception:
            pass
        auth_value, ok = QInputDialog.getMultiLineText(
            self,
            "GOG Account Data",
            "\n".join(
                [
                    "Paste one of the following:",
                    "",
                    f"1) JSON from {GOG_ACCOUNT_BASIC_URL} (recommended)",
                    "2) Public GOG account name (fallback, requires public profile)",
                ]
            ),
        )
        if not ok:
            return
        payload = _gog_auth_payload_from_text(str(auth_value or ""))
        if not payload:
            QMessageBox.warning(
                self,
                "GOG Sign-In",
                "GOG account data is required.",
            )
            return
        try:
            account = self._run_modal_background(
                "GOG Sign-In",
                "Linking GOG account...",
                lambda: self.state.connect_store_account(
                    "GOG",
                    payload,
                ),
            )
        except Exception as exc:
            err_type = type(exc).__name__
            QMessageBox.warning(
                self,
                "GOG Sign-In",
                f"GOG authentication failed ({err_type}):\n{exc}",
            )
            return
        if account is None:
            QMessageBox.warning(
                self,
                "GOG Sign-In",
                "Could not connect GOG account.",
            )
            return
        self._show_connect_and_sync_result("GOG", account.account_id)
        self._reload()

    def _on_connect_itch(self) -> None:
        launch_client = QMessageBox.question(
            self,
            "Itch.io Sign-In",
            "\n".join(
                [
                    "Open itch.io client now before linking?",
                    "",
                    "Choose Yes if you need to sign in first.",
                ]
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        payload = {"launch_client": "1" if launch_client == QMessageBox.StandardButton.Yes else "0"}
        try:
            account = self._run_modal_background(
                "Itch.io Sign-In",
                "Linking itch.io account...",
                lambda: self.state.connect_store_account("Itch.io", payload),
            )
        except Exception as exc:
            err_type = type(exc).__name__
            QMessageBox.warning(
                self,
                "Itch.io Sign-In",
                f"Itch.io authentication failed ({err_type}):\n{exc}",
            )
            return
        if account is None:
            QMessageBox.warning(
                self,
                "Itch.io Sign-In",
                (
                    "Could not connect itch.io account.\n\n"
                    "Make sure itch app is installed, launch it, and sign in before retrying."
                ),
            )
            return
        self._show_connect_and_sync_result("Itch.io", account.account_id)
        self._reload()

    def _collect_egs_auth_payload(self) -> dict[str, str] | None:
        return self._prompt_egs_auth_payload_external()

    def _prompt_egs_auth_payload_external(self) -> dict[str, str] | None:
        try:
            webbrowser.open(EPIC_LOGIN_AUTH_CODE_URL, new=2)
        except Exception:
            pass
        auth_value, ok = QInputDialog.getText(
            self,
            "Epic Authorization Data",
            "\n".join(
                [
                    "Paste Epic authorization code / redirect URL / JSON:",
                    "",
                    "If Epic shows 'Review Security Settings is required',",
                    "complete security review and retry.",
                ]
            ),
            QLineEdit.EchoMode.Normal,
        )
        if not ok:
            return None
        payload = _epic_auth_payload_from_text(str(auth_value or ""))
        clean = {
            key: str(value).strip()
            for key, value in payload.items()
            if str(value).strip()
        }
        if not clean:
            QMessageBox.warning(
                self,
                "Epic Authorization Data",
                "Authorization data is required.",
            )
            return None
        return clean

    def _handle_egs_connect_exception(self, exc: Exception) -> None:
        msg = str(exc)
        lowered = msg.casefold()
        if (
            "review_security_settings" in lowered
            or "corrective action" in lowered
            or "sid conflict" in lowered
        ):
            follow_up = QMessageBox.question(
                self,
                "Epic Security Review Required",
                "\n".join(
                    [
                        "Epic requires account security/session review before OAuth can continue.",
                        "",
                        "Open Epic Security Settings now?",
                    ]
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if follow_up == QMessageBox.StandardButton.Yes:
                try:
                    webbrowser.open(EPIC_SECURITY_SETTINGS_URL, new=2)
                except Exception:
                    pass

    def _show_connect_and_sync_result(self, store_name: str, account_id: str) -> None:
        inventory = list(getattr(self.parent(), "inventory", []))
        launch_prefs = self._collect_launcher_start_preferences()
        force_rebuild_all = self.force_rebuild_chk.isChecked()
        def _run(progress_cb, should_cancel):
            results = self.state.sync_store_accounts(
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                launch_client_by_store=launch_prefs,
            )
            if should_cancel():
                return results, 0
            linked_count = self.state.rebuild_store_links_from_inventory(
                list(inventory),
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                force_rebuild_all=force_rebuild_all,
            )
            return results, linked_count

        sync_out, canceled = self._run_modal_background_with_progress(
            "Storefront Sync",
            "Syncing accounts and rebuilding links...",
            _run,
        )
        results = list(sync_out[0] if isinstance(sync_out, tuple) else [])
        linked = int(sync_out[1] if isinstance(sync_out, tuple) else 0)
        ok_count = sum(1 for row in results if str(row.get("status", "")).casefold() == "ok")
        fail_count = sum(1 for row in results if str(row.get("status", "")).casefold() == "failed")
        ok_accounts, failed_accounts, other_accounts = self._sync_result_account_groups(results)
        imported_total = sum(int(row.get("imported_count", 0) or 0) for row in results)
        if self._after_sync_callback is not None:
            self._after_sync_callback()
        summary_lines = [
            f"Linked {store_name} account: {account_id}",
            f"Canceled: {'Yes' if canceled else 'No'}",
            f"Accounts synced: {len(results)}",
            f"Successful: {ok_count}",
            f"Failed: {fail_count}",
            f"Successful accounts: {', '.join(ok_accounts) if ok_accounts else 'None'}",
            f"Failed accounts: {', '.join(failed_accounts) if failed_accounts else 'None'}",
            f"Entitlements imported: {imported_total}",
            f"Strict links rebuilt: {linked}",
        ]
        if other_accounts:
            summary_lines.insert(7, f"Other status accounts: {', '.join(other_accounts)}")
        summary = "\n".join(summary_lines)
        if canceled or fail_count > 0:
            QMessageBox.warning(self, "Storefront Accounts", summary)
        else:
            self._show_success_popup("Storefront Accounts", summary)

    def _on_disconnect_selected(self) -> None:
        store_name = self._selected_store_name()
        if not store_name:
            QMessageBox.information(self, "Storefront Accounts", "Select a store row first.")
            return
        accounts = [
            account for account in self.state.list_store_accounts()
            if normalize_store_name(account.store_name) == store_name
        ]
        if not accounts:
            QMessageBox.information(
                self,
                "Storefront Accounts",
                f"No linked accounts for {store_name}.",
            )
            return
        options = [account.account_id for account in accounts]
        account_id = options[0]
        if len(options) > 1:
            selected, ok = QInputDialog.getItem(
                self,
                "Disconnect Store Account",
                "Account:",
                options,
                0,
                False,
            )
            if not ok:
                return
            account_id = str(selected or "").strip()
            if not account_id:
                return
        self.state.disconnect_store_account(store_name, account_id)
        QMessageBox.information(
            self,
            "Storefront Accounts",
            f"Disconnected {store_name} account: {account_id}",
        )
        self._reload()

    def _on_sync_enabled(self) -> None:
        inventory = list(getattr(self.parent(), "inventory", []))
        launch_prefs = self._collect_launcher_start_preferences()
        force_rebuild_all = self.force_rebuild_chk.isChecked()
        def _run(progress_cb, should_cancel):
            results = self.state.sync_store_accounts(
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                launch_client_by_store=launch_prefs,
            )
            if should_cancel():
                return results, 0
            linked_count = self.state.rebuild_store_links_from_inventory(
                list(inventory),
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                force_rebuild_all=force_rebuild_all,
            )
            return results, linked_count

        sync_out, canceled = self._run_modal_background_with_progress(
            "Storefront Sync",
            "Syncing accounts and rebuilding links...",
            _run,
        )
        results = list(sync_out[0] if isinstance(sync_out, tuple) else [])
        linked = int(sync_out[1] if isinstance(sync_out, tuple) else 0)
        ok_count = sum(1 for row in results if str(row.get("status", "")).casefold() == "ok")
        fail_count = sum(1 for row in results if str(row.get("status", "")).casefold() == "failed")
        ok_accounts, failed_accounts, other_accounts = self._sync_result_account_groups(results)
        imported_total = sum(int(row.get("imported_count", 0) or 0) for row in results)
        if self._after_sync_callback is not None:
            self._after_sync_callback()
        summary_lines = [
            f"Canceled: {'Yes' if canceled else 'No'}",
            f"Accounts processed: {len(results)}",
            f"Successful: {ok_count}",
            f"Failed: {fail_count}",
            f"Successful accounts: {', '.join(ok_accounts) if ok_accounts else 'None'}",
            f"Failed accounts: {', '.join(failed_accounts) if failed_accounts else 'None'}",
            f"Entitlements imported: {imported_total}",
            f"Strict links rebuilt: {linked}",
        ]
        if other_accounts:
            summary_lines.insert(6, f"Other status accounts: {', '.join(other_accounts)}")
        summary = "\n".join(summary_lines)
        if canceled or fail_count > 0:
            QMessageBox.warning(self, "Storefront Sync", summary)
        else:
            self._show_success_popup("Storefront Sync", summary)
        self._reload()

    def _on_rebuild_links(self) -> None:
        inventory = list(getattr(self.parent(), "inventory", []))
        force_rebuild_all = self.force_rebuild_chk.isChecked()
        result, canceled = self._run_modal_background_with_progress(
            "Store Ownership Links",
            "Rebuilding strict ownership links...",
            lambda progress_cb, should_cancel: self.state.rebuild_store_links_from_inventory(
                list(inventory),
                progress_cb=progress_cb,
                should_cancel=should_cancel,
                force_rebuild_all=force_rebuild_all,
            ),
        )
        linked = int(result or 0)
        if self._after_sync_callback is not None:
            self._after_sync_callback()
        summary = "\n".join(
            [
                f"Canceled: {'Yes' if canceled else 'No'}",
                f"Rebuilt strict ownership links: {linked}",
            ]
        )
        if canceled:
            QMessageBox.warning(self, "Store Ownership Links", summary)
        else:
            self._show_success_popup("Store Ownership Links", summary)
