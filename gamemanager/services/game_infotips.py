from __future__ import annotations

import html
import re
from urllib.parse import quote

import requests


_DEFAULT_TIMEOUT_SECONDS = 8.0
_MAX_INFOTIP_CHARS = 220
_WS_RE = re.compile(r"\s+")
_TAG_RE = re.compile(r"<[^>]+>")


def _normalize_text(value: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    text = _WS_RE.sub(" ", text).strip()
    return text


def _first_sentence(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    match = re.search(r"[.!?](?:\s|$)", normalized)
    sentence = normalized if match is None else normalized[: match.end()].strip()
    if len(sentence) <= _MAX_INFOTIP_CHARS:
        return sentence
    clipped = sentence[:_MAX_INFOTIP_CHARS].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,;:-") + "..."


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "GameManager/1.0"})
    return session


def _steam_short_description(
    game_name: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
) -> str:
    cleaned = game_name.strip()
    if not cleaned:
        return ""
    own_session = session is None
    s = _session() if own_session else session
    try:
        search_url = (
            "https://store.steampowered.com/api/storesearch/"
            f"?term={quote(cleaned)}&l=english&cc=us"
        )
        search_resp = s.get(search_url, timeout=timeout_seconds)
        if search_resp.status_code >= 400:
            return ""
        payload = search_resp.json() if search_resp.content else {}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            return ""
        query_cf = cleaned.casefold()

        def _score(item: object) -> int:
            if not isinstance(item, dict):
                return 0
            name = str(item.get("name") or "")
            name_cf = name.casefold()
            score = 0
            if name_cf == query_cf:
                score += 8
            if query_cf in name_cf or name_cf in query_cf:
                score += 3
            query_tokens = [tok for tok in re.split(r"[^a-z0-9]+", query_cf) if tok]
            name_tokens = {tok for tok in re.split(r"[^a-z0-9]+", name_cf) if tok}
            if query_tokens:
                score += sum(1 for tok in query_tokens if tok in name_tokens)
            return score

        best = max(items, key=_score)
        appid = str(best.get("id") or "").strip() if isinstance(best, dict) else ""
        if not appid:
            return ""
        details_url = (
            "https://store.steampowered.com/api/appdetails"
            f"?appids={quote(appid)}&l=english"
        )
        details_resp = s.get(details_url, timeout=timeout_seconds)
        if details_resp.status_code >= 400:
            return ""
        details_payload = details_resp.json() if details_resp.content else {}
        node = details_payload.get(appid) if isinstance(details_payload, dict) else None
        if not isinstance(node, dict) or not node.get("success"):
            return ""
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        raw = str(data.get("short_description") or "")
        return _first_sentence(raw)
    except (requests.RequestException, ValueError):
        return ""
    finally:
        if own_session and s is not None:
            s.close()


def _wikipedia_summary(
    game_name: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
) -> str:
    cleaned = game_name.strip()
    if not cleaned:
        return ""
    own_session = session is None
    s = _session() if own_session else session
    try:
        search_url = "https://en.wikipedia.org/w/api.php"
        search_queries = [f"{cleaned} video game", cleaned]
        title = ""
        for query in search_queries:
            params = {
                "action": "query",
                "list": "search",
                "format": "json",
                "srlimit": "1",
                "srsearch": query,
            }
            resp = s.get(search_url, params=params, timeout=timeout_seconds)
            if resp.status_code >= 400:
                continue
            payload = resp.json() if resp.content else {}
            query_node = payload.get("query") if isinstance(payload, dict) else None
            results = query_node.get("search") if isinstance(query_node, dict) else None
            if not isinstance(results, list) or not results:
                continue
            first = results[0] if isinstance(results[0], dict) else {}
            title = str(first.get("title") or "").strip()
            if title:
                break
        if not title:
            return ""
        summary_url = (
            "https://en.wikipedia.org/api/rest_v1/page/summary/"
            f"{quote(title, safe='')}"
        )
        summary_resp = s.get(summary_url, timeout=timeout_seconds)
        if summary_resp.status_code >= 400:
            return ""
        summary_payload = summary_resp.json() if summary_resp.content else {}
        raw = str(summary_payload.get("extract") or "") if isinstance(summary_payload, dict) else ""
        return _first_sentence(raw)
    except (requests.RequestException, ValueError):
        return ""
    finally:
        if own_session and s is not None:
            s.close()


def fetch_game_infotip(
    cleaned_name: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, str] | None:
    """
    Returns (infotip, source) where source is one of: steam, wikipedia.
    """
    name = cleaned_name.strip()
    if not name:
        return None
    with _session() as s:
        steam_tip = _steam_short_description(
            name,
            timeout_seconds=timeout_seconds,
            session=s,
        )
        if steam_tip:
            return steam_tip, "steam"
        wiki_tip = _wikipedia_summary(
            name,
            timeout_seconds=timeout_seconds,
            session=s,
        )
        if wiki_tip:
            return wiki_tip, "wikipedia"
    return None
