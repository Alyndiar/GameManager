from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
import re
from typing import Any
from urllib.parse import quote

import requests

from gamemanager.models import IconCandidate


DEFAULT_STEAMGRIDDB_API_BASE = "https://www.steamgriddb.com/api/v2"
DEFAULT_ICONFINDER_API_BASE = "https://api.iconfinder.com/v4"
SUPPORTED_SGDB_RESOURCES: tuple[str, ...] = ("icons", "logos", "grids", "heroes")
DEFAULT_SGDB_RESOURCE_ORDER: tuple[str, ...] = ("icons", "logos", "grids", "heroes")
DEFAULT_SGDB_ENABLED_RESOURCES: tuple[str, ...] = ("icons", "logos")
_DIMENSIONS_RE = re.compile(r"(?<!\d)(\d{2,5})\s*[xX]\s*(\d{2,5})(?!\d)")


@dataclass(slots=True)
class IconSearchSettings:
    steamgriddb_enabled: bool
    steamgriddb_api_key: str
    steamgriddb_api_base: str = DEFAULT_STEAMGRIDDB_API_BASE
    iconfinder_enabled: bool = True
    iconfinder_api_key: str = ""
    iconfinder_api_base: str = DEFAULT_ICONFINDER_API_BASE
    timeout_seconds: float = 15.0


def _aspect_penalty(width: int, height: int) -> float:
    if width <= 0 or height <= 0:
        return 0.0
    ratio = max(width, height) / max(1, min(width, height))
    if ratio <= 1.25:
        return 0.0
    if ratio >= 1.8:
        return 0.7
    return 0.25


def _score_candidate(candidate: IconCandidate) -> float:
    score = 0.0
    if candidate.has_alpha:
        score += 1.0
    if candidate.width > 0 and candidate.height > 0:
        score += min(candidate.width, candidate.height) / 512.0
    score -= _aspect_penalty(candidate.width, candidate.height)
    if "logo" in candidate.title.casefold() or "icon" in candidate.title.casefold():
        score += 0.3
    candidate_id = candidate.candidate_id.casefold()
    if candidate.provider == "SteamGridDB":
        if candidate_id.startswith("icons:"):
            score += 0.75
        elif candidate_id.startswith("logos:"):
            score += 0.2
    if candidate.provider == "Steam":
        if any(
            token in candidate_id
            for token in ("library_", "capsule_", "header_image", "background")
        ):
            score -= 0.9
    return score


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "GameBackupManager/1.0"})
    return s


def normalize_sgdb_resources(
    resources: Iterable[str] | None,
    *,
    default_enabled_only: bool = True,
) -> list[str]:
    if resources is None:
        if default_enabled_only:
            return list(DEFAULT_SGDB_ENABLED_RESOURCES)
        return list(DEFAULT_SGDB_RESOURCE_ORDER)
    seen: set[str] = set()
    normalized: list[str] = []
    for value in resources:
        key = str(value).strip().casefold()
        if not key or key in seen or key not in SUPPORTED_SGDB_RESOURCES:
            continue
        seen.add(key)
        normalized.append(key)
    if normalized:
        return normalized
    if default_enabled_only:
        return list(DEFAULT_SGDB_ENABLED_RESOURCES)
    return list(DEFAULT_SGDB_RESOURCE_ORDER)


def _safe_get_json(
    session: requests.Session,
    url: str,
    headers: dict[str, str] | None,
    timeout: float,
) -> dict[str, Any] | None:
    try:
        resp = session.get(url, headers=headers, timeout=timeout)
        if resp.status_code >= 400:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _coerce_positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        value_int = int(value)
        return value_int if value_int > 0 else 0
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return parsed if parsed > 0 else 0
    return 0


def _extract_url_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in (
            "url",
            "thumb",
            "thumbnail",
            "large",
            "medium",
            "small",
            "600",
            "512",
            "256",
            "128",
        ):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _extract_dimensions_from_text(value: Any) -> tuple[int, int]:
    text = str(value or "").strip()
    if not text:
        return 0, 0
    match = _DIMENSIONS_RE.search(text)
    if not match:
        return 0, 0
    return int(match.group(1)), int(match.group(2))


def _extract_item_dimensions(
    item: dict[str, Any],
    image_url: str,
    preview_url: str,
) -> tuple[int, int]:
    width = _coerce_positive_int(item.get("width"))
    height = _coerce_positive_int(item.get("height"))
    if width and height:
        return width, height

    thumb = item.get("thumb")
    if isinstance(thumb, dict):
        width = width or _coerce_positive_int(thumb.get("width"))
        height = height or _coerce_positive_int(thumb.get("height"))
        if width and height:
            return width, height

    for key in ("dimensions", "style", "size", "mime"):
        w, h = _extract_dimensions_from_text(item.get(key))
        if w and h:
            width = width or w
            height = height or h
            if width and height:
                return width, height

    for source in (image_url, preview_url):
        w, h = _extract_dimensions_from_text(source)
        if w and h:
            width = width or w
            height = height or h
            if width and height:
                return width, height

    if width and not height:
        height = width
    elif height and not width:
        width = height
    return width, height


def _parse_positive_int_string(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value > 0 else None
    if isinstance(value, float):
        value_int = int(value)
        return str(value_int) if value_int > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            parsed = int(stripped)
            return str(parsed) if parsed > 0 else None
    return None


def _extract_steam_appid(payload: Any, path: str = "") -> str | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            key_cf = str(key).casefold()
            next_path = f"{path}.{key_cf}" if path else key_cf
            if key_cf in {"steam_appid", "steamappid", "steam_id", "steamid"}:
                appid = _parse_positive_int_string(value)
                if appid:
                    return appid
            if "steam" in path and key_cf in {"id", "appid", "app_id"}:
                appid = _parse_positive_int_string(value)
                if appid:
                    return appid
            nested = _extract_steam_appid(value, next_path)
            if nested:
                return nested
    elif isinstance(payload, list):
        for value in payload:
            nested = _extract_steam_appid(value, path)
            if nested:
                return nested
    return None


def _url_exists(session: requests.Session, url: str, timeout: float) -> bool:
    try:
        head_resp = session.head(url, allow_redirects=True, timeout=timeout)
        if head_resp.status_code < 400:
            return True
        if head_resp.status_code in {403, 405}:
            get_resp = session.get(url, stream=True, timeout=timeout)
            ok = get_resp.status_code < 400
            get_resp.close()
            return ok
        return False
    except requests.RequestException:
        return False


def _steam_store_fallback_candidates(
    session: requests.Session,
    settings: IconSearchSettings,
    steam_appid: str,
    title: str,
) -> list[IconCandidate]:
    fallback: list[IconCandidate] = []
    seen: set[str] = set()

    def _add(
        url: str,
        candidate_id: str,
        width: int = 0,
        height: int = 0,
        has_alpha: bool = False,
    ) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        fallback.append(
            IconCandidate(
                provider="Steam",
                candidate_id=candidate_id,
                title=title,
                preview_url=url,
                image_url=url,
                width=width,
                height=height,
                has_alpha=has_alpha,
                source_url=url,
            )
        )

    appdetails_url = (
        f"https://store.steampowered.com/api/appdetails?appids={steam_appid}&l=english"
    )
    appdetails = _safe_get_json(session, appdetails_url, None, settings.timeout_seconds)
    app_payload = (
        appdetails.get(steam_appid, {}) if isinstance(appdetails, dict) else {}
    )
    data = app_payload.get("data", {}) if isinstance(app_payload, dict) else {}
    if isinstance(data, dict):
        known = [
            ("header_image", 460, 215, False),
            ("capsule_image", 231, 87, False),
            ("capsule_imagev5", 616, 353, False),
            ("background_raw", 1920, 1080, False),
            ("background", 1920, 1080, False),
        ]
        for field_name, width, height, has_alpha in known:
            value = data.get(field_name)
            if isinstance(value, str) and value.strip():
                _add(
                    value.strip(),
                    candidate_id=f"steam:{steam_appid}:{field_name}",
                    width=width,
                    height=height,
                    has_alpha=has_alpha,
                )

    # Probe common Steam static icon/logo assets when SGDB has no icon/logo rows.
    probe_urls: list[tuple[str, int, int, bool, str]] = [
        (
            f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{steam_appid}/logo.png",
            512,
            512,
            True,
            "logo",
        ),
        (
            f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{steam_appid}/logo_2x.png",
            1024,
            1024,
            True,
            "logo_2x",
        ),
        (
            f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{steam_appid}/library_600x900.jpg",
            600,
            900,
            False,
            "library_600x900",
        ),
    ]
    for url, width, height, has_alpha, label in probe_urls:
        if _url_exists(session, url, settings.timeout_seconds):
            _add(
                url,
                candidate_id=f"steam:{steam_appid}:{label}",
                width=width,
                height=height,
                has_alpha=has_alpha,
            )
    return fallback


def _lookup_steam_appid_for_game(
    session: requests.Session,
    settings: IconSearchSettings,
    headers: dict[str, str],
    game_id: int,
    seed_game_payload: dict[str, Any],
) -> str | None:
    appid = _extract_steam_appid(seed_game_payload)
    if appid:
        return appid
    details_url = f"{settings.steamgriddb_api_base.rstrip('/')}/games/id/{game_id}"
    details = _safe_get_json(session, details_url, headers, settings.timeout_seconds)
    if not details:
        return None
    return _extract_steam_appid(details.get("data"))


def _search_steamgriddb(
    settings: IconSearchSettings,
    game_name: str,
    cleaned_name: str,
    sgdb_resources: Iterable[str] | None = None,
) -> list[IconCandidate]:
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        return []
    endpoint_order = normalize_sgdb_resources(
        sgdb_resources, default_enabled_only=True
    )
    session = _session()
    headers = {"Authorization": f"Bearer {settings.steamgriddb_api_key.strip()}"}
    query = quote(cleaned_name or game_name)
    auto_url = f"{settings.steamgriddb_api_base.rstrip('/')}/search/autocomplete/{query}"
    auto_payload = _safe_get_json(session, auto_url, headers, settings.timeout_seconds)
    if not auto_payload:
        return []
    games = auto_payload.get("data") or []
    candidates: list[IconCandidate] = []
    for game in games[:5]:
        game_id = game.get("id")
        title = str(game.get("name") or cleaned_name or game_name or "Game")
        if not game_id:
            continue
        game_candidates: list[IconCandidate] = []
        for endpoint in endpoint_order:
            url = f"{settings.steamgriddb_api_base.rstrip('/')}/{endpoint}/game/{game_id}"
            payload = _safe_get_json(session, url, headers, settings.timeout_seconds)
            if not payload:
                continue
            for item in payload.get("data") or []:
                if not isinstance(item, dict):
                    continue
                raw_url = item.get("url")
                raw_thumb = item.get("thumb")
                image_url = _extract_url_text(raw_url)
                preview_url = _extract_url_text(raw_thumb)
                if not image_url:
                    image_url = preview_url
                if not image_url:
                    continue
                if not preview_url:
                    preview_url = image_url
                width, height = _extract_item_dimensions(
                    item,
                    image_url=image_url,
                    preview_url=preview_url,
                )
                candidate = IconCandidate(
                    provider="SteamGridDB",
                    candidate_id=f"{endpoint}:{item.get('id')}",
                    title=title,
                    preview_url=preview_url,
                    image_url=image_url,
                    width=width,
                    height=height,
                    has_alpha=True,
                    source_url=str(item.get("url") or image_url),
                )
                game_candidates.append(candidate)
        if not game_candidates:
            parsed_game_id = _parse_positive_int_string(game_id)
            if not parsed_game_id:
                continue
            steam_appid = _lookup_steam_appid_for_game(
                session=session,
                settings=settings,
                headers=headers,
                game_id=int(parsed_game_id),
                seed_game_payload=game if isinstance(game, dict) else {},
            )
            if steam_appid:
                game_candidates.extend(
                    _steam_store_fallback_candidates(
                        session=session,
                        settings=settings,
                        steam_appid=steam_appid,
                        title=title,
                    )
                )
        if game_candidates:
            candidates.extend(game_candidates)
    return candidates


def _best_iconfinder_raster(icon_payload: dict[str, Any]) -> tuple[str, str, int, int]:
    best_dl = ""
    best_preview = ""
    best_w = 0
    best_h = 0
    for raster in icon_payload.get("raster_sizes") or []:
        size = int(raster.get("size") or 0)
        for fmt in raster.get("formats") or []:
            dl = str(fmt.get("download_url") or "")
            preview = str(fmt.get("preview_url") or "")
            if not dl and not preview:
                continue
            if size >= max(best_w, best_h):
                best_dl = dl or preview
                best_preview = preview or dl
                best_w = size
                best_h = size
    return best_dl, best_preview, best_w, best_h


def _search_iconfinder(
    settings: IconSearchSettings,
    game_name: str,
    cleaned_name: str,
) -> list[IconCandidate]:
    if not settings.iconfinder_enabled or not settings.iconfinder_api_key.strip():
        return []
    session = _session()
    headers = {"Authorization": f"Bearer {settings.iconfinder_api_key.strip()}"}
    query = quote(f"{cleaned_name or game_name} icon logo transparent")
    url = (
        f"{settings.iconfinder_api_base.rstrip('/')}/icons/search"
        f"?query={query}&count=30&premium=0&vector=0"
    )
    payload = _safe_get_json(session, url, headers, settings.timeout_seconds)
    if not payload:
        return []
    candidates: list[IconCandidate] = []
    for item in payload.get("icons") or []:
        image_url, preview_url, width, height = _best_iconfinder_raster(item)
        if not image_url:
            continue
        title = str(item.get("tags", ["icon"])[0] if item.get("tags") else "icon")
        candidates.append(
            IconCandidate(
                provider="Iconfinder",
                candidate_id=str(item.get("icon_id") or ""),
                title=title,
                preview_url=preview_url or image_url,
                image_url=image_url,
                width=width,
                height=height,
                has_alpha=True,
                source_url=str(item.get("permalink") or image_url),
            )
        )
    return candidates


def search_icon_candidates(
    game_name: str,
    cleaned_name: str,
    settings: IconSearchSettings,
    sgdb_resources: Iterable[str] | None = None,
) -> list[IconCandidate]:
    candidates: list[IconCandidate] = []
    candidates.extend(
        _search_steamgriddb(
            settings,
            game_name,
            cleaned_name,
            sgdb_resources=sgdb_resources,
        )
    )
    if not candidates:
        candidates.extend(_search_iconfinder(settings, game_name, cleaned_name))
    # If SteamGridDB returns some but few/weak entries, augment with Iconfinder too.
    if len(candidates) < 6:
        candidates.extend(_search_iconfinder(settings, game_name, cleaned_name))
    # De-duplicate by image URL and sort by score.
    seen: set[str] = set()
    uniq: list[IconCandidate] = []
    for c in candidates:
        key = c.image_url.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    uniq.sort(key=_score_candidate, reverse=True)
    return uniq[:40]


def download_candidate_image(
    image_url: str,
    timeout_seconds: float = 20.0,
) -> bytes:
    session = _session()
    resp = session.get(image_url, timeout=timeout_seconds)
    resp.raise_for_status()
    return resp.content
