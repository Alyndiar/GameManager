from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

import requests

from gamemanager.models import SgdbGameCandidate
from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services.normalization import collapse_whitespace, normalize_separators
from gamemanager.services.storefronts.priority import STORE_PRIORITY_ORDER, normalize_store_name
from gamemanager.services.steamgriddb_upload import (
    SgdbGameDetails,
    resolve_game_by_platform_id,
    search_games,
)


_EDITION_TAIL_RE = re.compile(
    r"(?i)\s*(?:-|:)?\s*(?:goty|game of the year|definitive|remastered|complete|ultimate|deluxe|anniversary|enhanced|director(?:'s)? cut|collection|edition)\s*$"
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_NAME_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_RUNGAME_RE = re.compile(r"steam://rungameid/(\d+)", re.IGNORECASE)
_STEAM_STORE_APPID_SCORE_MIN = 0.90
_STORE_IDS_FILE_NAME = ".gm_store_ids.json"
_STORE_METADATA_ID_KEYS: dict[str, tuple[str, ...]] = {
    "Steam": ("steamappid", "steam_appid", "steamid"),
    "EGS": ("epicgameid", "epic_game_id", "egs_game_id", "catalogitemid"),
    "GOG": ("gogid", "gog_game_id", "gog_product_id"),
    "Itch.io": ("itchid", "itch_game_id"),
    "Humble": ("humbleid", "humble_game_id", "humble_subproduct_id"),
    "Ubisoft": ("ubisoftid", "uplayid", "ubisoft_game_id"),
    "Battle.net": ("bnetid", "battlenetid", "battlenet_game_id"),
    "Amazon Games": ("amazonid", "amazon_game_id", "prime_game_id"),
}
_STORE_PLATFORM_CANDIDATES: dict[str, tuple[str, ...]] = {
    "Steam": ("steam",),
    "EGS": ("epic", "egs"),
    "GOG": ("gog",),
    "Itch.io": ("itchio", "itch"),
    "Humble": ("humble",),
    "Ubisoft": ("ubisoft", "uplay"),
    "Battle.net": ("battlenet", "blizzard"),
    "Amazon Games": ("amazon", "primegaming"),
}


@dataclass(slots=True)
class _CandidateBuilder:
    game_id: int
    title: str
    score: float = 0.0
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    steam_appid: str | None = None
    identity_store: str | None = None
    identity_store_id: str | None = None
    store_ids: dict[str, str] = field(default_factory=dict)


def _tokenize(value: str) -> set[str]:
    return {token for token in _WORD_RE.findall(normalize_name_for_compare(value)) if token}


def normalize_name_for_compare(value: str) -> str:
    raw = collapse_whitespace(str(value or "")).casefold()
    if not raw:
        return ""
    words = [part for part in _NAME_NORMALIZE_RE.sub(" ", raw).split() if part]
    return " ".join(words)


def _token_overlap(a: str, b: str) -> float:
    ta = _tokenize(a)
    tb = _tokenize(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    if union <= 0:
        return 0.0
    return inter / union


def name_similarity(a: str, b: str) -> float:
    left = normalize_name_for_compare(a)
    right = normalize_name_for_compare(b)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    ratio = SequenceMatcher(None, left, right).ratio()
    overlap = _token_overlap(left, right)
    return max(0.0, min(1.0, (0.65 * ratio) + (0.35 * overlap)))


def _edition_stripped(name: str) -> str:
    value = collapse_whitespace(str(name or ""))
    if not value:
        return ""
    out = _EDITION_TAIL_RE.sub("", value).strip()
    return collapse_whitespace(out)


def _subtitle_light(name: str) -> str:
    value = collapse_whitespace(str(name or ""))
    if ":" not in value:
        return value
    return collapse_whitespace(value.split(":", 1)[0])


def build_name_variants(
    cleaned_name: str,
    folder_name: str,
    full_name: str,
) -> list[str]:
    seeds = [
        collapse_whitespace(cleaned_name),
        collapse_whitespace(folder_name),
        collapse_whitespace(normalize_separators(full_name)),
    ]
    out: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        if not seed:
            continue
        for candidate in (
            seed,
            _edition_stripped(seed),
            _subtitle_light(seed),
            _subtitle_light(_edition_stripped(seed)),
        ):
            value = collapse_whitespace(candidate)
            key = value.casefold()
            if not value or key in seen:
                continue
            seen.add(key)
            out.append(value)
    return out


def _read_appid_from_file(folder_path: Path) -> str | None:
    path = folder_path / "steam_appid.txt"
    if not path.exists() or not path.is_file():
        return None
    try:
        token = path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    return token if token.isdigit() else None


def _read_appids_from_urls(folder_path: Path) -> list[str]:
    out: list[str] = []
    if not folder_path.exists() or not folder_path.is_dir():
        return out
    for candidate in folder_path.glob("*.url"):
        try:
            content = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        match = _RUNGAME_RE.search(content)
        if not match:
            continue
        appid = str(match.group(1) or "").strip()
        if appid.isdigit():
            out.append(appid)
    return out


def _search_steam_store_appids(
    query: str,
    timeout_seconds: float = 8.0,
) -> list[str]:
    def _compact_alnum(value: str) -> str:
        return "".join(ch for ch in str(value or "").casefold() if ch.isalnum())

    token = collapse_whitespace(query)
    if not token:
        return []
    url = (
        "https://store.steampowered.com/api/storesearch/"
        f"?term={quote(token)}&l=english&cc=us"
    )
    try:
        response = requests.get(
            url,
            timeout=max(3.0, float(timeout_seconds)),
            headers={"User-Agent": "GameManager/1.0"},
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
    except Exception:
        return []
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    query_compact = _compact_alnum(token)
    query_norm = normalize_name_for_compare(token)
    scored: list[tuple[float, bool, str]] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        appid = str(row.get("id") or "").strip()
        if not appid.isdigit():
            continue
        title = collapse_whitespace(str(row.get("name") or ""))
        exact_compact = bool(query_compact) and query_compact == _compact_alnum(title)
        exact_norm = bool(query_norm) and query_norm == normalize_name_for_compare(title)
        exact = exact_compact or exact_norm
        sim = name_similarity(token, title)
        overlap = _token_overlap(token, title)
        score = (0.7 * sim) + (0.3 * overlap)
        if exact:
            score = 1.0
        scored.append((score, exact, appid))
    scored.sort(key=lambda pair: (pair[0], 1 if pair[1] else 0), reverse=True)
    exact_only: list[str] = [appid for _score, exact, appid in scored if exact]
    if exact_only:
        return exact_only[:3]
    accepted: list[str] = []
    for score, exact, appid in scored:
        if not exact and score < _STEAM_STORE_APPID_SCORE_MIN:
            continue
        accepted.append(appid)
        if len(accepted) >= 3:
            break
    return accepted


def _steam_store_name_for_appid(
    appid: str,
    timeout_seconds: float = 8.0,
) -> str:
    token = str(appid or "").strip()
    if not token.isdigit():
        return ""
    try:
        response = requests.get(
            f"https://store.steampowered.com/api/appdetails?appids={quote(token)}&l=english&cc=us",
            timeout=max(3.0, float(timeout_seconds)),
            headers={"User-Agent": "GameManager/1.0"},
        )
        response.raise_for_status()
        payload = response.json() if response.content else {}
    except Exception:
        return ""
    node = payload.get(token, {}) if isinstance(payload, dict) else {}
    if not isinstance(node, dict):
        return ""
    if not bool(node.get("success")):
        return ""
    data = node.get("data")
    if not isinstance(data, dict):
        return ""
    return collapse_whitespace(str(data.get("name") or ""))


def discover_steam_appids(
    folder_path: str,
    cleaned_name: str,
    full_name: str,
    *,
    include_assigned_hints: bool = True,
) -> list[str]:
    folder = Path(folder_path)
    found: list[str] = []
    seen: set[str] = set()

    if include_assigned_hints:
        file_appid = _read_appid_from_file(folder)
        if file_appid and file_appid not in seen:
            seen.add(file_appid)
            found.append(file_appid)

    for appid in _read_appids_from_urls(folder):
        if appid not in seen:
            seen.add(appid)
            found.append(appid)

    folder_name = os.path.basename(folder_path.rstrip("\\/"))
    for variant in build_name_variants(cleaned_name, folder_name, full_name):
        for appid in _search_steam_store_appids(variant):
            if appid in seen:
                continue
            seen.add(appid)
            found.append(appid)
    return found


def _canonical_metadata_from_desktop_ini(folder_path: str) -> dict[str, str]:
    desktop_ini = Path(folder_path) / "desktop.ini"
    if not desktop_ini.exists() or not desktop_ini.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        for raw_line in desktop_ini.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = str(raw_line or "").strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key_token = str(key or "").strip().casefold()
            value_token = str(value or "").strip()
            if key_token and value_token:
                out[key_token] = value_token
    except OSError:
        return {}
    return out


def _read_store_ids_file(folder_path: str) -> dict[str, str]:
    path = Path(folder_path) / _STORE_IDS_FILE_NAME
    if not path.exists() or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in payload.items():
        canonical = normalize_store_name(str(key or "").strip())
        token = str(value or "").strip()
        if canonical and token:
            out[canonical] = token
    return out


def _store_id_hints_from_metadata(
    store_name: str,
    metadata: dict[str, str],
) -> list[str]:
    canonical = normalize_store_name(store_name)
    keys = _STORE_METADATA_ID_KEYS.get(canonical, ())
    out: list[str] = []
    seen: set[str] = set()
    for key in keys:
        token = str(metadata.get(key, "")).strip()
        if not token or token in seen:
            continue
        if canonical == "Steam" and not token.isdigit():
            continue
        seen.add(token)
        out.append(token)
    return out


def discover_store_identity_hints(
    folder_path: str,
    cleaned_name: str,
    full_name: str,
    *,
    include_assigned_hints: bool = True,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    hints: dict[str, list[str]] = {store: [] for store in STORE_PRIORITY_ORDER}
    names: dict[str, list[str]] = {}
    folder = Path(folder_path)
    metadata = _canonical_metadata_from_desktop_ini(folder_path)
    seen_by_store: dict[str, set[str]] = {store: set() for store in STORE_PRIORITY_ORDER}

    def _add(store_name: str, value: str) -> None:
        canonical = normalize_store_name(store_name)
        token = str(value or "").strip()
        if not canonical or not token:
            return
        seen = seen_by_store.setdefault(canonical, set())
        if token in seen:
            return
        seen.add(token)
        hints.setdefault(canonical, []).append(token)

    if include_assigned_hints:
        for store_name, token in _read_store_ids_file(folder_path).items():
            _add(store_name, token)
        for token in _store_id_hints_from_metadata(
            "Steam",
            metadata,
        ):
            _add("Steam", token)
        for store_name in STORE_PRIORITY_ORDER:
            if store_name == "Steam":
                continue
            for token in _store_id_hints_from_metadata(
                store_name,
                metadata,
            ):
                _add(store_name, token)
        steam_file = _read_appid_from_file(folder)
        if steam_file:
            _add("Steam", steam_file)

    for appid in _read_appids_from_urls(folder):
        _add("Steam", appid)

    folder_name = os.path.basename(folder_path.rstrip("\\/"))
    steam_name_terms: list[str] = []
    seen_terms: set[str] = set()
    for variant in build_name_variants(cleaned_name, folder_name, full_name):
        for appid in _search_steam_store_appids(variant):
            _add("Steam", appid)
        variant_key = normalize_name_for_compare(variant)
        if variant_key and variant_key not in seen_terms:
            seen_terms.add(variant_key)
            steam_name_terms.append(variant)
    for appid in hints.get("Steam", []):
        title = _steam_store_name_for_appid(appid)
        key = normalize_name_for_compare(title)
        if key and key not in seen_terms:
            seen_terms.add(key)
            steam_name_terms.append(title)
    if steam_name_terms:
        names["Steam"] = steam_name_terms
    return hints, names


def _upsert_candidate(
    by_game_id: dict[int, _CandidateBuilder],
    game_id: int,
    title: str,
    *,
    score: float,
    confidence: float,
    evidence: str,
    steam_appid: str | None = None,
    identity_store: str | None = None,
    identity_store_id: str | None = None,
    store_ids: dict[str, str] | None = None,
) -> None:
    existing = by_game_id.get(int(game_id))
    if existing is None:
        existing = _CandidateBuilder(game_id=int(game_id), title=title.strip() or f"Game {game_id}")
        by_game_id[int(game_id)] = existing
    existing.score = max(existing.score, float(score))
    existing.confidence = max(existing.confidence, float(confidence))
    if evidence and evidence not in existing.evidence:
        existing.evidence.append(evidence)
    if steam_appid and not existing.steam_appid:
        existing.steam_appid = steam_appid
    if identity_store and not existing.identity_store:
        existing.identity_store = identity_store
    if identity_store_id and not existing.identity_store_id:
        existing.identity_store_id = identity_store_id
    for key, value in dict(store_ids or {}).items():
        store_key = normalize_store_name(str(key or "").strip())
        token = str(value or "").strip()
        if store_key and token and store_key not in existing.store_ids:
            existing.store_ids[store_key] = token


def _confidence_from_name_match(query: str, title: str, evidence_count: int) -> tuple[float, float]:
    if normalize_name_for_compare(query) == normalize_name_for_compare(title):
        return 1.0, 1.0
    sim = name_similarity(query, title)
    overlap = _token_overlap(query, title)
    raw = (0.62 * sim) + (0.38 * overlap)
    confidence = min(0.99, (0.45 + (0.50 * raw) + (0.02 * max(0, evidence_count - 1))))
    return raw, max(0.0, min(1.0, confidence))


def _to_public(builder: _CandidateBuilder) -> SgdbGameCandidate:
    confidence = min(1.0, builder.confidence + min(0.05, 0.01 * max(0, len(builder.evidence) - 1)))
    if builder.confidence >= 1.0:
        confidence = 1.0
    return SgdbGameCandidate(
        game_id=int(builder.game_id),
        title=builder.title,
        confidence=max(0.0, min(1.0, confidence)),
        evidence=list(builder.evidence),
        steam_appid=builder.steam_appid,
        identity_store=builder.identity_store,
        identity_store_id=builder.identity_store_id,
        store_ids=dict(builder.store_ids),
    )


def resolve_target_candidates(
    settings: IconSearchSettings,
    *,
    folder_path: str,
    cleaned_name: str,
    full_name: str,
    include_assigned_hints: bool = True,
) -> tuple[list[SgdbGameCandidate], list[str], int | None]:
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        return [], [], None
    folder_name = os.path.basename(folder_path.rstrip("\\/"))
    variants = build_name_variants(cleaned_name, folder_name, full_name)
    store_id_hints, store_name_terms = discover_store_identity_hints(
        folder_path,
        cleaned_name,
        full_name,
        include_assigned_hints=include_assigned_hints,
    )

    by_game_id: dict[int, _CandidateBuilder] = {}
    exact_appid_game_ids: set[int] = set()
    matched_hint_store: str = ""
    for store_name in STORE_PRIORITY_ORDER:
        canonical_store = normalize_store_name(store_name)
        ids = [str(value or "").strip() for value in store_id_hints.get(canonical_store, [])]
        ids = [value for value in ids if value]
        if canonical_store == "Steam":
            ids = [value for value in ids if value.isdigit()]
        if not ids:
            continue
        matched_hint_store = canonical_store
        for store_id in ids:
            platforms = _STORE_PLATFORM_CANDIDATES.get(canonical_store, (canonical_store.casefold(),))
            for platform in platforms:
                try:
                    details: SgdbGameDetails = resolve_game_by_platform_id(
                        settings,
                        platform,
                        store_id,
                    )
                except Exception:
                    continue
                _upsert_candidate(
                    by_game_id,
                    details.game_id,
                    details.title,
                    score=1.0,
                    confidence=1.0,
                    evidence=f"Exact {canonical_store} ID {store_id}",
                    steam_appid=details.steam_appid or (store_id if canonical_store == "Steam" else None),
                    identity_store=canonical_store,
                    identity_store_id=store_id,
                    store_ids={canonical_store: store_id},
                )
                if canonical_store == "Steam":
                    exact_appid_game_ids.add(int(details.game_id))
                break
        break

    if by_game_id:
        exact_appid_game_id = (
            next(iter(exact_appid_game_ids))
            if len(exact_appid_game_ids) == 1
            else None
        )
        candidates = [_to_public(value) for value in by_game_id.values()]
        candidates.sort(
            key=lambda item: (
                float(item.confidence),
                len(item.evidence),
                item.title.casefold(),
            ),
            reverse=True,
        )
        return candidates, variants, exact_appid_game_id

    # If a higher-priority store was identified but didn't map directly, prefer that store's names for SGDB search.
    search_terms = variants
    if matched_hint_store:
        preferred_terms = store_name_terms.get(matched_hint_store, [])
        if preferred_terms:
            search_terms = preferred_terms
    for variant in search_terms:
        try:
            matches = search_games(settings, variant, limit=8)
        except Exception:
            matches = []
        for match in matches:
            raw_score, confidence = _confidence_from_name_match(variant, match.title, 1)
            if confidence < 0.52:
                continue
            steam_token = str(match.steam_appid or "").strip()
            steam_token = steam_token if steam_token.isdigit() else ""
            _upsert_candidate(
                by_game_id,
                match.game_id,
                match.title,
                score=raw_score,
                confidence=confidence,
                evidence=f"Name match '{variant}'",
                steam_appid=match.steam_appid,
                identity_store="Steam" if steam_token else None,
                identity_store_id=steam_token or None,
                store_ids={"Steam": steam_token} if steam_token else None,
            )

    candidates = [_to_public(value) for value in by_game_id.values()]
    candidates.sort(
        key=lambda item: (
            float(item.confidence),
            len(item.evidence),
            item.title.casefold(),
        ),
        reverse=True,
    )
    return candidates, variants, None
