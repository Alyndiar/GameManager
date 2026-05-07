from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import threading
import time
from typing import Any, Callable

from gamemanager.services.storefronts.base import (
    StoreAuthResult,
    StoreConnectorStatus,
    StoreEntitlement,
    StorePlugin,
)
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


_BUTLER_LISTEN_NOTIFICATION_TYPE = "butlerd/listen-notification"
_RPC_ABORT_CODES = {410, 499}


def _as_bool(value: Any) -> bool:
    token = str(value or "").strip().casefold()
    return token in {"1", "true", "yes", "y", "on"}


def _itch_user_path() -> Path:
    appdata = str(os.environ.get("APPDATA") or "").strip()
    if appdata:
        return Path(appdata) / "itch"
    return Path.home() / "AppData" / "Roaming" / "itch"


def _butler_executable_path() -> Path:
    user_path = _itch_user_path()
    chosen = user_path / "broth" / "butler" / ".chosen-version"
    if not chosen.is_file():
        return Path("")
    try:
        version = chosen.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return Path("")
    if not version:
        return Path("")
    exe = user_path / "broth" / "butler" / "versions" / version / "butler.exe"
    return exe if exe.is_file() else Path("")


def _butler_database_path() -> Path:
    user_path = _itch_user_path()
    db = user_path / "db" / "butler.db"
    return db if db.is_file() else Path("")


def _itch_client_executable_path() -> Path:
    localappdata = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if not localappdata:
        return Path("")
    install_dir = Path(localappdata) / "itch"
    state_file = install_dir / "state.json"
    if not state_file.is_file():
        return Path("")
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return Path("")
    current = str(payload.get("current") or "").strip()
    if not current:
        return Path("")
    exe = install_dir / f"app-{current}" / "itch.exe"
    return exe if exe.is_file() else Path("")


def _start_itch_client() -> bool:
    exe = _itch_client_executable_path()
    if exe.is_file():
        try:
            subprocess.Popen([str(exe)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            pass
    # Best effort deep-link fallback.
    if os.name == "nt":
        try:
            os.startfile("itch://open")  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
    return False


def _parse_datetime_sort_key(raw: str) -> tuple[int, str]:
    token = str(raw or "").strip()
    if not token:
        return (0, "")
    normalized = token.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        return (1, dt.isoformat())
    except ValueError:
        return (0, token)


def _normalize_profile(profile: dict[str, Any]) -> dict[str, str]:
    user = profile.get("user")
    user_obj = user if isinstance(user, dict) else {}
    profile_id = str(profile.get("id") or user_obj.get("id") or "").strip()
    username = str(user_obj.get("username") or "").strip()
    display_name = str(user_obj.get("displayName") or user_obj.get("display_name") or "").strip()
    last_connected = str(profile.get("lastConnected") or "").strip()
    return {
        "id": profile_id,
        "username": username,
        "display_name": display_name or username or profile_id,
        "last_connected": last_connected,
    }


def _select_profile(
    profiles: list[dict[str, str]],
    *,
    account_hint: str = "",
    username_hint: str = "",
) -> dict[str, str] | None:
    if not profiles:
        return None
    account = str(account_hint or "").strip()
    username = str(username_hint or "").strip().casefold()
    if account:
        for profile in profiles:
            if str(profile.get("id") or "").strip() == account:
                return profile
    if username:
        for profile in profiles:
            profile_username = str(profile.get("username") or "").strip().casefold()
            profile_display = str(profile.get("display_name") or "").strip().casefold()
            if profile_username == username or profile_display == username:
                return profile
    # Playnite-like behavior: choose the most recently connected profile.
    ranked = sorted(
        profiles,
        key=lambda row: (
            _parse_datetime_sort_key(row.get("last_connected", "")),
            row.get("id", ""),
        ),
        reverse=True,
    )
    return ranked[0] if ranked else None


class _ButlerRpcClient:
    def __init__(self, address: str, *, timeout_s: float = 20.0):
        host, port_token = str(address or "").strip().split(":", 1)
        self._sock = socket.create_connection((host, int(port_token)), timeout=timeout_s)
        self._sock.settimeout(timeout_s)
        self._reader = self._sock.makefile(mode="r", encoding="utf-8", newline="\n")
        self._writer = self._sock.makefile(mode="w", encoding="utf-8", newline="\n")
        self._timeout_s = timeout_s
        self._id_counter = 0
        self._lock = threading.Lock()

    def close(self) -> None:
        try:
            self._reader.close()
        except Exception:
            pass
        try:
            self._writer.close()
        except Exception:
            pass
        try:
            self._sock.close()
        except Exception:
            pass

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        allow_abort: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            self._id_counter += 1
            req_id = self._id_counter
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": str(method or "").strip(),
                "params": dict(params or {}),
            }
            self._writer.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._writer.flush()
            deadline = time.monotonic() + self._timeout_s
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(f"Itch butler RPC timeout for method '{method}'.")
                self._sock.settimeout(remaining)
                line = self._reader.readline()
                if not line:
                    raise RuntimeError("Itch butler RPC connection closed.")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if message_id != req_id:
                    # Ignore notifications / requests / responses for other ids.
                    continue
                error_obj = message.get("error")
                if isinstance(error_obj, dict):
                    code = int(error_obj.get("code") or 0)
                    text = str(error_obj.get("message") or "").strip() or f"code={code}"
                    if allow_abort and code in _RPC_ABORT_CODES:
                        return {}
                    raise RuntimeError(f"Itch butler RPC '{method}' failed: {text}")
                result = message.get("result")
                if isinstance(result, dict):
                    return result
                if result is None:
                    return {}
                if isinstance(result, list):
                    return {"items": result}
                return {"value": result}


class _ButlerSession:
    def __init__(self, *, timeout_s: float = 12.0):
        self._timeout_s = timeout_s
        self._proc: subprocess.Popen[str] | None = None
        self._rpc: _ButlerRpcClient | None = None
        self._output_queue: queue.SimpleQueue[tuple[str, str | None]] = queue.SimpleQueue()
        self._stderr_tail: list[str] = []

    @staticmethod
    def _pump_stream(
        source: str,
        stream,
        out_queue: queue.SimpleQueue[tuple[str, str | None]],
    ) -> None:
        try:
            for line in iter(stream.readline, ""):
                if line == "":
                    break
                out_queue.put((source, line.rstrip("\r\n")))
        finally:
            out_queue.put((source, None))

    def __enter__(self) -> "_ButlerSession":
        exe = _butler_executable_path()
        db_path = _butler_database_path()
        if not exe.is_file():
            raise RuntimeError("Itch butler executable was not found.")
        if not db_path.is_file():
            raise RuntimeError("Itch butler database was not found.")

        self._proc = subprocess.Popen(
            [
                str(exe),
                "daemon",
                "--json",
                "--dbpath",
                str(db_path),
                "--transport",
                "tcp",
                "--keep-alive",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            bufsize=1,
        )
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        threading.Thread(
            target=self._pump_stream,
            args=("stdout", self._proc.stdout, self._output_queue),
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump_stream,
            args=("stderr", self._proc.stderr, self._output_queue),
            daemon=True,
        ).start()

        deadline = time.monotonic() + self._timeout_s
        endpoint = ""
        secret = ""
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                break
            try:
                source, line = self._output_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                continue
            if source == "stderr":
                if line.strip():
                    self._stderr_tail.append(line.strip())
                    self._stderr_tail = self._stderr_tail[-8:]
                continue
            text = str(line or "").strip()
            if not text.startswith("{"):
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if str(payload.get("type") or "").strip() != _BUTLER_LISTEN_NOTIFICATION_TYPE:
                continue
            tcp = payload.get("tcp")
            tcp_obj = tcp if isinstance(tcp, dict) else {}
            endpoint = str(tcp_obj.get("address") or "").strip()
            secret = str(payload.get("secret") or "").strip()
            if endpoint and secret:
                break

        if not endpoint or not secret:
            message = "Itch butler daemon failed to provide listen endpoint."
            if self._stderr_tail:
                message += " " + " | ".join(self._stderr_tail)
            raise RuntimeError(message)

        self._rpc = _ButlerRpcClient(endpoint, timeout_s=max(20.0, self._timeout_s))
        self._rpc.request("Meta.Authenticate", {"secret": secret})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._rpc is not None:
            try:
                self._rpc.request("Meta.Shutdown", {}, allow_abort=True)
            except Exception:
                pass
            self._rpc.close()
        if self._proc is not None:
            try:
                self._proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        allow_abort: bool = False,
    ) -> dict[str, Any]:
        if self._rpc is None:
            raise RuntimeError("Itch butler session is not initialized.")
        return self._rpc.request(method, params, allow_abort=allow_abort)


def _profile_id_param(profile_id: str) -> int | str:
    token = str(profile_id or "").strip()
    return int(token) if token.isdigit() else token


def _fetch_profiles_once() -> list[dict[str, str]]:
    with _ButlerSession(timeout_s=14.0) as session:
        payload = session.request("Profile.List", {})
    rows = payload.get("profiles")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_profile(row)
        if normalized.get("id", ""):
            out.append(normalized)
    return out


def _list_profiles(
    *,
    launch_client: bool = False,
    wait_for_profiles_s: float = 0.0,
) -> list[dict[str, str]]:
    if launch_client:
        _start_itch_client()
    deadline = time.monotonic() + max(0.0, float(wait_for_profiles_s))
    last_error: Exception | None = None
    while True:
        try:
            profiles = _fetch_profiles_once()
            if profiles:
                return profiles
        except Exception as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            if last_error is not None:
                raise last_error
            return []
        time.sleep(1.25)


@dataclass(slots=True)
class _ItchOwnedSnapshot:
    owned_keys: list[dict[str, Any]]
    caves: list[dict[str, Any]]


def _fetch_owned_snapshot(
    profile_id: str,
    *,
    progress_cb: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> _ItchOwnedSnapshot:
    owned_keys: list[dict[str, Any]] = []
    caves: list[dict[str, Any]] = []
    with _ButlerSession(timeout_s=18.0) as session:
        if progress_cb is not None:
            progress_cb("Itch.io sync: fetch owned keys", 20, 100)
        if should_cancel is not None and should_cancel():
            return _ItchOwnedSnapshot(owned_keys=owned_keys, caves=caves)
        params = {"profileId": _profile_id_param(profile_id)}
        payload = session.request("Fetch.ProfileOwnedKeys", params, allow_abort=True)
        if bool(payload.get("stale")):
            fresh_params = dict(params)
            fresh_params["fresh"] = True
            payload = session.request("Fetch.ProfileOwnedKeys", fresh_params, allow_abort=True)
        rows = payload.get("items")
        if isinstance(rows, list):
            owned_keys = [row for row in rows if isinstance(row, dict)]

        if progress_cb is not None:
            progress_cb("Itch.io sync: fetch installed caves", 60, 100)
        if should_cancel is not None and should_cancel():
            return _ItchOwnedSnapshot(owned_keys=owned_keys, caves=caves)
        cursor = ""
        page = 0
        while True:
            if should_cancel is not None and should_cancel():
                break
            page += 1
            cave_params: dict[str, Any] = {}
            if cursor:
                cave_params["cursor"] = cursor
            cave_payload = session.request("Fetch.Caves", cave_params, allow_abort=True)
            cave_items = cave_payload.get("items")
            if isinstance(cave_items, list):
                caves.extend([row for row in cave_items if isinstance(row, dict)])
            cursor = str(cave_payload.get("nextCursor") or "").strip()
            if progress_cb is not None:
                progress_cb(f"Itch.io sync: installed caves page {page}", 60 + min(30, page * 5), 100)
            if not cursor:
                break
    return _ItchOwnedSnapshot(owned_keys=owned_keys, caves=caves)


def _store_game_id_from_url(url: str, fallback_game_id: str) -> str:
    token = str(url or "").strip()
    if token.startswith("http://") or token.startswith("https://"):
        return token
    return str(fallback_game_id or "").strip()


def _owned_keys_to_entitlements(rows: list[dict[str, Any]]) -> dict[str, StoreEntitlement]:
    by_game_id: dict[str, StoreEntitlement] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key_id = str(row.get("id") or "").strip()
        game = row.get("game")
        game_obj = game if isinstance(game, dict) else {}
        game_id = str(game_obj.get("id") or row.get("gameId") or "").strip()
        title = str(game_obj.get("title") or "").strip()
        if not game_id or not title:
            continue
        game_url = str(game_obj.get("url") or "").strip()
        metadata = {
            "game_id": game_id,
            "key_id": key_id,
            "url": game_url,
            "cover_url": str(game_obj.get("coverUrl") or "").strip(),
            "still_cover_url": str(game_obj.get("stillCoverUrl") or "").strip(),
            "classification": str(game_obj.get("classification") or "").strip(),
            "user_id": str(game_obj.get("userId") or "").strip(),
            "owner_id": str(row.get("ownerId") or "").strip(),
        }
        by_game_id[game_id] = StoreEntitlement(
            entitlement_id=game_id,
            title=title,
            store_game_id=_store_game_id_from_url(game_url, game_id),
            manifest_id=game_id,
            is_installed=False,
            metadata_json=json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        )
    return by_game_id


def _merge_caves_into_entitlements(
    by_game_id: dict[str, StoreEntitlement],
    caves: list[dict[str, Any]],
) -> list[StoreEntitlement]:
    for cave in caves:
        if not isinstance(cave, dict):
            continue
        game = cave.get("game")
        game_obj = game if isinstance(game, dict) else {}
        game_id = str(game_obj.get("id") or "").strip()
        title = str(game_obj.get("title") or "").strip()
        if not game_id:
            continue
        install_info = cave.get("installInfo")
        install_obj = install_info if isinstance(install_info, dict) else {}
        install_path = str(install_obj.get("installFolder") or "").strip()
        if not install_path:
            install_path = str(install_obj.get("installLocation") or "").strip()
        existing = by_game_id.get(game_id)
        if existing is None:
            game_url = str(game_obj.get("url") or "").strip()
            by_game_id[game_id] = StoreEntitlement(
                entitlement_id=game_id,
                title=title or f"Itch Game {game_id}",
                store_game_id=_store_game_id_from_url(game_url, game_id),
                manifest_id=game_id,
                install_path=install_path,
                is_installed=bool(install_path),
                metadata_json=json.dumps(
                    {
                        "game_id": game_id,
                        "url": game_url,
                        "cave_id": str(cave.get("id") or "").strip(),
                        "source": "fetch_caves_only",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            continue
        if install_path:
            existing.install_path = install_path
            existing.is_installed = True
        elif not existing.install_path:
            existing.is_installed = bool(existing.is_installed)
    return list(by_game_id.values())


class ItchConnector(StubLauncherConnector):
    store_name = "Itch.io"

    def connect(self, auth_payload: dict[str, str] | None = None) -> StoreAuthResult:
        payload = dict(auth_payload or {})
        hint = str(payload.get("account_id") or payload.get("profile_id") or "").strip()
        username_hint = str(payload.get("username") or payload.get("display_name") or "").strip()
        launch_client = _as_bool(payload.get("launch_client") or "0")

        if not _butler_executable_path().is_file() or not _butler_database_path().is_file():
            return StoreAuthResult(
                success=False,
                status="itch_not_installed",
                message=(
                    "Itch app (with butler runtime) was not found. "
                    "Install itch.io app and sign in first."
                ),
            )

        wait_s = 35.0 if launch_client else 0.0
        try:
            profiles = _list_profiles(
                launch_client=launch_client,
                wait_for_profiles_s=wait_s,
            )
        except Exception as exc:
            return StoreAuthResult(
                success=False,
                status="profile_query_failed",
                message=f"Could not read itch.io launcher profiles: {exc}",
            )

        profile = _select_profile(profiles, account_hint=hint, username_hint=username_hint)
        if profile is None:
            return StoreAuthResult(
                success=False,
                status="missing_profile",
                message=(
                    "No logged-in itch.io profile was found in the local launcher. "
                    "Open itch.io app, sign in, then retry."
                ),
            )

        profile_id = str(profile.get("id") or "").strip()
        display_name = (
            str(profile.get("display_name") or "").strip()
            or str(profile.get("username") or "").strip()
            or profile_id
        )
        return StoreAuthResult(
            success=True,
            account_id=profile_id,
            display_name=display_name,
            auth_kind="launcher_profile_owned_keys",
            status="connected",
            message="itch.io launcher profile linked.",
        )

    def refresh_entitlements(
        self,
        account_id: str,
        *,
        token_secret: str = "",
        auth_payload: dict[str, str] | None = None,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[StoreEntitlement]:
        _ = token_secret
        payload = dict(auth_payload or {})
        account_hint = str(account_id or payload.get("account_id") or "").strip()
        username_hint = str(payload.get("username") or payload.get("display_name") or "").strip()
        launch_client = _as_bool(payload.get("launch_client") or "0")
        if progress_cb is not None:
            progress_cb("Itch.io sync: resolve profile", 5, 100)
        if should_cancel is not None and should_cancel():
            return []
        profiles = _list_profiles(
            launch_client=launch_client,
            wait_for_profiles_s=20.0 if launch_client else 0.0,
        )
        profile = _select_profile(profiles, account_hint=account_hint, username_hint=username_hint)
        if profile is None:
            if progress_cb is not None:
                progress_cb("Itch.io sync: no launcher profile", 100, 100)
            return []
        profile_id = str(profile.get("id") or "").strip()
        if not profile_id:
            if progress_cb is not None:
                progress_cb("Itch.io sync: invalid profile", 100, 100)
            return []

        snapshot = _fetch_owned_snapshot(
            profile_id,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
        )
        if should_cancel is not None and should_cancel():
            return _merge_caves_into_entitlements(
                _owned_keys_to_entitlements(snapshot.owned_keys),
                snapshot.caves,
            )
        if progress_cb is not None:
            progress_cb("Itch.io sync: merge library + installs", 95, 100)
        entitlements = _merge_caves_into_entitlements(
            _owned_keys_to_entitlements(snapshot.owned_keys),
            snapshot.caves,
        )
        if progress_cb is not None:
            progress_cb("Itch.io sync: done", 100, 100)
        return entitlements

    def status(self, account_id: str = "") -> StoreConnectorStatus:
        available = bool(_butler_executable_path().is_file() and _butler_database_path().is_file())
        connected = available and bool(str(account_id or "").strip())
        message = (
            "Playnite-style itch.io integration via local butler daemon "
            "(Profile.List + Fetch.ProfileOwnedKeys + Fetch.Caves)."
        )
        if not available:
            message = "Itch app/butler runtime not found. Install itch.io app and sign in."
        return StoreConnectorStatus(
            available=available,
            connected=connected,
            auth_kind="launcher_profile_owned_keys",
            message=message,
            metadata={"store_name": self.store_name},
        )


PLUGIN = StorePlugin(
    store_name="Itch.io",
    connector_cls=ItchConnector,
    auth_kind="launcher_profile_owned_keys",
    supports_full_library_sync=True,
    description=(
        "itch.io connector using Playnite-style local butler daemon for profile-owned keys "
        "and installed cave merge."
    ),
)
