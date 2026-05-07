from __future__ import annotations

import configparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import json
import locale
import os
from pathlib import Path
import re
from http import cookiejar
from typing import Callable
from urllib import error, parse, request
import time

from gamemanager.services.storefronts.base import (
    StoreAuthResult,
    StoreConnectorStatus,
    StoreEntitlement,
    StorePlugin,
)
from gamemanager.services.storefronts.stub_connector import StubLauncherConnector


EPIC_CLIENT_ID = "34a02cf8f4414e29b15921876da36f9a"
EPIC_LOGIN_AUTH_CODE_URL = (
    "https://www.epicgames.com/id/login"
    "?redirectUrl=https%3A%2F%2Fwww.epicgames.com%2Fid%2Fapi%2Fredirect"
    f"%3FclientId%3D{EPIC_CLIENT_ID}%26responseType%3Dcode"
)
EPIC_SECURITY_SETTINGS_URL = "https://www.epicgames.com/account/password"
_EPIC_DEFAULT_IDENTITY_DOMAIN = "account-public-service-prod03.ol.epicgames.com"
_EPIC_DEFAULT_LIBRARY_DOMAIN = "library-service.live.use1a.on.epicgames.com"
_EPIC_DEFAULT_CATALOG_DOMAIN = "catalog-public-service-prod06.ol.epicgames.com"
_EPIC_SET_SID_URL = "https://www.epicgames.com/id/api/set-sid?sid={0}"
_EPIC_CSRF_URL = "https://www.epicgames.com/id/api/csrf"
_EPIC_EXCHANGE_GENERATE_URL = "https://www.epicgames.com/id/api/exchange/generate"
_EPIC_BASIC_AUTH = (
    "MzRhMDJjZjhmNDQxNGUyOWIxNTkyMTg3NmRhMzZmOWE6"
    "ZGFhZmJjY2M3Mzc3NDUwMzlkZmZlNTNkOTRmYzc2Y2Y="
)
_EPIC_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) EpicGamesLauncher"
_AUTH_CODE_REGEX = re.compile(r'"authorizationCode"\s*:\s*"([^"]+)"', re.IGNORECASE)
_EXCHANGE_CODE_REGEX = re.compile(r'"exchangeCode"\s*:\s*"([^"]+)"', re.IGNORECASE)
_SID_REGEX = re.compile(r'"sid"\s*:\s*"([^"]+)"', re.IGNORECASE)
_EGS_CATALOG_BATCH_SIZE = 40
_EGS_CATALOG_MAX_WORKERS = 6
_EGS_CATALOG_CACHE_VERSION = 1
_EGS_CATALOG_CACHE_TTL_S = 60 * 60 * 24 * 30
_EGS_CATALOG_CACHE_MAX_ITEMS = 30_000


def _non_null_token(value: object) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    if token.casefold() in {"null", "none", "undefined"}:
        return ""
    return token


def _normalize_epic_domain(value: str, default: str) -> str:
    token = str(value or "").strip()
    if not token:
        return default
    parsed = parse.urlparse(token)
    if parsed.netloc:
        token = str(parsed.netloc or "").strip()
    token = token.strip().strip("/")
    if not token:
        return default
    return token


def _epic_launcher_config_candidates() -> list[Path]:
    candidates: list[Path] = []
    program_data = str(os.environ.get("PROGRAMDATA") or "").strip()
    if program_data:
        launcher_installed = (
            Path(program_data)
            / "Epic"
            / "UnrealEngineLauncher"
            / "LauncherInstalled.dat"
        )
        if launcher_installed.is_file():
            try:
                payload = _decode_json(launcher_installed.read_bytes())
                entries = payload.get("InstallationList")
                if isinstance(entries, list):
                    for row in entries:
                        if not isinstance(row, dict):
                            continue
                        install_location = str(row.get("InstallLocation") or "").strip()
                        if not install_location:
                            continue
                        app_name = str(row.get("AppName") or "").strip().casefold()
                        namespace_id = str(row.get("NamespaceId") or "").strip().casefold()
                        if app_name != "epicgameslauncher" and namespace_id != "epic":
                            continue
                        candidates.append(
                            Path(install_location)
                            / "Launcher"
                            / "Portal"
                            / "Config"
                            / "DefaultPortalRegions.ini"
                        )
            except Exception:
                pass

    roots = [
        Path(str(os.environ.get("ProgramFiles(x86)") or "").strip()) / "Epic Games",
        Path(str(os.environ.get("ProgramFiles") or "").strip()) / "Epic Games",
    ]
    for root in roots:
        token = str(root).strip()
        if not token:
            continue
        candidates.append(
            root / "Launcher" / "Portal" / "Config" / "DefaultPortalRegions.ini"
        )

    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        token = str(path).strip()
        if not token:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


@lru_cache(maxsize=1)
def _epic_service_domains() -> tuple[str, str, str]:
    identity = _EPIC_DEFAULT_IDENTITY_DOMAIN
    library = _EPIC_DEFAULT_LIBRARY_DOMAIN
    catalog = _EPIC_DEFAULT_CATALOG_DOMAIN
    for config_path in _epic_launcher_config_candidates():
        if not config_path.is_file():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(config_path, encoding="utf-8")
        except Exception:
            continue
        identity_candidate = parser.get(
            "Portal.OnlineSubsystemMcp.OnlineIdentityMcp Prod",
            "Domain",
            fallback="",
        )
        library_candidate = parser.get(
            "Portal.OnlineSubsystemMcp.OnlineLibraryServiceMcp Prod",
            "Domain",
            fallback="",
        )
        catalog_candidate = parser.get(
            "Portal.OnlineSubsystemMcp.OnlineCatalogServiceMcp Prod",
            "Domain",
            fallback="",
        )
        identity = _normalize_epic_domain(identity_candidate, identity)
        library = _normalize_epic_domain(library_candidate, library)
        catalog = _normalize_epic_domain(catalog_candidate, catalog)
        break
    return identity, library, catalog


def _epic_oauth_token_url() -> str:
    identity, _, _ = _epic_service_domains()
    return f"https://{identity}/account/api/oauth/token"


def _epic_account_url(account_id: str) -> str:
    identity, _, _ = _epic_service_domains()
    return (
        f"https://{identity}/account/api/public/account/"
        f"{parse.quote(str(account_id), safe='')}"
    )


def _epic_library_items_url(cursor: str = "") -> str:
    _, library, _ = _epic_service_domains()
    base = f"https://{library}/library/api/public/items?includeMetadata=true&platform=Windows"
    token = str(cursor or "").strip()
    if token:
        return f"{base}&cursor={parse.quote(token, safe='')}"
    return base


def _epic_catalog_url(namespace: str, catalog_item_id: str) -> str:
    _, _, catalog = _epic_service_domains()
    return (
        f"https://{catalog}/catalog/api/shared/namespace/{parse.quote(namespace, safe='')}"
        f"/bulk/items?id={parse.quote(catalog_item_id, safe='')}"
        "&country=US&locale=en-US&includeMainGameDetails=true"
    )


def _epic_catalog_bulk_url(namespace: str, catalog_item_ids: list[str]) -> str:
    _, _, catalog = _epic_service_domains()
    ids = [str(value or "").strip() for value in catalog_item_ids if str(value or "").strip()]
    if not ids:
        return ""
    id_query = "&".join(f"id={parse.quote(value, safe='')}" for value in ids)
    return (
        f"https://{catalog}/catalog/api/shared/namespace/{parse.quote(namespace, safe='')}"
        f"/bulk/items?{id_query}"
        "&country=US&locale=en-US&includeMainGameDetails=true"
    )


def _default_egs_catalog_cache_path(project_data_dir: str = "") -> Path:
    root = Path(str(project_data_dir or "").strip())
    if not root:
        # Fallback: workspace-local cache folder.
        root = Path(__file__).resolve().parents[3]
    return root / "cache" / "storefronts" / "egs_catalog_cache.json"


def _parse_cache_row_key(raw: str) -> tuple[str, str] | None:
    token = str(raw or "").strip().casefold()
    if not token or "|" not in token:
        return None
    namespace, catalog_id = token.split("|", 1)
    namespace = str(namespace or "").strip().casefold()
    catalog_id = str(catalog_id or "").strip().casefold()
    if not namespace or not catalog_id:
        return None
    return namespace, catalog_id


def _encode_cache_row_key(namespace: str, catalog_item_id: str) -> str:
    return f"{str(namespace or '').strip().casefold()}|{str(catalog_item_id or '').strip().casefold()}"


def _load_egs_catalog_cache(
    path: Path,
    *,
    now_s: float | None = None,
) -> dict[tuple[str, str], tuple[dict[str, object], float]]:
    now_value = float(now_s if now_s is not None else time.time())
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    try:
        version = int(payload.get("version") or 0)
    except Exception:
        version = 0
    if version != _EGS_CATALOG_CACHE_VERSION:
        return {}
    items = payload.get("items")
    if not isinstance(items, dict):
        return {}
    out: dict[tuple[str, str], tuple[dict[str, object], float]] = {}
    for raw_key, raw_row in items.items():
        row_key = _parse_cache_row_key(str(raw_key or ""))
        if row_key is None:
            continue
        row_obj = raw_row if isinstance(raw_row, dict) else {}
        payload_obj = row_obj.get("payload")
        if not isinstance(payload_obj, dict):
            continue
        try:
            fetched_unix = float(row_obj.get("fetched_unix") or 0.0)
        except Exception:
            fetched_unix = 0.0
        if fetched_unix <= 0.0:
            continue
        if (now_value - fetched_unix) > _EGS_CATALOG_CACHE_TTL_S:
            continue
        out[row_key] = (payload_obj, fetched_unix)
    return out


def _save_egs_catalog_cache(
    path: Path,
    rows: dict[tuple[str, str], tuple[dict[str, object], float]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep most recently fetched rows only.
    ordered = sorted(
        rows.items(),
        key=lambda entry: float(entry[1][1]),
        reverse=True,
    )[:_EGS_CATALOG_CACHE_MAX_ITEMS]
    payload_rows: dict[str, dict[str, object]] = {}
    for (namespace, catalog_id), (catalog_item, fetched_unix) in ordered:
        if not isinstance(catalog_item, dict):
            continue
        payload_rows[_encode_cache_row_key(namespace, catalog_id)] = {
            "payload": catalog_item,
            "fetched_unix": float(fetched_unix),
        }
    doc = {
        "version": _EGS_CATALOG_CACHE_VERSION,
        "saved_unix": time.time(),
        "items": payload_rows,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(doc, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


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


def _extract_auth_payload(raw_value: str) -> dict[str, str]:
    out = {
        "authorization_code": "",
        "exchange_code": "",
        "sid": "",
    }
    token = str(raw_value or "").strip().strip('"')
    if not token:
        return out
    # Full redirect URL pasted from browser.
    if token.startswith("http://") or token.startswith("https://"):
        try:
            parsed = parse.urlparse(token)
            query = parse.parse_qs(parsed.query or "", keep_blank_values=False)
            for key in ("authorizationCode", "code"):
                values = list(query.get(key) or [])
                if values:
                    code = _non_null_token(values[0])
                    if code:
                        out["authorization_code"] = code
            for key in ("exchangeCode", "exchange_code"):
                values = list(query.get(key) or [])
                if values:
                    code = _non_null_token(values[0])
                    if code:
                        out["exchange_code"] = code
            values = list(query.get("sid") or [])
            if values:
                sid = _non_null_token(values[0])
                if sid:
                    out["sid"] = sid
            if out["authorization_code"] or out["exchange_code"] or out["sid"]:
                return out
        except Exception:
            pass
    # JSON payload pasted from Epic redirect page.
    if token.startswith("{") and token.endswith("}"):
        try:
            payload = json.loads(token)
            if isinstance(payload, dict):
                for key in ("authorizationCode", "code"):
                    value = _non_null_token(payload.get(key))
                    if value:
                        out["authorization_code"] = value
                for key in ("exchangeCode", "exchange_code"):
                    value = _non_null_token(payload.get(key))
                    if value:
                        out["exchange_code"] = value
                redirect_url = _non_null_token(payload.get("redirectUrl"))
                if redirect_url:
                    try:
                        parsed_redirect = parse.urlparse(redirect_url)
                        query_redirect = parse.parse_qs(
                            parsed_redirect.query or "",
                            keep_blank_values=False,
                        )
                        if not out["authorization_code"]:
                            for key in ("authorizationCode", "code"):
                                values = list(query_redirect.get(key) or [])
                                if values:
                                    value = _non_null_token(values[0])
                                    if value:
                                        out["authorization_code"] = value
                                        break
                        if not out["exchange_code"]:
                            for key in ("exchangeCode", "exchange_code"):
                                values = list(query_redirect.get(key) or [])
                                if values:
                                    value = _non_null_token(values[0])
                                    if value:
                                        out["exchange_code"] = value
                                        break
                        if not out["sid"]:
                            values = list(query_redirect.get("sid") or [])
                            if values:
                                sid_query = _non_null_token(values[0])
                                if sid_query:
                                    out["sid"] = sid_query
                    except Exception:
                        pass
                sid_value = _non_null_token(payload.get("sid"))
                if sid_value:
                    out["sid"] = sid_value
                if out["authorization_code"] or out["exchange_code"] or out["sid"]:
                    return out
        except Exception:
            pass
    match = _AUTH_CODE_REGEX.search(token)
    if match is not None:
        out["authorization_code"] = str(match.group(1) or "").strip()
    match = _EXCHANGE_CODE_REGEX.search(token)
    if match is not None:
        out["exchange_code"] = str(match.group(1) or "").strip()
    match = _SID_REGEX.search(token)
    if match is not None:
        out["sid"] = _non_null_token(match.group(1))
    if out["authorization_code"] or out["exchange_code"] or out["sid"]:
        return out
    out["authorization_code"] = token
    return out


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout_s: int = 30,
) -> dict[str, object]:
    req = request.Request(
        str(url),
        data=body,
        headers=dict(headers or {}),
        method=method,
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
        raw_text = raw.decode("utf-8", errors="ignore").strip()
        code = str(details.get("errorCode") or details.get("error") or "").strip()
        msg = str(details.get("errorMessage") or details.get("message") or "").strip()
        suffix = f" ({code})" if code else ""
        if msg:
            raise RuntimeError(f"Epic request failed: {msg}{suffix}") from exc
        if raw_text:
            snippet = raw_text.replace("\r", " ").replace("\n", " ").strip()
            if len(snippet) > 320:
                snippet = snippet[:320].rstrip() + "..."
            raise RuntimeError(
                f"Epic request failed with HTTP {int(exc.code)}{suffix}: {snippet}"
            ) from exc
        raise RuntimeError(f"Epic request failed with HTTP {int(exc.code)}{suffix}") from exc


def _epic_oauth_token(payload: dict[str, str]) -> dict[str, object]:
    encoded = parse.urlencode(payload).encode("utf-8")
    return _http_json(
        _epic_oauth_token_url(),
        method="POST",
        headers={
            "Authorization": f"basic {_EPIC_BASIC_AUTH}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _EPIC_USER_AGENT,
        },
        body=encoded,
        timeout_s=35,
    )


def _oauth_tokens_from_authorization_code(authorization_code: str) -> dict[str, object]:
    return _epic_oauth_token(
        {
            "grant_type": "authorization_code",
            "code": str(authorization_code or "").strip(),
            "token_type": "eg1",
        }
    )


def _oauth_tokens_from_exchange_code(exchange_code: str) -> dict[str, object]:
    return _epic_oauth_token(
        {
            "grant_type": "exchange_code",
            "exchange_code": str(exchange_code or "").strip(),
            "token_type": "eg1",
        }
    )


def _epic_exchange_code_from_sid(sid: str) -> str:
    sid_token = _non_null_token(sid)
    if not sid_token:
        return ""
    cookie_store = cookiejar.CookieJar()
    opener = request.build_opener(request.HTTPCookieProcessor(cookie_store))
    common_headers = {
        "X-Epic-Event-Action": "login",
        "X-Epic-Event-Category": "login",
        "X-Epic-Strategy-Flags": "",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": _EPIC_USER_AGENT,
    }
    req = request.Request(
        _EPIC_SET_SID_URL.format(parse.quote(sid_token, safe="")),
        headers=common_headers,
        method="GET",
    )
    try:
        opener.open(req, timeout=20).read()
    except error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        details = _decode_json(raw)
        code = str(details.get("errorCode") or details.get("error") or "").strip()
        msg = str(details.get("errorMessage") or details.get("message") or "").strip()
        suffix = f" ({code})" if code else ""
        if msg:
            raise RuntimeError(f"Epic request failed: {msg}{suffix}") from exc
        raise RuntimeError(f"Epic request failed with HTTP {int(exc.code)}{suffix}") from exc

    csrf_req = request.Request(
        _EPIC_CSRF_URL,
        headers=common_headers,
        method="GET",
    )
    try:
        opener.open(csrf_req, timeout=20).read()
    except error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        details = _decode_json(raw)
        code = str(details.get("errorCode") or details.get("error") or "").strip()
        msg = str(details.get("errorMessage") or details.get("message") or "").strip()
        suffix = f" ({code})" if code else ""
        if msg:
            raise RuntimeError(f"Epic request failed: {msg}{suffix}") from exc
        raise RuntimeError(f"Epic request failed with HTTP {int(exc.code)}{suffix}") from exc
    xsrf_token = ""
    for cookie in list(cookie_store):
        if str(cookie.name or "").strip().upper() == "XSRF-TOKEN":
            xsrf_token = str(cookie.value or "").strip()
            break
    if not xsrf_token:
        raise RuntimeError("Epic request failed: could not establish CSRF token from sid.")
    xsrf_token = parse.unquote(xsrf_token)
    country = "US"
    try:
        locale_name = str(locale.getdefaultlocale()[0] or "")
        if "_" in locale_name:
            suffix = locale_name.split("_", 1)[1].strip()
            if suffix and len(suffix) == 2:
                country = suffix.upper()
    except Exception:
        country = "US"
    cookie_store.set_cookie(
        cookiejar.Cookie(
            version=0,
            name="EPIC_COUNTRY",
            value=country,
            port=None,
            port_specified=False,
            domain=".epicgames.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=True,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
    )
    headers = dict(common_headers)
    headers["X-XSRF-TOKEN"] = xsrf_token
    exchange_req = request.Request(
        _EPIC_EXCHANGE_GENERATE_URL,
        headers=headers,
        data=b"",
        method="POST",
    )
    try:
        with opener.open(exchange_req, timeout=20) as response:
            payload = _decode_json(response.read())
    except error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        details = _decode_json(raw)
        code = str(details.get("errorCode") or details.get("error") or "").strip()
        msg = str(details.get("errorMessage") or details.get("message") or "").strip()
        if int(exc.code) == 409:
            if msg:
                raise RuntimeError(
                    "Epic sid conflict: session is stale or account action is still pending. "
                    f"{msg}"
                ) from exc
            raise RuntimeError(
                "Epic sid conflict: session is stale or account action is still pending. "
                "Sign in again and paste a fresh redirect JSON/code."
            ) from exc
        suffix = f" ({code})" if code else ""
        if msg:
            raise RuntimeError(f"Epic request failed: {msg}{suffix}") from exc
        raise RuntimeError(f"Epic request failed with HTTP {int(exc.code)}{suffix}") from exc
    exchange_code = str(payload.get("code") or payload.get("exchangeCode") or "").strip()
    if not exchange_code:
        raise RuntimeError("Epic request failed: sid did not produce an exchange code.")
    return exchange_code


def _fetch_epic_account(
    account_id: str,
    *,
    token_type: str,
    access_token: str,
) -> dict[str, object]:
    return _http_json(
        _epic_account_url(account_id),
        headers={
            "Authorization": f"{token_type} {access_token}",
            "User-Agent": _EPIC_USER_AGENT,
        },
        timeout_s=25,
    )


def _fetch_epic_library_records(
    *,
    token_type: str,
    access_token: str,
    progress_cb: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    next_cursor = ""
    page = 0
    while True:
        if should_cancel is not None and should_cancel():
            break
        url = _epic_library_items_url(next_cursor)
        payload = _http_json(
            url,
            headers={
                "Authorization": f"{token_type} {access_token}",
                "User-Agent": _EPIC_USER_AGENT,
            },
            timeout_s=30,
        )
        meta = payload.get("responseMetadata")
        next_cursor_token = ""
        if isinstance(meta, dict):
            next_cursor_token = str(meta.get("nextCursor") or "").strip()
        page += 1
        if progress_cb is not None:
            progress_cb(page, page + (1 if next_cursor_token else 0))
        rows = payload.get("records")
        if isinstance(rows, list):
            out.extend([row for row in rows if isinstance(row, dict)])
        if not isinstance(meta, dict):
            break
        next_cursor = next_cursor_token
        if not next_cursor:
            break
    return out


def _fetch_epic_catalog_item(
    namespace: str,
    catalog_item_id: str,
    *,
    token_type: str,
    access_token: str,
) -> dict[str, object]:
    rows = _fetch_epic_catalog_items(
        namespace,
        [catalog_item_id],
        token_type=token_type,
        access_token=access_token,
    )
    row = rows.get(str(catalog_item_id or "").strip())
    if isinstance(row, dict):
        return row
    return {}


def _fetch_epic_catalog_items(
    namespace: str,
    catalog_item_ids: list[str],
    *,
    token_type: str,
    access_token: str,
) -> dict[str, dict[str, object]]:
    ids = [str(value or "").strip() for value in catalog_item_ids if str(value or "").strip()]
    if not namespace or not ids:
        return {}
    if len(ids) == 1:
        payload = _http_json(
            _epic_catalog_url(namespace, ids[0]),
            headers={
                "Authorization": f"{token_type} {access_token}",
                "User-Agent": _EPIC_USER_AGENT,
            },
            timeout_s=30,
        )
    else:
        bulk_url = _epic_catalog_bulk_url(namespace, ids)
        payload = _http_json(
            bulk_url,
            headers={
                "Authorization": f"{token_type} {access_token}",
                "User-Agent": _EPIC_USER_AGENT,
            },
            timeout_s=30,
        )
    out: dict[str, dict[str, object]] = {}
    if not isinstance(payload, dict):
        return out
    for raw_id in ids:
        row = payload.get(raw_id)
        if isinstance(row, dict):
            out[raw_id] = row
    return out


def _batch_catalog_ids(
    ids: list[str],
    *,
    batch_size: int = _EGS_CATALOG_BATCH_SIZE,
) -> list[list[str]]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw in ids:
        token = str(raw or "").strip()
        if not token:
            continue
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(token)
    if not unique:
        return []
    size = max(1, int(batch_size))
    return [unique[idx : idx + size] for idx in range(0, len(unique), size)]


def _fetch_epic_catalog_items_parallel(
    missing_keys: list[tuple[str, str]],
    *,
    token_type: str,
    access_token: str,
    max_workers: int = _EGS_CATALOG_MAX_WORKERS,
    progress_cb: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict[tuple[str, str], dict[str, object]]:
    grouped: dict[str, list[str]] = {}
    for namespace, catalog_item_id in missing_keys:
        ns = str(namespace or "").strip()
        item_id = str(catalog_item_id or "").strip()
        if not ns or not item_id:
            continue
        grouped.setdefault(ns, []).append(item_id)
    batches: list[tuple[str, list[str]]] = []
    for namespace, ids in grouped.items():
        for chunk in _batch_catalog_ids(ids):
            batches.append((namespace, chunk))
    total_batches = len(batches)
    if total_batches <= 0:
        return {}
    workers = max(1, min(int(max_workers), total_batches))
    out: dict[tuple[str, str], dict[str, object]] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                _fetch_epic_catalog_items,
                namespace,
                batch_ids,
                token_type=token_type,
                access_token=access_token,
            ): (namespace, batch_ids)
            for namespace, batch_ids in batches
        }
        for future in as_completed(future_map):
            completed += 1
            if progress_cb is not None:
                progress_cb(completed, total_batches)
            if should_cancel is not None and should_cancel():
                continue
            namespace, _batch_ids = future_map[future]
            try:
                rows = future.result()
            except Exception:
                continue
            for raw_id, row in rows.items():
                if not isinstance(row, dict):
                    continue
                out[(str(namespace).casefold(), str(raw_id).casefold())] = row
    return out


def _catalog_batch_count(missing_keys: list[tuple[str, str]]) -> int:
    grouped: dict[str, list[str]] = {}
    for namespace, catalog_item_id in missing_keys:
        ns = str(namespace or "").strip()
        item_id = str(catalog_item_id or "").strip()
        if not ns or not item_id:
            continue
        grouped.setdefault(ns, []).append(item_id)
    total = 0
    for ids in grouped.values():
        total += len(_batch_catalog_ids(ids))
    return total


def _resolve_project_data_dir(auth_payload: dict[str, str] | None) -> str:
    payload = dict(auth_payload or {})
    return str(payload.get("project_data_dir") or "").strip()


def _load_catalog_cache_for_sync(
    auth_payload: dict[str, str] | None,
) -> tuple[Path, dict[tuple[str, str], tuple[dict[str, object], float]]]:
    cache_path = _default_egs_catalog_cache_path(
        project_data_dir=_resolve_project_data_dir(auth_payload),
    )
    rows = _load_egs_catalog_cache(cache_path)
    return cache_path, rows


def _save_catalog_cache_for_sync(
    cache_path: Path,
    rows: dict[tuple[str, str], tuple[dict[str, object], float]],
) -> None:
    try:
        _save_egs_catalog_cache(cache_path, rows)
    except Exception:
        # Cache write failures should never fail storefront sync.
        return


def _catalog_cache_entry_to_item(
    cache_rows: dict[tuple[str, str], tuple[dict[str, object], float]],
    key: tuple[str, str],
) -> dict[str, object]:
    row = cache_rows.get(key)
    if row is None:
        return {}
    payload, _fetched_unix = row
    if isinstance(payload, dict):
        return payload
    return {}


def _catalog_keys_from_records(
    records: list[dict[str, object]],
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in records:
        if not isinstance(row, dict):
            continue
        namespace = str(row.get("namespace") or "").strip().casefold()
        catalog_item_id = str(row.get("catalogItemId") or "").strip().casefold()
        if not namespace or not catalog_item_id:
            continue
        key = (namespace, catalog_item_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _epic_manifest_dirs() -> list[Path]:
    program_data = Path(r"C:\ProgramData")
    default = program_data / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    return [default]


def _installed_entitlements_from_manifests() -> list[StoreEntitlement]:
    out: list[StoreEntitlement] = []
    seen: set[str] = set()
    for root in _epic_manifest_dirs():
        if not root.exists() or not root.is_dir():
            continue
        for item in root.glob("*.item"):
            try:
                payload = json.loads(item.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            catalog_id = str(payload.get("CatalogItemId") or "").strip()
            app_name = str(payload.get("AppName") or "").strip()
            title = str(payload.get("DisplayName") or "").strip() or app_name
            install_location = str(payload.get("InstallLocation") or "").strip()
            entitlement = catalog_id or app_name
            if not entitlement or not title:
                continue
            key = entitlement.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(
                StoreEntitlement(
                    entitlement_id=entitlement,
                    title=title,
                    store_game_id=catalog_id or entitlement,
                    manifest_id=app_name or entitlement,
                    install_path=install_location,
                    is_installed=bool(install_location),
                )
            )
    return out


def _catalog_metadata_title(catalog_item: dict[str, object], fallback: str) -> str:
    title = str(catalog_item.get("title") or "").strip()
    if title:
        return title
    return str(fallback or "").strip()


def _is_catalog_item_importable(catalog_item: dict[str, object]) -> bool:
    categories = catalog_item.get("categories")
    if not isinstance(categories, list):
        return True
    category_paths = {
        str(row.get("path") or "").strip().casefold()
        for row in categories
        if isinstance(row, dict)
    }
    if {"digitalextras", "plugins", "plugins/engine"} & category_paths:
        return False
    main_game = catalog_item.get("mainGameItem")
    if isinstance(main_game, dict) and main_game:
        return "addons/launchable" in category_paths
    return True


class EpicConnector(StubLauncherConnector):
    store_name = "EGS"

    def __init__(self) -> None:
        self._updated_refresh_token = ""

    def connect(self, auth_payload: dict[str, str] | None = None) -> StoreAuthResult:
        payload = dict(auth_payload or {})
        parsed = _extract_auth_payload(str(payload.get("authorization_code") or ""))
        # Allow explicit non-primary overrides if caller already split fields.
        for key in ("exchange_code", "sid"):
            value = str(payload.get(key) or "").strip()
            if value:
                parsed[key] = value
        authorization_code = str(parsed.get("authorization_code") or "").strip()
        exchange_code = str(parsed.get("exchange_code") or "").strip()
        sid = str(parsed.get("sid") or "").strip()
        if not authorization_code and not exchange_code and sid:
            exchange_code = _epic_exchange_code_from_sid(sid)
        if not authorization_code and not exchange_code:
            return StoreAuthResult(
                success=False,
                status="missing_authorization_input",
                message=(
                    "Epic connection requires authorization code, exchange code, or sid from Epic sign-in."
                ),
            )
        tokens: dict[str, object]
        if exchange_code:
            tokens = _oauth_tokens_from_exchange_code(exchange_code)
        else:
            try:
                tokens = _oauth_tokens_from_authorization_code(authorization_code)
            except Exception as exc:
                # Epic sometimes returns authorization tokens that only work as exchange_code.
                # Try exchange grant as robust fallback whenever auth_code exchange fails.
                first_error = exc
                try:
                    tokens = _oauth_tokens_from_exchange_code(authorization_code)
                except Exception as fallback_exc:
                    raise RuntimeError(
                        "Epic authorization exchange failed for both grant types. "
                        f"authorization_code error: {first_error}; "
                        f"exchange_code fallback error: {fallback_exc}"
                    ) from fallback_exc
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        access_token = str(tokens.get("access_token") or "").strip()
        token_type = str(tokens.get("token_type") or "bearer").strip() or "bearer"
        account_id = str(tokens.get("account_id") or "").strip()
        if not account_id or not refresh_token or not access_token:
            return StoreAuthResult(
                success=False,
                status="invalid_auth_response",
                message="Epic OAuth response was missing required fields.",
            )
        display_name = account_id
        try:
            account = _fetch_epic_account(
                account_id,
                token_type=token_type,
                access_token=access_token,
            )
            display_name = str(account.get("displayName") or account_id).strip() or account_id
        except Exception:
            display_name = account_id
        return StoreAuthResult(
            success=True,
            account_id=account_id,
            display_name=display_name,
            auth_kind="browser_oauth",
            token_secret=refresh_token,
            expires_utc=str(tokens.get("refresh_expires_at") or tokens.get("expires_at") or "").strip(),
            scopes="",
            status="connected",
            message="Epic account linked.",
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
        self._updated_refresh_token = ""
        if progress_cb is not None:
            progress_cb("EGS sync: local manifests", 5, 100)
        if should_cancel is not None and should_cancel():
            return []
        installed = _installed_entitlements_from_manifests()
        refresh_token = str(token_secret or "").strip()
        if not refresh_token:
            if progress_cb is not None:
                progress_cb("EGS sync: local manifests only", 100, 100)
            return installed
        if progress_cb is not None:
            progress_cb("EGS sync: refresh token", 12, 100)
        if should_cancel is not None and should_cancel():
            return installed
        tokens = _epic_oauth_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "token_type": "eg1",
            }
        )
        self._updated_refresh_token = str(tokens.get("refresh_token") or "").strip()
        access_token = str(tokens.get("access_token") or "").strip()
        token_type = str(tokens.get("token_type") or "bearer").strip() or "bearer"
        resolved_account_id = str(tokens.get("account_id") or "").strip() or str(account_id or "").strip()
        if not access_token or not resolved_account_id:
            if progress_cb is not None:
                progress_cb("EGS sync: token refresh failed", 100, 100)
            return installed
        if progress_cb is not None:
            progress_cb("EGS sync: fetch owned library pages", 18, 100)
        records = _fetch_epic_library_records(
            token_type=token_type,
            access_token=access_token,
            progress_cb=(
                (lambda current, total: progress_cb(
                    f"EGS sync: library pages {current}",
                    min(45, 18 + int(round((current / max(1, total)) * 27.0))),
                    100,
                ))
                if progress_cb is not None
                else None
            ),
            should_cancel=should_cancel,
        )
        if should_cancel is not None and should_cancel():
            return installed
        cache_path, persistent_catalog_cache = _load_catalog_cache_for_sync(auth_payload)
        catalog_cache: dict[tuple[str, str], dict[str, object]] = {
            key: payload
            for key, (payload, _fetched_unix) in persistent_catalog_cache.items()
            if isinstance(payload, dict)
        }
        catalog_lookup_keys = _catalog_keys_from_records(records)
        missing_catalog_keys = [
            key for key in catalog_lookup_keys
            if key not in catalog_cache
        ]
        if missing_catalog_keys:
            planned_batches = max(1, _catalog_batch_count(missing_catalog_keys))
            if progress_cb is not None:
                progress_cb(
                    f"EGS sync: fetch catalog metadata (batches 0/{planned_batches})",
                    45,
                    100,
                )
            fetched_catalog_rows = _fetch_epic_catalog_items_parallel(
                missing_catalog_keys,
                token_type=token_type,
                access_token=access_token,
                max_workers=_EGS_CATALOG_MAX_WORKERS,
                progress_cb=(
                    (lambda done, total: progress_cb(
                        f"EGS sync: fetch catalog metadata batches {done}/{max(1, total)}",
                        min(75, 45 + int(round((max(0, done) / max(1, total)) * 30.0))),
                        100,
                    ))
                    if progress_cb is not None
                    else None
                ),
                should_cancel=should_cancel,
            )
            now_s = time.time()
            for key, row in fetched_catalog_rows.items():
                if not isinstance(row, dict):
                    continue
                catalog_cache[key] = row
                persistent_catalog_cache[key] = (row, now_s)
            if fetched_catalog_rows:
                _save_catalog_cache_for_sync(cache_path, persistent_catalog_cache)
            if should_cancel is not None and should_cancel():
                return installed
        api_rows: list[StoreEntitlement] = []
        seen: set[str] = set()
        total_records = max(1, len(records))
        if progress_cb is not None:
            progress_cb("EGS sync: resolve catalog metadata", 75, 100)
        def _emit_catalog_progress(idx: int) -> None:
            if progress_cb is None:
                return
            if idx != total_records and idx % 25 != 0:
                return
            pct = 75 + int(round((idx / total_records) * 10.0))
            progress_cb(
                f"EGS sync: resolve catalog metadata {idx}/{total_records}",
                min(85, pct),
                100,
            )
        for idx, row in enumerate(records, start=1):
            if should_cancel is not None and should_cancel():
                break
            namespace = str(row.get("namespace") or "").strip()
            sandbox_type = str(row.get("sandboxType") or "").strip().casefold()
            app_name = str(row.get("appName") or "").strip()
            catalog_item_id = str(row.get("catalogItemId") or "").strip()
            asset_id = str(row.get("assetId") or "").strip()
            if namespace.casefold() == "ue":
                _emit_catalog_progress(idx)
                continue
            if sandbox_type == "private":
                _emit_catalog_progress(idx)
                continue
            if app_name.startswith("UE_"):
                _emit_catalog_progress(idx)
                continue
            entitlement_id = catalog_item_id or app_name or asset_id
            if not entitlement_id:
                _emit_catalog_progress(idx)
                continue
            cache_key = (namespace.casefold(), catalog_item_id.casefold())
            catalog_item = {}
            if namespace and catalog_item_id:
                catalog_item = catalog_cache.get(cache_key, {})
            if catalog_item and not _is_catalog_item_importable(catalog_item):
                _emit_catalog_progress(idx)
                continue
            title = _catalog_metadata_title(
                catalog_item,
                fallback=str(row.get("title") or row.get("labelName") or app_name or entitlement_id),
            )
            if not title:
                _emit_catalog_progress(idx)
                continue
            dedupe_key = (
                catalog_item_id or app_name or entitlement_id
            ).casefold()
            if dedupe_key in seen:
                _emit_catalog_progress(idx)
                continue
            seen.add(dedupe_key)
            api_rows.append(
                StoreEntitlement(
                    entitlement_id=entitlement_id,
                    title=title,
                    store_game_id=catalog_item_id or entitlement_id,
                    manifest_id=app_name or entitlement_id,
                    is_installed=False,
                    metadata_json=json.dumps(
                        {
                            "namespace": namespace,
                            "catalog_item_id": catalog_item_id,
                            "app_name": app_name,
                            "asset_id": asset_id,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                )
            )
            _emit_catalog_progress(idx)

        by_store_id: dict[str, StoreEntitlement] = {}
        by_manifest_id: dict[str, StoreEntitlement] = {}
        for row in api_rows:
            store_id_key = str(row.store_game_id or "").strip().casefold()
            manifest_key = str(row.manifest_id or "").strip().casefold()
            if store_id_key:
                by_store_id[store_id_key] = row
            if manifest_key:
                by_manifest_id[manifest_key] = row
        total_installed = max(1, len(installed))
        if progress_cb is not None:
            progress_cb("EGS sync: merge installed markers", 85, 100)
        def _emit_merge_progress(idx: int) -> None:
            if progress_cb is None:
                return
            if idx != total_installed and idx % 25 != 0:
                return
            pct = 85 + int(round((idx / total_installed) * 15.0))
            progress_cb(
                f"EGS sync: merge installed markers {idx}/{total_installed}",
                min(100, pct),
                100,
            )
        for idx, row in enumerate(installed, start=1):
            if should_cancel is not None and should_cancel():
                break
            merged = None
            store_key = str(row.store_game_id or "").strip().casefold()
            manifest_key = str(row.manifest_id or "").strip().casefold()
            if store_key:
                merged = by_store_id.get(store_key)
            if merged is None and manifest_key:
                merged = by_manifest_id.get(manifest_key)
            if merged is None:
                api_rows.append(row)
                _emit_merge_progress(idx)
                continue
            if row.install_path and not merged.install_path:
                merged.install_path = row.install_path
            merged.is_installed = bool(merged.is_installed or row.is_installed)
            _emit_merge_progress(idx)
        if progress_cb is not None:
            progress_cb("EGS sync: done", 100, 100)
        return api_rows

    def updated_token_secret(self) -> str:
        return str(self._updated_refresh_token or "").strip()

    def status(self, account_id: str = "") -> StoreConnectorStatus:
        connected = bool(str(account_id or "").strip())
        return StoreConnectorStatus(
            available=True,
            connected=connected,
            auth_kind="browser_oauth",
            message="Epic account connector using browser authorization code + API sync.",
            metadata={"store_name": self.store_name},
        )


PLUGIN = StorePlugin(
    store_name="EGS",
    connector_cls=EpicConnector,
    auth_kind="browser_oauth",
    supports_full_library_sync=True,
    description="Epic Games Store connector with launcher manifests and account library sync.",
)
