from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from collections import deque
import re
import threading
import time
from typing import Any
from urllib.parse import quote

import requests

from gamemanager.models import IconCandidate


DEFAULT_STEAMGRIDDB_API_BASE = "https://www.steamgriddb.com/api/v2"
DEFAULT_IGDB_API_BASE = "https://api.igdb.com/v4"
DEFAULT_TWITCH_OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
SUPPORTED_SGDB_RESOURCES: tuple[str, ...] = ("icons", "logos", "grids", "heroes")
DEFAULT_SGDB_RESOURCE_ORDER: tuple[str, ...] = ("icons", "logos", "grids", "heroes")
DEFAULT_SGDB_ENABLED_RESOURCES: tuple[str, ...] = ("icons", "logos")
_DIMENSIONS_RE = re.compile(r"(?<!\d)(\d{2,5})\s*[xX]\s*(\d{2,5})(?!\d)")
_IGDB_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_IGDB_SOURCE_TO_STORE: dict[int, str] = {
    1: "Steam",
    5: "GOG",
    20: "Amazon Games",
    22: "Amazon Games",
    23: "Amazon Games",
    26: "EGS",
    30: "Itch.io",
}
_IGDB_WEBSITE_TYPE_TO_STORE: dict[int, str] = {
    13: "Steam",
    15: "Itch.io",
    16: "EGS",
    17: "GOG",
}
_STEAM_APP_RE = re.compile(r"/app/(\d+)", re.IGNORECASE)
_IGDB_RATE_LOCK = threading.Lock()
_IGDB_RATE_TIMESTAMPS: deque[float] = deque()
_IGDB_CONCURRENCY_SEMAPHORE = threading.Semaphore(8)
_IGDB_RATE_LIMIT_PER_SEC = 4


@dataclass(slots=True)
class IconSearchSettings:
    steamgriddb_enabled: bool
    steamgriddb_api_key: str
    steamgriddb_api_base: str = DEFAULT_STEAMGRIDDB_API_BASE
    igdb_enabled: bool = False
    igdb_client_id: str = ""
    igdb_client_secret: str = ""
    igdb_api_base: str = DEFAULT_IGDB_API_BASE
    twitch_oauth_token_url: str = DEFAULT_TWITCH_OAUTH_TOKEN_URL
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
    if candidate.provider == "IGDB":
        if ":cover:" in candidate_id:
            score -= 0.25
        elif ":artwork:" in candidate_id:
            score += 0.1
    return score


def _provider_rank(provider: str) -> int:
    token = str(provider or "").strip()
    if token == "SteamGridDB":
        return 0
    if token == "Steam":
        return 1
    if token == "IGDB":
        return 2
    return 3


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


def _safe_post_json(
    session: requests.Session,
    url: str,
    *,
    headers: dict[str, str] | None,
    data: dict[str, Any] | None = None,
    body: str = "",
    timeout: float,
    enforce_igdb_rate_limit: bool = False,
) -> dict[str, Any] | list[Any] | None:
    semaphore_acquired = False
    try:
        if enforce_igdb_rate_limit:
            _IGDB_CONCURRENCY_SEMAPHORE.acquire()
            semaphore_acquired = True
            while True:
                sleep_for = 0.0
                with _IGDB_RATE_LOCK:
                    now = time.monotonic()
                    while _IGDB_RATE_TIMESTAMPS and (now - _IGDB_RATE_TIMESTAMPS[0]) >= 1.0:
                        _IGDB_RATE_TIMESTAMPS.popleft()
                    if len(_IGDB_RATE_TIMESTAMPS) < _IGDB_RATE_LIMIT_PER_SEC:
                        _IGDB_RATE_TIMESTAMPS.append(now)
                        break
                    oldest = _IGDB_RATE_TIMESTAMPS[0]
                    sleep_for = max(0.01, 1.0 - (now - oldest))
                time.sleep(sleep_for)
        if body:
            resp = session.post(
                url,
                headers=headers,
                data=body.encode("utf-8"),
                timeout=timeout,
            )
        else:
            resp = session.post(url, headers=headers, data=data, timeout=timeout)
        if resp.status_code >= 400:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None
    finally:
        if semaphore_acquired:
            _IGDB_CONCURRENCY_SEMAPHORE.release()


def _igdb_image_url(image_id: str, size: str) -> str:
    token = str(image_id or "").strip()
    if not token:
        return ""
    return f"https://images.igdb.com/igdb/image/upload/t_{size}/{token}.jpg"


def _normalize_name_for_compare(value: str) -> str:
    raw = str(value or "").strip().casefold()
    if not raw:
        return ""
    return " ".join(part for part in re.sub(r"[^a-z0-9]+", " ", raw).split() if part)


def _store_id_from_store_url(store_name: str, url: str) -> str:
    token = str(url or "").strip()
    if not token:
        return ""
    if store_name == "Steam":
        match = _STEAM_APP_RE.search(token)
        if match:
            appid = str(match.group(1) or "").strip()
            return appid if appid.isdigit() else ""
        return ""
    if store_name == "GOG":
        marker = "/game/"
        idx = token.lower().find(marker)
        if idx >= 0:
            slug = token[idx + len(marker) :].split("?", 1)[0].split("#", 1)[0].strip("/")
            return slug
    if store_name == "EGS":
        # Typical forms include /p/<slug> or /product/<slug>.
        for marker in ("/p/", "/product/"):
            idx = token.lower().find(marker)
            if idx >= 0:
                slug = token[idx + len(marker) :].split("?", 1)[0].split("#", 1)[0].strip("/")
                return slug
    if store_name == "Itch.io":
        return token
    return ""


def _igdb_access_token(
    session: requests.Session,
    settings: IconSearchSettings,
) -> str:
    if not settings.igdb_enabled:
        return ""
    client_id = str(settings.igdb_client_id or "").strip()
    client_secret = str(settings.igdb_client_secret or "").strip()
    if not client_id or not client_secret:
        return ""
    cache_key = f"{client_id}:{client_secret}"
    cached = _IGDB_TOKEN_CACHE.get(cache_key)
    now = time.time()
    if cached is not None:
        token, expires_at = cached
        if token and now < max(0.0, float(expires_at) - 30.0):
            return token
    payload = _safe_post_json(
        session,
        str(settings.twitch_oauth_token_url or DEFAULT_TWITCH_OAUTH_TOKEN_URL).strip(),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        headers=None,
        timeout=settings.timeout_seconds,
    )
    if not isinstance(payload, dict):
        return ""
    token = str(payload.get("access_token") or "").strip()
    expires_in_raw = payload.get("expires_in")
    expires_in = 0
    if isinstance(expires_in_raw, (int, float)):
        expires_in = max(0, int(expires_in_raw))
    if not token:
        return ""
    _IGDB_TOKEN_CACHE[cache_key] = (token, now + float(expires_in or 3600))
    return token


def _search_igdb(
    settings: IconSearchSettings,
    game_name: str,
    cleaned_name: str,
) -> list[IconCandidate]:
    session = _session()
    token = _igdb_access_token(session, settings)
    if not token:
        return []
    query_text = str(cleaned_name or game_name).strip()
    if not query_text:
        return []
    headers = {
        "Client-ID": str(settings.igdb_client_id or "").strip(),
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    body = (
        f'search "{query_text.replace(chr(34), "").strip()}"; '
        "fields id,name,cover.image_id,artworks.image_id,url; "
        "limit 8;"
    )
    payload = _safe_post_json(
        session,
        f"{settings.igdb_api_base.rstrip('/')}/games",
        headers=headers,
        body=body,
        timeout=settings.timeout_seconds,
        enforce_igdb_rate_limit=True,
    )
    rows = payload if isinstance(payload, list) else []
    out: list[IconCandidate] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        game_id = str(row.get("id") or "").strip()
        title = str(row.get("name") or query_text).strip() or query_text
        source_url = str(row.get("url") or "").strip() or f"https://www.igdb.com/games/{game_id}"

        cover = row.get("cover")
        if isinstance(cover, dict):
            image_id = str(cover.get("image_id") or "").strip()
            if image_id:
                image_url = _igdb_image_url(image_id, "1080p")
                preview_url = _igdb_image_url(image_id, "cover_big")
                if image_url and image_url not in seen:
                    seen.add(image_url)
                    out.append(
                        IconCandidate(
                            provider="IGDB",
                            candidate_id=f"igdb:cover:{game_id}:{image_id}",
                            title=title,
                            preview_url=preview_url or image_url,
                            image_url=image_url,
                            width=264,
                            height=374,
                            has_alpha=False,
                            source_url=source_url,
                        )
                    )

        artworks = row.get("artworks")
        if isinstance(artworks, list):
            for artwork in artworks[:3]:
                if not isinstance(artwork, dict):
                    continue
                image_id = str(artwork.get("image_id") or "").strip()
                if not image_id:
                    continue
                image_url = _igdb_image_url(image_id, "1080p")
                preview_url = _igdb_image_url(image_id, "screenshot_med")
                if not image_url or image_url in seen:
                    continue
                seen.add(image_url)
                out.append(
                    IconCandidate(
                        provider="IGDB",
                        candidate_id=f"igdb:artwork:{game_id}:{image_id}",
                        title=title,
                        preview_url=preview_url or image_url,
                        image_url=image_url,
                        width=1920,
                        height=1080,
                        has_alpha=False,
                        source_url=source_url,
                    )
                )
    return out


def lookup_igdb_store_ids_for_title(
    settings: IconSearchSettings,
    title: str,
) -> dict[str, str]:
    session = _session()
    token = _igdb_access_token(session, settings)
    if not token:
        return {}
    query_text = str(title or "").strip()
    if not query_text:
        return {}
    headers = {
        "Client-ID": str(settings.igdb_client_id or "").strip(),
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    safe_query = query_text.replace(chr(34), "").strip()
    games_payload = _safe_post_json(
        session,
        f"{settings.igdb_api_base.rstrip('/')}/games",
        headers=headers,
        body=f'search "{safe_query}"; fields id,name; limit 8;',
        timeout=settings.timeout_seconds,
        enforce_igdb_rate_limit=True,
    )
    games = games_payload if isinstance(games_payload, list) else []
    if not games:
        return {}
    query_norm = _normalize_name_for_compare(query_text)
    selected_game_id = 0
    for row in games:
        if not isinstance(row, dict):
            continue
        game_id = int(row.get("id") or 0)
        if game_id <= 0:
            continue
        name_norm = _normalize_name_for_compare(str(row.get("name") or ""))
        if query_norm and name_norm and query_norm == name_norm:
            selected_game_id = game_id
            break
    if selected_game_id <= 0:
        return {}

    store_ids: dict[str, str] = {}
    external_payload = _safe_post_json(
        session,
        f"{settings.igdb_api_base.rstrip('/')}/external_games",
        headers=headers,
        body=(
            "fields external_game_source,uid,url,name,game; "
            f"where game = {selected_game_id}; limit 200;"
        ),
        timeout=settings.timeout_seconds,
        enforce_igdb_rate_limit=True,
    )
    for row in external_payload if isinstance(external_payload, list) else []:
        if not isinstance(row, dict):
            continue
        source_id = int(row.get("external_game_source") or 0)
        store_name = _IGDB_SOURCE_TO_STORE.get(source_id, "")
        if not store_name or store_name in store_ids:
            continue
        token = str(row.get("uid") or "").strip()
        if not token:
            token = _store_id_from_store_url(store_name, str(row.get("url") or ""))
        if token:
            store_ids[store_name] = token

    websites_payload = _safe_post_json(
        session,
        f"{settings.igdb_api_base.rstrip('/')}/websites",
        headers=headers,
        body=(
            "fields type,url,game; "
            f"where game = {selected_game_id}; limit 200;"
        ),
        timeout=settings.timeout_seconds,
        enforce_igdb_rate_limit=True,
    )
    for row in websites_payload if isinstance(websites_payload, list) else []:
        if not isinstance(row, dict):
            continue
        type_id = int(row.get("type") or 0)
        store_name = _IGDB_WEBSITE_TYPE_TO_STORE.get(type_id, "")
        if not store_name or store_name in store_ids:
            continue
        token = _store_id_from_store_url(store_name, str(row.get("url") or ""))
        if token:
            store_ids[store_name] = token
    return store_ids


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
        candidates.extend(_search_igdb(settings, game_name, cleaned_name))
    # If SteamGridDB returns some but few entries, augment with IGDB too.
    if len(candidates) < 6:
        candidates.extend(_search_igdb(settings, game_name, cleaned_name))
    # De-duplicate by image URL and sort by score.
    seen: set[str] = set()
    uniq: list[IconCandidate] = []
    for c in candidates:
        key = c.image_url.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(c)
    uniq.sort(
        key=lambda c: (
            _provider_rank(c.provider),
            -float(_score_candidate(c)),
            str(c.title or "").casefold(),
        )
    )
    return uniq[:40]


def download_candidate_image(
    image_url: str,
    timeout_seconds: float = 20.0,
) -> bytes:
    session = _session()
    resp = session.get(image_url, timeout=timeout_seconds)
    resp.raise_for_status()
    return resp.content
