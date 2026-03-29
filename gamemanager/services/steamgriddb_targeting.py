from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import quote

import requests

from gamemanager.models import SgdbGameCandidate
from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services.normalization import collapse_whitespace, normalize_separators
from gamemanager.services.steamgriddb_upload import (
    SgdbGameDetails,
    resolve_game_by_platform_id,
    search_games,
)


_EDITION_TAIL_RE = re.compile(
    r"(?i)\s*(?:-|:)?\s*(?:goty|game of the year|definitive|remastered|complete|ultimate|deluxe|anniversary|enhanced|director(?:'s)? cut|collection|edition)\s*$"
)
_WORD_RE = re.compile(r"[a-z0-9]+")
_RUNGAME_RE = re.compile(r"steam://rungameid/(\d+)", re.IGNORECASE)
_STEAM_STORE_APPID_SCORE_MIN = 0.90


@dataclass(slots=True)
class _CandidateBuilder:
    game_id: int
    title: str
    score: float = 0.0
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)
    steam_appid: str | None = None


def _tokenize(value: str) -> set[str]:
    return {token for token in _WORD_RE.findall(value.casefold()) if token}


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
    left = collapse_whitespace(str(a or "")).casefold()
    right = collapse_whitespace(str(b or "")).casefold()
    if not left or not right:
        return 0.0
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
    scored: list[tuple[float, bool, str]] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        appid = str(row.get("id") or "").strip()
        if not appid.isdigit():
            continue
        title = collapse_whitespace(str(row.get("name") or ""))
        exact_compact = bool(query_compact) and query_compact == _compact_alnum(title)
        sim = name_similarity(token, title)
        overlap = _token_overlap(token, title)
        score = (0.7 * sim) + (0.3 * overlap)
        if exact_compact:
            score = 1.0
        scored.append((score, exact_compact, appid))
    scored.sort(key=lambda pair: (pair[0], 1 if pair[1] else 0), reverse=True)
    accepted: list[str] = []
    for score, exact_compact, appid in scored:
        if not exact_compact and score < _STEAM_STORE_APPID_SCORE_MIN:
            continue
        accepted.append(appid)
        if len(accepted) >= 3:
            break
    return accepted


def discover_steam_appids(
    folder_path: str,
    cleaned_name: str,
    full_name: str,
) -> list[str]:
    folder = Path(folder_path)
    found: list[str] = []
    seen: set[str] = set()

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


def _upsert_candidate(
    by_game_id: dict[int, _CandidateBuilder],
    game_id: int,
    title: str,
    *,
    score: float,
    confidence: float,
    evidence: str,
    steam_appid: str | None = None,
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


def _confidence_from_name_match(query: str, title: str, evidence_count: int) -> tuple[float, float]:
    sim = name_similarity(query, title)
    overlap = _token_overlap(query, title)
    raw = (0.62 * sim) + (0.38 * overlap)
    confidence = min(0.94, (0.45 + (0.50 * raw) + (0.02 * max(0, evidence_count - 1))))
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
    )


def resolve_target_candidates(
    settings: IconSearchSettings,
    *,
    folder_path: str,
    cleaned_name: str,
    full_name: str,
) -> tuple[list[SgdbGameCandidate], list[str], int | None]:
    if not settings.steamgriddb_enabled or not settings.steamgriddb_api_key.strip():
        return [], [], None
    folder_name = os.path.basename(folder_path.rstrip("\\/"))
    variants = build_name_variants(cleaned_name, folder_name, full_name)
    appids = discover_steam_appids(folder_path, cleaned_name, full_name)

    by_game_id: dict[int, _CandidateBuilder] = {}
    exact_appid_game_id: int | None = None

    for appid in appids:
        try:
            details: SgdbGameDetails = resolve_game_by_platform_id(settings, "steam", appid)
        except Exception:
            continue
        _upsert_candidate(
            by_game_id,
            details.game_id,
            details.title,
            score=1.0,
            confidence=1.0,
            evidence=f"Exact Steam AppID {appid}",
            steam_appid=appid,
        )
        exact_appid_game_id = int(details.game_id)

    for variant in variants:
        try:
            matches = search_games(settings, variant, limit=8)
        except Exception:
            matches = []
        for match in matches:
            raw_score, confidence = _confidence_from_name_match(variant, match.title, 1)
            if confidence < 0.52:
                continue
            _upsert_candidate(
                by_game_id,
                match.game_id,
                match.title,
                score=raw_score,
                confidence=confidence,
                evidence=f"Name match '{variant}'",
                steam_appid=match.steam_appid,
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
