from __future__ import annotations

import re
from urllib.parse import quote_plus

from gamemanager.services.storefronts.priority import normalize_store_name

_EGS_INTERNAL_ID_RE = re.compile(r"^[a-f0-9]{32}$", re.IGNORECASE)
_EGS_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,127}$")
_GOG_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,127}$", re.IGNORECASE)
_ITCH_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_ITCH_DOMAIN_PATH_RE = re.compile(
    r"^[a-z0-9][a-z0-9-]{0,62}\.itch\.io/[a-z0-9][a-z0-9_-]{0,127}$",
    re.IGNORECASE,
)


def _egs_store_url(*, store_game_id: str, title: str) -> str:
    token = str(store_game_id or "").strip().strip("/")
    query = quote_plus(str(title or "").strip()) if str(title or "").strip() else ""
    if token:
        lowered = token.casefold()
        if lowered.startswith("p/"):
            slug = lowered.split("/", 1)[1].strip()
            if slug and _EGS_SLUG_RE.fullmatch(slug):
                return f"https://store.epicgames.com/en-US/p/{slug}"
        if _EGS_SLUG_RE.fullmatch(lowered) and not _EGS_INTERNAL_ID_RE.fullmatch(lowered):
            return f"https://store.epicgames.com/en-US/p/{lowered}"
    # Catalog/offer ids are often opaque hex tokens and do not resolve well as browse queries.
    if query:
        return (
            f"https://store.epicgames.com/en-US/browse?q={query}"
            "&sortBy=relevancy&sortDir=DESC&count=40"
        )
    if token and not _EGS_INTERNAL_ID_RE.fullmatch(token):
        return (
            f"https://store.epicgames.com/en-US/browse?q={quote_plus(token)}"
            "&sortBy=relevancy&sortDir=DESC&count=40"
        )
    return "https://store.epicgames.com/"


def _gog_store_url(*, store_game_id: str, title: str) -> str:
    token = str(store_game_id or "").strip().strip("/")
    query = quote_plus(str(title or "").strip()) if str(title or "").strip() else ""
    if token:
        lowered = token.casefold()
        if lowered.startswith("game/"):
            return f"https://www.gog.com/en/{lowered}"
        if "/game/" in lowered:
            idx = lowered.find("/game/")
            path = lowered[idx + 1 :].strip("/")
            if path:
                return f"https://www.gog.com/en/{path}"
        if _GOG_SLUG_RE.fullmatch(lowered):
            return f"https://www.gog.com/en/game/{lowered}"
    return f"https://www.gog.com/en/games?query={query}" if query else "https://www.gog.com/"


def _itch_store_url(*, store_game_id: str, title: str) -> str:
    token = str(store_game_id or "").strip()
    query = quote_plus(str(title or "").strip()) if str(title or "").strip() else ""
    if token:
        if _ITCH_URL_RE.match(token):
            return token
        normalized = token.strip("/")
        if normalized.startswith("itch.io/"):
            return f"https://{normalized}"
        if _ITCH_DOMAIN_PATH_RE.fullmatch(normalized):
            return f"https://{normalized}"
    return f"https://itch.io/search?q={query}" if query else "https://itch.io/"


def store_game_url(
    store_name: str,
    *,
    store_game_id: str = "",
    title: str = "",
) -> str:
    canonical = normalize_store_name(store_name)
    game_id = str(store_game_id or "").strip()
    game_title = str(title or "").strip()
    query = quote_plus(game_title) if game_title else ""

    if canonical == "Steam":
        if game_id.isdigit():
            return f"https://store.steampowered.com/app/{game_id}/"
        return f"https://store.steampowered.com/search/?term={query}" if query else "https://store.steampowered.com/"
    if canonical == "EGS":
        return _egs_store_url(store_game_id=game_id, title=game_title)
    if canonical == "GOG":
        return _gog_store_url(store_game_id=game_id, title=game_title)
    if canonical == "Itch.io":
        return _itch_store_url(store_game_id=game_id, title=game_title)
    if canonical == "Humble":
        return (
            f"https://www.humblebundle.com/store/search?sort=bestselling&search={query}"
            if query
            else "https://www.humblebundle.com/store"
        )
    if canonical == "Ubisoft":
        return (
            f"https://store.ubisoft.com/search?lang=en_US&q={query}"
            if query
            else "https://store.ubisoft.com/"
        )
    if canonical == "Battle.net":
        return f"https://shop.battle.net/search?query={query}" if query else "https://shop.battle.net/"
    if canonical == "Amazon Games":
        return f"https://gaming.amazon.com/search?k={query}" if query else "https://gaming.amazon.com/"
    if query:
        return f"https://www.google.com/search?q={query}+{quote_plus(canonical + ' store')}"
    return "https://www.google.com/"
