from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from gamemanager.models import SgdbIconAsset
from gamemanager.services.icon_sources import IconSearchSettings


@dataclass(slots=True)
class SgdbGameDetails:
    game_id: int
    title: str
    steam_appid: str | None = None


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "GameManager/1.0"})
    return session


def _headers(settings: IconSearchSettings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.steamgriddb_api_key.strip()}"}


def _safe_get_json(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    timeout: float,
    params: dict[str, object] | None = None,
) -> dict[str, Any]:
    response = session.get(url, headers=headers, timeout=timeout, params=params)
    response.raise_for_status()
    payload = response.json() if response.content else {}
    if not isinstance(payload, dict):
        raise ValueError("Unexpected SteamGridDB payload.")
    return payload


def _extract_error_messages(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    errors = payload.get("errors")
    if isinstance(errors, list):
        for value in errors:
            token = str(value or "").strip()
            if token:
                out.append(token)
    message = str(payload.get("message") or "").strip()
    if message:
        out.append(message)
    return out


def _extract_steam_appid(payload: Any, path: str = "") -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_cf = str(key).casefold()
            next_path = f"{path}.{key_cf}" if path else key_cf
            if key_cf in {"steam_appid", "steamappid", "steam_id", "steamid"}:
                token = str(value or "").strip()
                if token.isdigit():
                    return token
            if "steam" in path and key_cf in {"id", "appid", "app_id"}:
                token = str(value or "").strip()
                if token.isdigit():
                    return token
            nested = _extract_steam_appid(value, next_path)
            if nested:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _extract_steam_appid(value, path)
            if nested:
                return nested
    return None


def _details_from_payload(payload: dict[str, Any]) -> SgdbGameDetails:
    node = payload.get("data")
    if not isinstance(node, dict):
        raise ValueError("SteamGridDB response missing game data.")
    game_id = int(node.get("id") or 0)
    if game_id <= 0:
        raise ValueError("SteamGridDB response missing game id.")
    title = str(node.get("name") or "").strip() or f"Game {game_id}"
    steam_appid = _extract_steam_appid(node)
    return SgdbGameDetails(game_id=game_id, title=title, steam_appid=steam_appid)


def search_games(
    settings: IconSearchSettings,
    term: str,
    limit: int = 10,
) -> list[SgdbGameDetails]:
    query = str(term or "").strip()
    if not query:
        return []
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        return []
    session = _session()
    url = f"{settings.steamgriddb_api_base.rstrip('/')}/search/autocomplete/{requests.utils.quote(query)}"
    payload = _safe_get_json(
        session,
        url,
        _headers(settings),
        settings.timeout_seconds,
    )
    out: list[SgdbGameDetails] = []
    for row in (payload.get("data") or [])[: max(1, int(limit))]:
        if not isinstance(row, dict):
            continue
        game_id = int(row.get("id") or 0)
        if game_id <= 0:
            continue
        title = str(row.get("name") or "").strip() or f"Game {game_id}"
        out.append(
            SgdbGameDetails(
                game_id=game_id,
                title=title,
                steam_appid=_extract_steam_appid(row),
            )
        )
    return out


def resolve_game_by_id(
    settings: IconSearchSettings,
    game_id: int,
) -> SgdbGameDetails:
    if int(game_id) <= 0:
        raise ValueError("Invalid game id.")
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        raise RuntimeError("SteamGridDB is not configured.")
    session = _session()
    url = f"{settings.steamgriddb_api_base.rstrip('/')}/games/id/{int(game_id)}"
    payload = _safe_get_json(
        session,
        url,
        _headers(settings),
        settings.timeout_seconds,
    )
    return _details_from_payload(payload)


def resolve_game_by_platform_id(
    settings: IconSearchSettings,
    platform: str,
    platform_id: str,
) -> SgdbGameDetails:
    platform_token = str(platform or "").strip().casefold()
    platform_id_token = str(platform_id or "").strip()
    if not platform_token or not platform_id_token:
        raise ValueError("Invalid platform game lookup input.")
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        raise RuntimeError("SteamGridDB is not configured.")
    session = _session()
    url = (
        f"{settings.steamgriddb_api_base.rstrip('/')}/games/"
        f"{requests.utils.quote(platform_token)}/{requests.utils.quote(platform_id_token)}"
    )
    payload = _safe_get_json(
        session,
        url,
        _headers(settings),
        settings.timeout_seconds,
    )
    return _details_from_payload(payload)


def list_game_icons(
    settings: IconSearchSettings,
    game_id: int,
    *,
    limit: int = 50,
    max_pages: int = 2,
) -> list[SgdbIconAsset]:
    if int(game_id) <= 0:
        return []
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        return []
    session = _session()
    out: list[SgdbIconAsset] = []
    base_url = f"{settings.steamgriddb_api_base.rstrip('/')}/icons/game/{int(game_id)}"
    page_cap = max(1, min(10, int(max_pages)))
    per_page = max(1, min(50, int(limit)))
    for page in range(0, page_cap):
        payload = _safe_get_json(
            session,
            base_url,
            _headers(settings),
            settings.timeout_seconds,
            params={"limit": per_page, "page": page},
        )
        rows = payload.get("data") or []
        if not isinstance(rows, list) or not rows:
            break
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            icon_id = int(row.get("id") or 0)
            url = str(row.get("url") or "").strip()
            if icon_id <= 0 or not url:
                continue
            thumb = str(row.get("thumb") or url).strip() or url
            author = row.get("author") if isinstance(row.get("author"), dict) else {}
            out.append(
                SgdbIconAsset(
                    icon_id=icon_id,
                    url=url,
                    thumb_url=thumb,
                    author_name=str(author.get("name") or "").strip(),
                    author_steam64=str(author.get("steam64") or "").strip(),
                )
            )
            added += 1
        if added <= 0:
            break
        if len(rows) < per_page:
            break
    return out


def download_image_bytes(
    image_url: str,
    timeout_seconds: float = 20.0,
) -> bytes:
    session = _session()
    response = session.get(str(image_url or "").strip(), timeout=timeout_seconds)
    response.raise_for_status()
    return response.content


def upload_icon(
    settings: IconSearchSettings,
    game_id: int,
    png_bytes: bytes,
) -> None:
    if int(game_id) <= 0:
        raise ValueError("Invalid game id.")
    if not png_bytes:
        raise ValueError("Icon upload payload is empty.")
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        raise RuntimeError("SteamGridDB is not configured.")
    session = _session()
    url = f"{settings.steamgriddb_api_base.rstrip('/')}/icons"
    data = {
        "game_id": int(game_id),
        # SGDB requires explicit style for icon uploads.
        "style": "custom",
    }
    files = {
        "asset": (
            "icon.png",
            png_bytes,
            "image/png",
        )
    }
    response = session.post(
        url,
        headers=_headers(settings),
        data=data,
        files=files,
        timeout=settings.timeout_seconds,
    )
    if int(response.status_code) >= 400:
        detail = ""
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        errors = _extract_error_messages(payload)
        if errors:
            detail = "; ".join(errors)
        else:
            detail = str(getattr(response, "text", "") or "").strip()
        if not detail:
            detail = f"HTTP {int(response.status_code)}"
        raise RuntimeError(
            f"SteamGridDB upload failed ({int(response.status_code)}): {detail}"
        )
    if response.content:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and payload.get("success") is False:
            errors = _extract_error_messages(payload)
            detail = "; ".join(errors) if errors else "API returned success=false."
            raise RuntimeError(f"SteamGridDB upload failed: {detail}")


def delete_icons(
    settings: IconSearchSettings,
    icon_ids: list[int] | tuple[int, ...],
) -> None:
    normalized = [int(value) for value in icon_ids if int(value) > 0]
    if not normalized:
        return
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        raise RuntimeError("SteamGridDB is not configured.")
    token = ",".join(str(value) for value in normalized)
    url = f"{settings.steamgriddb_api_base.rstrip('/')}/icons/{token}"
    session = _session()
    response = session.delete(url, headers=_headers(settings), timeout=settings.timeout_seconds)
    response.raise_for_status()
