from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Callable

from gamemanager.models import InventoryItem, StoreOwnedGame
from gamemanager.services.storefronts.priority import normalize_store_name, sort_stores


@dataclass(slots=True)
class StoreMatchEvidence:
    inventory_path: str
    store_name: str
    account_id: str
    entitlement_id: str
    match_method: str
    confidence: float
    notes: str = ""


_STORE_METADATA_ID_KEYS: dict[str, tuple[str, ...]] = {
    "Steam": ("steamappid", "steam_appid"),
    "EGS": ("epicgameid", "epic_game_id", "egs_game_id", "catalogitemid"),
    "GOG": ("gogid", "gog_game_id", "gog_product_id"),
    "Itch.io": ("itchid", "itch_game_id"),
    "Humble": ("humbleid", "humble_game_id", "humble_subproduct_id"),
    "Ubisoft": ("ubisoftid", "uplayid", "ubisoft_game_id"),
    "Battle.net": ("bnetid", "battlenetid", "battlenet_game_id"),
    "Amazon Games": ("amazonid", "amazon_game_id", "prime_game_id"),
}
_STEAM_LEGACY_METADATA_ID_KEYS: tuple[str, ...] = ("steamid",)
_STORE_IDS_FILE_NAME = ".gm_store_ids.json"


def _canonical_metadata(meta: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in dict(meta or {}).items():
        key_norm = str(key or "").strip().casefold()
        token = str(value or "").strip()
        if key_norm and token:
            out[key_norm] = token
    return out


def _id_values_for_keys(
    metadata: dict[str, str],
    keys: tuple[str, ...],
    *,
    numeric_only: bool = False,
) -> set[str]:
    values: set[str] = set()
    for key in keys:
        token = str(metadata.get(key, "")).strip()
        if token and (not numeric_only or token.isdigit()):
            values.add(token.casefold())
    return values


def _strong_ids_for_inventory(store_name: str, metadata: dict[str, str]) -> set[str]:
    keys = _STORE_METADATA_ID_KEYS.get(store_name, ())
    numeric_only = store_name == "Steam"
    values = _id_values_for_keys(metadata, keys, numeric_only=numeric_only)
    if values:
        return values
    if store_name == "Steam":
        # Backward-compat: only read legacy SteamID keys when canonical keys are missing.
        return _id_values_for_keys(
            metadata,
            _STEAM_LEGACY_METADATA_ID_KEYS,
            numeric_only=True,
        )
    return values


def _strong_ids_from_inventory_files(store_name: str, inventory_path: str) -> set[str]:
    canonical = normalize_store_name(store_name)
    folder = Path(str(inventory_path or "").strip())
    values: set[str] = set()
    if canonical == "Steam":
        marker = folder / "steam_appid.txt"
        if marker.exists() and marker.is_file():
            try:
                token = marker.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                token = ""
            if token.isdigit():
                values.add(token.casefold())

    ids_file = folder / _STORE_IDS_FILE_NAME
    if ids_file.exists() and ids_file.is_file():
        try:
            parsed = json.loads(ids_file.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, dict):
            lookup_keys = {
                canonical,
                canonical.casefold(),
                canonical.replace(" ", "_").casefold(),
            }
            for key in lookup_keys:
                token = str(parsed.get(key, "")).strip()
                if token and (canonical != "Steam" or token.isdigit()):
                    values.add(token.casefold())
    return values


def preferred_store_id_for_owned_game(
    store_name: str,
    owned: StoreOwnedGame,
) -> str:
    canonical = normalize_store_name(store_name or owned.store_name)
    tokens = [
        str(owned.store_game_id or "").strip(),
        str(owned.manifest_id or "").strip(),
        str(owned.entitlement_id or "").strip(),
    ]
    if canonical == "Steam":
        for token in tokens:
            if token.isdigit():
                return token
        return ""
    for token in tokens:
        if token:
            return token
    return ""


def persist_store_id_hint(
    *,
    inventory_path: str,
    store_name: str,
    store_id: str,
) -> bool:
    canonical = normalize_store_name(store_name)
    folder = Path(str(inventory_path or "").strip())
    token = str(store_id or "").strip()
    if not canonical or not token or not folder.exists() or not folder.is_dir():
        return False

    changed = False
    ids_file = folder / _STORE_IDS_FILE_NAME
    current: dict[str, str] = {}
    if ids_file.exists() and ids_file.is_file():
        try:
            loaded = json.loads(ids_file.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                key_token = str(key or "").strip()
                value_token = str(value or "").strip()
                if key_token and value_token:
                    current[key_token] = value_token

    if current.get(canonical, "") != token:
        current[canonical] = token
        try:
            ids_file.write_text(
                json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return changed
        changed = True

    if canonical == "Steam":
        marker = folder / "steam_appid.txt"
        existing = ""
        if marker.exists() and marker.is_file():
            try:
                existing = marker.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                existing = ""
        if existing != token:
            try:
                marker.write_text(f"{token}\n", encoding="utf-8")
            except OSError:
                return changed
            changed = True
    return changed


def _strong_ids_for_entitlement(row: StoreOwnedGame) -> set[str]:
    values = {
        str(row.entitlement_id or "").strip().casefold(),
        str(row.store_game_id or "").strip().casefold(),
        str(row.manifest_id or "").strip().casefold(),
    }
    return {value for value in values if value}


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_BRACKETED_RE = re.compile(r"\[[^\]]*\]|\([^\)]*\)|\{[^\}]*\}")
_ALPHA_NUM_BOUNDARY_RE = re.compile(r"([a-z])([0-9])|([0-9])([a-z])")
_NOISE_WORDS = {
    "fitgirl",
    "repack",
    "rune",
    "codex",
    "gog",
    "steamrip",
    "portable",
    "update",
    "build",
}


def _title_token(value: str) -> str:
    raw = str(value or "").strip().casefold()
    if not raw:
        return ""
    no_brackets = _BRACKETED_RE.sub(" ", raw)
    separated = _ALPHA_NUM_BOUNDARY_RE.sub(r"\1\3 \2\4", no_brackets)
    words = [part for part in _NON_ALNUM_RE.sub(" ", separated).split() if part]
    words = [part for part in words if part not in _NOISE_WORDS]
    if not words:
        return ""
    return " ".join(words)


def _is_one_edit_apart(a: str, b: str) -> bool:
    a = str(a or "").replace(" ", "")
    b = str(b or "").replace(" ", "")
    if a == b:
        return False
    la = len(a)
    lb = len(b)
    if abs(la - lb) > 1:
        return False
    i = 0
    j = 0
    edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if la > lb:
            i += 1
        elif lb > la:
            j += 1
        else:
            i += 1
            j += 1
    if i < la or j < lb:
        edits += 1
    return edits == 1


def strict_match_inventory_to_owned_games(
    inventory: list[InventoryItem],
    *,
    metadata_loader: Callable[[str], dict[str, str]],
    owned_games_by_store: dict[str, list[StoreOwnedGame]],
    progress_cb: Callable[[str, int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[StoreMatchEvidence]:
    matches: list[StoreMatchEvidence] = []
    matched_inventory_store: set[tuple[str, str]] = set()
    blocked_fallback_inventory_store: set[tuple[str, str]] = set()
    owned_title_index: dict[str, dict[str, list[StoreOwnedGame]]] = {}
    inventory_title_counts: dict[tuple[str, str], int] = {}
    dir_items = [item for item in inventory if item.is_dir]
    total_items = max(1, len(dir_items))

    for raw_store_name, owned_rows in owned_games_by_store.items():
        store_name = normalize_store_name(raw_store_name)
        token_map: dict[str, list[StoreOwnedGame]] = {}
        for owned in owned_rows:
            token = _title_token(owned.title)
            if not token:
                continue
            token_map.setdefault(token, []).append(owned)
        if token_map:
            owned_title_index[store_name] = token_map

    if progress_cb is not None:
        progress_cb("Rebuild links: index titles", 0, total_items)
    for idx, item in enumerate(dir_items, start=1):
        if should_cancel is not None and should_cancel():
            return []
        inv_token = _title_token(item.cleaned_name or item.full_name)
        if not inv_token:
            if progress_cb is not None:
                progress_cb("Rebuild links: index titles", idx, total_items)
            continue
        for raw_store_name in owned_games_by_store.keys():
            store_name = normalize_store_name(raw_store_name)
            inventory_title_counts[(store_name, inv_token)] = (
                inventory_title_counts.get((store_name, inv_token), 0) + 1
            )
        if progress_cb is not None:
            progress_cb("Rebuild links: index titles", idx, total_items)

    if progress_cb is not None:
        progress_cb("Rebuild links: match strong IDs", 0, total_items)
    for idx, item in enumerate(dir_items, start=1):
        if should_cancel is not None and should_cancel():
            return []
        metadata = _canonical_metadata(metadata_loader(item.full_path))
        if not metadata:
            if progress_cb is not None:
                progress_cb("Rebuild links: match strong IDs", idx, total_items)
            continue
        for raw_store_name, owned_rows in owned_games_by_store.items():
            store_name = normalize_store_name(raw_store_name)
            inv_file_ids = _strong_ids_from_inventory_files(store_name, item.full_path)
            inv_ids = set(inv_file_ids)
            if store_name == "Steam":
                # Steam appid file/ids hint is authoritative when present.
                if not inv_ids:
                    inv_ids.update(_strong_ids_for_inventory(store_name, metadata))
            else:
                inv_ids.update(_strong_ids_for_inventory(store_name, metadata))
            if not inv_ids:
                continue
            matched_by_strong_id = False
            for owned in owned_rows:
                if normalize_store_name(owned.store_name) != store_name:
                    continue
                ent_ids = _strong_ids_for_entitlement(owned)
                if not ent_ids:
                    continue
                if inv_ids.isdisjoint(ent_ids):
                    continue
                matches.append(
                    StoreMatchEvidence(
                        inventory_path=item.full_path,
                        store_name=store_name,
                        account_id=owned.account_id,
                        entitlement_id=owned.entitlement_id,
                        match_method="strong_id",
                        confidence=1.0,
                        notes="Matched inventory metadata id to owned entitlement id.",
                    )
                )
                matched_by_strong_id = True
                matched_inventory_store.add((item.full_path, store_name))
            if not matched_by_strong_id:
                # Explicit local IDs are treated as stronger evidence than title fallbacks.
                blocked_fallback_inventory_store.add((item.full_path, store_name))
        if progress_cb is not None:
            progress_cb("Rebuild links: match strong IDs", idx, total_items)

    # Fallback: exact title match only when unique on both sides.
    if progress_cb is not None:
        progress_cb("Rebuild links: match exact titles", 0, total_items)
    for idx, item in enumerate(dir_items, start=1):
        if should_cancel is not None and should_cancel():
            return []
        inv_title_token = _title_token(item.cleaned_name or item.full_name)
        if not inv_title_token:
            if progress_cb is not None:
                progress_cb("Rebuild links: match exact titles", idx, total_items)
            continue
        for raw_store_name, owned_rows in owned_games_by_store.items():
            store_name = normalize_store_name(raw_store_name)
            if (item.full_path, store_name) in matched_inventory_store:
                continue
            if (item.full_path, store_name) in blocked_fallback_inventory_store:
                continue
            token_map = owned_title_index.get(store_name, {})
            candidates = token_map.get(inv_title_token, [])
            if len(candidates) != 1:
                continue
            if inventory_title_counts.get((store_name, inv_title_token), 0) != 1:
                continue
            owned = candidates[0]
            matches.append(
                StoreMatchEvidence(
                    inventory_path=item.full_path,
                    store_name=store_name,
                    account_id=owned.account_id,
                    entitlement_id=owned.entitlement_id,
                    match_method="exact_title_unique",
                    confidence=1.0,
                    notes="Matched by unique exact normalized title.",
                )
            )
            matched_inventory_store.add((item.full_path, store_name))
        if progress_cb is not None:
            progress_cb("Rebuild links: match exact titles", idx, total_items)
    return matches


def ownership_map_from_store_links(store_links: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        path: sort_stores(stores)
        for path, stores in store_links.items()
        if stores
    }
