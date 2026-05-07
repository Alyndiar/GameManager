from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Callable
from urllib import error, parse, request

from gamemanager.services.storefronts.base import (
    StoreAuthResult,
    StoreConnectorStatus,
    StoreEntitlement,
    StorePlugin,
)
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


GOG_ACCOUNT_PAGE_URL = "https://www.gog.com/account/"
GOG_ACCOUNT_BASIC_URL = "https://menu.gog.com/v1/account/basic"
_GOG_STATS_URL_TEMPLATE = (
    "https://www.gog.com/u/{username}/games/stats?sort=recent_playtime&order=desc&page={page}"
)
_GOG_FILTERED_PRODUCTS_URL_TEMPLATE = (
    "https://www.gog.com/account/getFilteredProducts"
    "?hiddenFlag=0&mediaType=1&page={page}&sortBy=title"
)
_GOG_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) GameManager/1.0"
_GOG_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,127}$", re.IGNORECASE)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_from_now_seconds(seconds: int) -> str:
    return (_utc_now() + timedelta(seconds=max(0, int(seconds)))).isoformat()


def _decode_json(payload: bytes) -> dict[str, object]:
    text = payload.decode("utf-8", errors="ignore")
    stripped = str(text or "").strip()
    if not stripped:
        return {}
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return parsed
    return {}


def _http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_s: int = 30,
) -> dict[str, object]:
    req = request.Request(
        str(url),
        headers=dict(headers or {}),
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=timeout_s) as response:
            return _decode_json(response.read())
    except error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        details = _decode_json(raw)
        code = str(details.get("error") or details.get("errorCode") or "").strip()
        msg = str(details.get("message") or details.get("errorMessage") or "").strip()
        suffix = f" ({code})" if code else ""
        if msg:
            raise RuntimeError(f"GOG request failed: {msg}{suffix}") from exc
        raise RuntimeError(f"GOG request failed with HTTP {int(exc.code)}{suffix}") from exc


def _extract_slug_from_url(url: str) -> str:
    token = str(url or "").strip()
    if not token:
        return ""
    parsed = parse.urlparse(token)
    path = str(parsed.path or "").strip().strip("/")
    if not path:
        return ""
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0].casefold() == "game":
        slug = str(parts[1] or "").strip()
        if slug and _GOG_SLUG_RE.fullmatch(slug):
            return slug
    if len(parts) >= 3 and parts[1].casefold() == "game":
        slug = str(parts[2] or "").strip()
        if slug and _GOG_SLUG_RE.fullmatch(slug):
            return slug
    if parts and _GOG_SLUG_RE.fullmatch(parts[-1]):
        return str(parts[-1]).strip()
    return ""


def _parse_gog_auth_payload(payload: dict[str, str] | None) -> dict[str, str]:
    raw = dict(payload or {})
    out: dict[str, str] = {
        "account_id": "",
        "username": "",
        "access_token": "",
        "expires_utc": "",
    }

    for key in ("account_id", "user_id", "userid"):
        value = str(raw.get(key) or "").strip()
        if value:
            out["account_id"] = value
            break
    for key in ("username", "account_name", "display_name"):
        value = str(raw.get(key) or "").strip()
        if value:
            out["username"] = value
            break
    for key in ("access_token", "token", "gog_access_token"):
        value = str(raw.get(key) or "").strip()
        if value:
            out["access_token"] = value
            break
    for key in ("expires_utc", "token_expires_utc"):
        value = str(raw.get(key) or "").strip()
        if value:
            out["expires_utc"] = value
            break

    raw_basic = str(raw.get("account_basic_json") or raw.get("authorization_code") or "").strip()
    if raw_basic.startswith("{") and raw_basic.endswith("}"):
        try:
            parsed = json.loads(raw_basic)
        except Exception:
            parsed = {}
        if isinstance(parsed, dict):
            account_id = str(parsed.get("userId") or parsed.get("user_id") or "").strip()
            username = str(parsed.get("username") or "").strip()
            access_token = str(parsed.get("accessToken") or parsed.get("access_token") or "").strip()
            expires = parsed.get("accessTokenExpires")
            if account_id:
                out["account_id"] = account_id
            if username:
                out["username"] = username
            if access_token:
                out["access_token"] = access_token
            if out["expires_utc"] == "" and expires is not None:
                try:
                    out["expires_utc"] = _utc_from_now_seconds(int(expires))
                except Exception:
                    pass

    if not out["username"] and not out["account_id"] and not out["access_token"] and raw_basic:
        # Plain account name flow (public library mode).
        out["username"] = raw_basic
        out["account_id"] = raw_basic
    if not out["account_id"] and out["username"]:
        out["account_id"] = out["username"]
    return out


def _bearer_headers(access_token: str) -> dict[str, str]:
    token = str(access_token or "").strip()
    base = {"User-Agent": _GOG_USER_AGENT}
    if token:
        base["Authorization"] = f"Bearer {token}"
    return base


def _fetch_account_basic(access_token: str) -> dict[str, object]:
    token = str(access_token or "").strip()
    if not token:
        return {}
    payload = _http_json(
        GOG_ACCOUNT_BASIC_URL,
        headers=_bearer_headers(token),
        timeout_s=25,
    )
    if not payload:
        return {}
    return payload


def _parse_stats_items(payload: dict[str, object]) -> list[dict[str, object]]:
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    rows = embedded.get("items")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _parse_legacy_items(payload: dict[str, object]) -> list[dict[str, object]]:
    rows = payload.get("products")
    if not isinstance(rows, list):
        return []
    out: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        game_id = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip()
        if not game_id or not title:
            continue
        out.append(
            {
                "game": {
                    "id": game_id,
                    "title": title,
                    "url": str(row.get("url") or "").strip(),
                    "image": str(row.get("image") or "").strip(),
                },
                "stats": row.get("stats"),
            }
        )
    return out


def _owned_games_from_stats(
    *,
    username: str,
    access_token: str = "",
    progress_cb: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    user = str(username or "").strip()
    if not user:
        return []
    first_url = _GOG_STATS_URL_TEMPLATE.format(username=parse.quote(user, safe=""), page=1)
    first = _http_json(first_url, headers=_bearer_headers(access_token), timeout_s=30)
    total_pages = 0
    try:
        total_pages = int(first.get("pages") or 0)
    except Exception:
        total_pages = 0
    if total_pages <= 0:
        total_pages = 1
    out = _parse_stats_items(first)
    if progress_cb is not None:
        progress_cb("GOG sync: library stats pages", 1, max(1, total_pages))
    for page in range(2, total_pages + 1):
        if should_cancel is not None and should_cancel():
            break
        url = _GOG_STATS_URL_TEMPLATE.format(username=parse.quote(user, safe=""), page=page)
        payload = _http_json(url, headers=_bearer_headers(access_token), timeout_s=30)
        out.extend(_parse_stats_items(payload))
        if progress_cb is not None:
            progress_cb("GOG sync: library stats pages", page, max(1, total_pages))
    return out


def _owned_games_from_legacy(
    *,
    access_token: str = "",
    progress_cb: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    first_url = _GOG_FILTERED_PRODUCTS_URL_TEMPLATE.format(page=1)
    first = _http_json(first_url, headers=_bearer_headers(access_token), timeout_s=30)
    total_pages = 0
    try:
        total_pages = int(first.get("totalPages") or 0)
    except Exception:
        total_pages = 0
    if total_pages <= 0:
        total_pages = 1
    out = _parse_legacy_items(first)
    if progress_cb is not None:
        progress_cb("GOG sync: legacy library pages", 1, max(1, total_pages))
    for page in range(2, total_pages + 1):
        if should_cancel is not None and should_cancel():
            break
        url = _GOG_FILTERED_PRODUCTS_URL_TEMPLATE.format(page=page)
        payload = _http_json(url, headers=_bearer_headers(access_token), timeout_s=30)
        out.extend(_parse_legacy_items(payload))
        if progress_cb is not None:
            progress_cb("GOG sync: legacy library pages", page, max(1, total_pages))
    return out


def _rows_to_entitlements(rows: list[dict[str, object]]) -> list[StoreEntitlement]:
    out: list[StoreEntitlement] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        game = row.get("game")
        if not isinstance(game, dict):
            continue
        game_id = str(game.get("id") or "").strip()
        title = str(game.get("title") or "").strip()
        if not game_id or not title:
            continue
        if game_id in seen:
            continue
        seen.add(game_id)
        game_url = str(game.get("url") or "").strip()
        slug = _extract_slug_from_url(game_url)
        store_id = slug or game_id
        out.append(
            StoreEntitlement(
                entitlement_id=game_id,
                title=title,
                store_game_id=store_id,
                manifest_id=game_id,
                is_installed=False,
                metadata_json=json.dumps(
                    {
                        "game_id": game_id,
                        "url": game_url,
                        "image": str(game.get("image") or "").strip(),
                        "slug": slug,
                        "stats": row.get("stats"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
        )
    return out


class GogConnector(StubLauncherConnector):
    store_name = "GOG"

    def connect(self, auth_payload: dict[str, str] | None = None) -> StoreAuthResult:
        payload = _parse_gog_auth_payload(auth_payload)
        access_token = str(payload.get("access_token") or "").strip()
        account_id = str(payload.get("account_id") or "").strip()
        username = str(payload.get("username") or "").strip()
        expires_utc = str(payload.get("expires_utc") or "").strip()

        # Try to enrich account identity from account/basic whenever token exists.
        if access_token:
            try:
                account = _fetch_account_basic(access_token)
            except Exception:
                account = {}
            if account:
                account_id = str(account.get("userId") or account_id).strip() or account_id
                username = str(account.get("username") or username).strip() or username
                if not expires_utc:
                    try:
                        expires_utc = _utc_from_now_seconds(int(account.get("accessTokenExpires") or 0))
                    except Exception:
                        expires_utc = ""

        if not account_id and not username:
            return StoreAuthResult(
                success=False,
                status="missing_account_identity",
                message=(
                    "GOG connection requires either account/basic JSON (recommended) "
                    "or a public account name."
                ),
            )
        if not account_id:
            account_id = username
        if not username:
            username = account_id

        if access_token:
            auth_kind = "browser_session_token"
            status = "connected_private_or_public"
            message = "GOG account linked using account/basic payload."
        else:
            auth_kind = "public_account_name"
            status = "connected_public_profile"
            message = (
                "GOG account linked by public username. Sync requires the profile "
                "library to be visible publicly."
            )

        return StoreAuthResult(
            success=True,
            account_id=account_id,
            display_name=username or account_id,
            auth_kind=auth_kind,
            token_secret=access_token,
            expires_utc=expires_utc,
            scopes="",
            status=status,
            message=message,
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
        payload = dict(auth_payload or {})
        access_token = str(token_secret or payload.get("access_token") or "").strip()
        username = str(payload.get("username") or "").strip()
        if progress_cb is not None:
            progress_cb("GOG sync: resolve account identity", 5, 100)
        if should_cancel is not None and should_cancel():
            return []

        if access_token:
            try:
                basic = _fetch_account_basic(access_token)
            except Exception:
                basic = {}
            if basic:
                username = str(basic.get("username") or username or account_id).strip()
        if not username:
            username = str(account_id or "").strip()
        if not username:
            if progress_cb is not None:
                progress_cb("GOG sync: missing account identity", 100, 100)
            return []

        rows: list[dict[str, object]] = []
        if progress_cb is not None:
            progress_cb("GOG sync: fetch stats library", 12, 100)
        try:
            rows = _owned_games_from_stats(
                username=username,
                access_token=access_token,
                progress_cb=(
                    (lambda stage, cur, total: progress_cb(
                        stage,
                        min(60, 12 + int(round((max(0, cur) / max(1, total)) * 48.0))),
                        100,
                    ))
                    if progress_cb is not None
                    else None
                ),
                should_cancel=should_cancel,
            )
        except Exception:
            rows = []

        if should_cancel is not None and should_cancel():
            return _rows_to_entitlements(rows)

        # Playnite-compatible fallback endpoint.
        if not rows:
            if progress_cb is not None:
                progress_cb("GOG sync: fallback legacy library", 65, 100)
            try:
                rows = _owned_games_from_legacy(
                    access_token=access_token,
                    progress_cb=(
                        (lambda stage, cur, total: progress_cb(
                            stage,
                            min(92, 65 + int(round((max(0, cur) / max(1, total)) * 27.0))),
                            100,
                        ))
                        if progress_cb is not None
                        else None
                    ),
                    should_cancel=should_cancel,
                )
            except Exception:
                rows = []

        entitlements = _rows_to_entitlements(rows)
        if progress_cb is not None:
            progress_cb("GOG sync: done", 100, 100)
        return entitlements

    def status(self, account_id: str = "") -> StoreConnectorStatus:
        connected = bool(str(account_id or "").strip())
        return StoreConnectorStatus(
            available=True,
            connected=connected,
            auth_kind="browser_session_or_public_account",
            message=(
                "GOG connector using Playnite-compatible account/basic + library endpoints "
                "with public-account fallback."
            ),
            metadata={"store_name": self.store_name},
        )


PLUGIN = StorePlugin(
    store_name="GOG",
    connector_cls=GogConnector,
    auth_kind="browser_session_or_public_account",
    supports_full_library_sync=True,
    description="GOG connector with account/basic + library sync and Playnite-compatible endpoint fallback.",
)
