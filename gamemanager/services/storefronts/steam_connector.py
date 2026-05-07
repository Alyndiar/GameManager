from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Callable
from urllib import parse, request

from gamemanager.services.storefronts.base import (
    StoreAuthResult,
    StoreEntitlement,
    StorePlugin,
)
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


_KV_RE = re.compile(r'^\s*"(?P<key>[^"]+)"\s+"(?P<value>.*)"\s*$')
_STEAM_OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"


def _parse_vdf_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = _KV_RE.match(line)
            if not match:
                continue
            key = str(match.group("key") or "").strip()
            value = str(match.group("value") or "").strip()
            if key:
                values[key.casefold()] = value.replace("\\\\", "\\")
    except OSError:
        return {}
    return values


def _steam_library_paths() -> list[Path]:
    candidates = [
        Path(r"C:\Program Files (x86)\Steam"),
        Path(r"C:\Program Files\Steam"),
    ]
    libraries: list[Path] = []
    for root in candidates:
        steamapps = root / "steamapps"
        if steamapps.exists() and steamapps.is_dir():
            libraries.append(steamapps)
            lib_vdf = steamapps / "libraryfolders.vdf"
            if lib_vdf.exists():
                parsed = _parse_vdf_key_values(lib_vdf)
                for key, value in parsed.items():
                    if key != "path":
                        continue
                    extra = Path(value) / "steamapps"
                    if extra.exists() and extra.is_dir():
                        libraries.append(extra)
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in libraries:
        token = str(path).casefold()
        if token in seen:
            continue
        seen.add(token)
        deduped.append(path)
    return deduped


def _owned_games_from_api(account_id: str, api_key: str) -> list[StoreEntitlement]:
    params = {
        "key": str(api_key or "").strip(),
        "steamid": str(account_id or "").strip(),
        "include_appinfo": "true",
        "include_played_free_games": "true",
        "format": "json",
    }
    if not params["key"] or not params["steamid"]:
        return []
    query = parse.urlencode(params)
    target = f"{_STEAM_OWNED_GAMES_URL}?{query}"
    req = request.Request(
        target,
        headers={"User-Agent": "GameManager/1.0"},
        method="GET",
    )
    with request.urlopen(req, timeout=25) as response:
        payload = response.read()
    decoded = json.loads(payload.decode("utf-8", errors="ignore"))
    rows = decoded.get("response", {}).get("games", [])
    out: list[StoreEntitlement] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        appid = str(row.get("appid") or "").strip()
        title = str(row.get("name") or "").strip() or f"Steam App {appid}"
        if not appid or not title:
            continue
        if appid in seen:
            continue
        seen.add(appid)
        out.append(
            StoreEntitlement(
                entitlement_id=appid,
                title=title,
                store_game_id=appid,
                manifest_id=appid,
                is_installed=False,
            )
        )
    return out


def _owned_games_from_installed_manifests() -> list[StoreEntitlement]:
    out: list[StoreEntitlement] = []
    seen: set[str] = set()
    for steamapps in _steam_library_paths():
        for manifest in steamapps.glob("appmanifest_*.acf"):
            parsed = _parse_vdf_key_values(manifest)
            appid = str(parsed.get("appid", "")).strip()
            name = str(parsed.get("name", "")).strip()
            installdir = str(parsed.get("installdir", "")).strip()
            if not appid or not name:
                continue
            if appid in seen:
                continue
            seen.add(appid)
            install_path = ""
            if installdir:
                install_path = str((steamapps / "common" / installdir).resolve())
            out.append(
                StoreEntitlement(
                    entitlement_id=appid,
                    title=name,
                    store_game_id=appid,
                    manifest_id=appid,
                    install_path=install_path,
                    is_installed=True,
                )
            )
    return out


class SteamConnector(StubLauncherConnector):
    store_name = "Steam"

    def connect(self, auth_payload: dict[str, str] | None = None) -> StoreAuthResult:
        payload = dict(auth_payload or {})
        account_id = str(payload.get("account_id", "")).strip()
        display_name = str(payload.get("display_name", "")).strip() or account_id
        api_key = str(payload.get("steam_api_key", "")).strip()
        if not account_id:
            return StoreAuthResult(
                success=False,
                status="missing_account_id",
                message="Steam connector requires account id.",
            )
        if not api_key:
            return StoreAuthResult(
                success=False,
                status="missing_api_key",
                message="Steam connector requires Steam Web API key.",
            )
        return StoreAuthResult(
            success=True,
            account_id=account_id,
            display_name=display_name,
            auth_kind="steam_openid_api_key",
            token_secret=api_key,
            status="connected",
            message="Steam account linked with API key.",
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
        _ = auth_payload
        if progress_cb is not None:
            progress_cb("Steam sync: local manifests", 5, 100)
        if should_cancel is not None and should_cancel():
            return []
        installed = _owned_games_from_installed_manifests()
        api_key = str(token_secret or "").strip()
        if not api_key:
            if progress_cb is not None:
                progress_cb("Steam sync: local manifests only", 100, 100)
            return installed
        if progress_cb is not None:
            progress_cb("Steam sync: owned API list", 40, 100)
        if should_cancel is not None and should_cancel():
            return installed
        api_rows = _owned_games_from_api(account_id, api_key)
        if progress_cb is not None:
            progress_cb("Steam sync: merge installed + owned", 70, 100)
        by_id: dict[str, StoreEntitlement] = {
            str(row.entitlement_id).strip(): row for row in api_rows
        }
        total = max(1, len(installed))
        for idx, row in enumerate(installed, start=1):
            if should_cancel is not None and should_cancel():
                break
            key = str(row.entitlement_id).strip()
            existing = by_id.get(key)
            if existing is None:
                by_id[key] = row
            else:
                if row.install_path and not existing.install_path:
                    existing.install_path = row.install_path
                existing.is_installed = bool(existing.is_installed or row.is_installed)
            if progress_cb is not None:
                pct = 70 + int(round((idx / total) * 30.0))
                progress_cb("Steam sync: merge installed + owned", min(100, pct), 100)
        if progress_cb is not None:
            progress_cb("Steam sync: done", 100, 100)
        return list(by_id.values())


PLUGIN = StorePlugin(
    store_name="Steam",
    connector_cls=SteamConnector,
    auth_kind="steam_web_or_api",
    supports_full_library_sync=True,
    description="Steam connector with launcher manifests and API-backed account sync.",
)
