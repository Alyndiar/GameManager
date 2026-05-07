from __future__ import annotations

STORE_PRIORITY_ORDER: tuple[str, ...] = (
    "Steam",
    "EGS",
    "GOG",
    "Itch.io",
    "Humble",
    "Ubisoft",
    "Battle.net",
    "Amazon Games",
)

STORE_NAME_ALIASES: dict[str, str] = {
    "steam": "Steam",
    "valve": "Steam",
    "epic": "EGS",
    "egs": "EGS",
    "epic games": "EGS",
    "epic games store": "EGS",
    "gog": "GOG",
    "gog.com": "GOG",
    "itch": "Itch.io",
    "itchio": "Itch.io",
    "itch.io": "Itch.io",
    "humble": "Humble",
    "humble bundle": "Humble",
    "ubisoft": "Ubisoft",
    "ubisoft connect": "Ubisoft",
    "uplay": "Ubisoft",
    "battle.net": "Battle.net",
    "battlenet": "Battle.net",
    "blizzard": "Battle.net",
    "amazon": "Amazon Games",
    "amazon games": "Amazon Games",
    "prime gaming": "Amazon Games",
}

STORE_SHORT_LABELS: dict[str, str] = {
    "Steam": "S",
    "EGS": "E",
    "GOG": "G",
    "Itch.io": "I",
    "Humble": "H",
    "Ubisoft": "U",
    "Battle.net": "B",
    "Amazon Games": "A",
}

STORE_BADGE_COLORS: dict[str, str] = {
    "Steam": "#2A475E",
    "EGS": "#222222",
    "GOG": "#5C2D91",
    "Itch.io": "#FA5C5C",
    "Humble": "#4A77A8",
    "Ubisoft": "#1C1C1C",
    "Battle.net": "#006FBF",
    "Amazon Games": "#FF9900",
}

_PRIORITY_INDEX = {name: idx for idx, name in enumerate(STORE_PRIORITY_ORDER)}


def normalize_store_name(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    cf = token.casefold()
    resolved = STORE_NAME_ALIASES.get(cf)
    if resolved:
        return resolved
    for canonical in STORE_PRIORITY_ORDER:
        if canonical.casefold() == cf:
            return canonical
    return token


def store_sort_key(value: str) -> tuple[int, str]:
    canonical = normalize_store_name(value)
    rank = _PRIORITY_INDEX.get(canonical, len(STORE_PRIORITY_ORDER))
    return rank, canonical.casefold()


def sort_stores(values: list[str]) -> list[str]:
    normalized = [normalize_store_name(value) for value in values]
    deduped = sorted({value for value in normalized if value}, key=store_sort_key)
    return deduped


def primary_store(values: list[str]) -> str | None:
    ordered = sort_stores(values)
    if not ordered:
        return None
    return ordered[0]
