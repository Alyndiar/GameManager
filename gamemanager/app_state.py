from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Callable
from urllib import request

from gamemanager.db import Database
from gamemanager.models import (
    IconApplyResult,
    IconCandidate,
    IconRebuildEntry,
    InventoryItem,
    MovePlanItem,
    OperationReport,
    RenamePlanItem,
    RootDisplayInfo,
    RootFolder,
    StoreAccount,
    StoreOwnedGame,
    SgdbGameBinding,
    SgdbGameCandidate,
    SgdbOriginStatus,
    SgdbTargetResolution,
    TagCandidate,
)
from gamemanager.services.icon_repair import repair_absolute_icon_paths
from gamemanager.services.icon_readability import (
    build_rebuild_preview_frames,
    clean_backup_icon_files,
    collect_existing_local_icons,
    is_local_folder_icon,
    rebuild_existing_local_icons,
)
from gamemanager.services.icon_cache import DiskImageCache
from gamemanager.services.icon_pipeline import (
    build_preview_png,
    normalize_background_fill_mode,
)
from gamemanager.services.icon_apply_subprocess import apply_folder_icon_in_subprocess
from gamemanager.services.icon_source_probe_subprocess import probe_icon_source_in_subprocess
from gamemanager.services.sgdb_upload_subprocess import upload_icon_to_sgdb_in_subprocess
from gamemanager.services.icon_origin import (
    detect_sgdb_origin_by_visual,
    icon_fingerprint256_from_ico,
    processed_fingerprint256_from_source_image,
)
from gamemanager.services.game_infotips import fetch_game_infotip
from gamemanager.services.folder_icons import (
    clear_folder_icon_metadata,
    read_folder_icon_metadata as read_legacy_folder_icon_metadata,
    read_folder_info_tip,
    set_folder_info_tip,
)
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.icon_sources import (
    DEFAULT_IGDB_API_BASE,
    DEFAULT_SGDB_ENABLED_RESOURCES,
    DEFAULT_SGDB_RESOURCE_ORDER,
    DEFAULT_STEAMGRIDDB_API_BASE,
    IconSearchSettings,
    download_candidate_image,
    lookup_igdb_store_ids_for_title,
    normalize_sgdb_resources,
    search_icon_candidates,
)
from gamemanager.services.steamgriddb_targeting import (
    name_similarity,
    resolve_target_candidates,
)
from gamemanager.services.steamgriddb_upload import (
    resolve_game_by_platform_id as sgdb_resolve_game_by_platform_id,
    resolve_game_by_id as sgdb_resolve_game_by_id,
)
from gamemanager.services.pillow_image import load_image_rgba_bytes
from gamemanager.services.normalization import canonicalize_tag
from gamemanager.services.operations import (
    build_move_plan,
    build_rename_plan,
    execute_move_plan,
    execute_rename_plan,
)
from gamemanager.services.scan_cache import DirectorySizeCache
from gamemanager.services.scanner import list_root_display_infos, scan_roots
from gamemanager.services.secret_store import delete_secret, get_secret, set_secret
from gamemanager.services.store_linking import (
    ownership_map_from_store_links,
    persist_store_id_hint,
    preferred_store_id_for_owned_game,
    strict_match_inventory_to_owned_games,
)
from gamemanager.services.storefront_sync import StorefrontSyncCoordinator
from gamemanager.services.storefronts.priority import normalize_store_name, primary_store, sort_stores
from gamemanager.services.storefronts.registry import available_store_names, connector_for_store
from gamemanager.services.storefronts.store_urls import store_game_url
from gamemanager.services.tagging import collect_tag_candidates

_STORE_ID_HINT_KEYS: dict[str, tuple[str, ...]] = {
    "Steam": ("steamappid", "steam_appid", "steamid"),
    "EGS": ("epicgameid", "epic_game_id", "egs_game_id", "catalogitemid"),
    "GOG": ("gogid", "gog_game_id", "gog_product_id"),
    "Itch.io": ("itchid", "itch_game_id"),
    "Humble": ("humbleid", "humble_game_id", "humble_subproduct_id"),
    "Ubisoft": ("ubisoftid", "uplayid", "ubisoft_game_id"),
    "Battle.net": ("bnetid", "battlenetid", "battlenet_game_id"),
    "Amazon Games": ("amazonid", "amazon_game_id", "prime_game_id"),
}
_STORE_IDS_FILENAME = ".gm_store_ids.json"
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


class AppState:
    def __init__(self, db_path: Path):
        self.db = Database(db_path)
        cache_root = self.db.db_path.parent / "cache"
        self.preview_cache = DiskImageCache(cache_root / "candidate_previews")
        self.download_cache = DiskImageCache(cache_root / "candidate_downloads")
        self.dir_size_cache = DirectorySizeCache(cache_root / "dir_sizes.json")

    @staticmethod
    def _pref_bool(value: str) -> bool:
        return value.strip().casefold() not in {"0", "false", "no", "off", ""}

    @staticmethod
    def _normalize_preview_payload(payload: bytes) -> bytes:
        if not payload:
            return payload
        try:
            image = load_image_rgba_bytes(payload, preferred_ico_size=256)
            out = BytesIO()
            image.save(out, format="PNG")
            return out.getvalue()
        except Exception:
            return payload

    def _perf_scan_workers(self) -> int | None:
        raw = self.get_ui_pref("perf_scan_size_workers", "0").strip()
        try:
            parsed = int(raw)
        except ValueError:
            return None
        if parsed <= 0:
            return None
        return max(1, min(64, parsed))

    def _perf_progress_interval_s(self) -> float:
        raw = self.get_ui_pref("perf_progress_interval_ms", "50").strip()
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 50
        parsed = max(10, min(500, parsed))
        return parsed / 1000.0

    def _perf_dir_cache_enabled(self) -> bool:
        return self._pref_bool(self.get_ui_pref("perf_dir_cache_enabled", "1"))

    def _perf_dir_cache_max_entries(self) -> int:
        raw = self.get_ui_pref("perf_dir_cache_max_entries", "200000").strip()
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 200_000
        return max(1_000, min(2_000_000, parsed))

    def list_roots(self) -> list[RootFolder]:
        return self.db.list_roots()

    def add_root(self, path: str) -> str:
        if not path or not path.strip():
            raise ValueError("Path is empty.")
        normalized = os.path.normpath(os.path.abspath(path.strip()))
        root_path = Path(normalized)
        if not root_path.exists():
            raise ValueError(f"Folder does not exist: {normalized}")
        if not root_path.is_dir():
            raise ValueError(f"Path is not a folder: {normalized}")
        inserted = self.db.add_root(str(root_path))
        return "added" if inserted else "duplicate"

    def remove_root(self, root_id: int) -> None:
        self.db.remove_root(root_id)

    def get_ui_pref(self, key: str, default: str) -> str:
        return self.db.get_ui_pref(key, default)

    def set_ui_pref(self, key: str, value: str) -> None:
        self.db.set_ui_pref(key, value)

    @staticmethod
    def _norm_folder_path(folder_path: str) -> str:
        token = str(folder_path or "").strip()
        if not token:
            return ""
        return os.path.normpath(os.path.abspath(token))

    @staticmethod
    def _norm_store_inventory_path(path: str) -> str:
        token = str(path or "").strip()
        if not token:
            return ""
        return os.path.normcase(os.path.normpath(os.path.abspath(token)))

    @staticmethod
    def _serialize_evidence(evidence: list[str]) -> str:
        clean = [str(value).strip() for value in evidence if str(value).strip()]
        return json.dumps(clean, ensure_ascii=False)

    @staticmethod
    def _deserialize_evidence(raw: str) -> list[str]:
        token = str(raw or "").strip()
        if not token:
            return []
        try:
            parsed = json.loads(token)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [str(value).strip() for value in parsed if str(value).strip()]

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _binding_from_row(row_obj) -> SgdbGameBinding | None:
        if row_obj is None:
            return None
        return SgdbGameBinding(
            folder_path=str(row_obj["folder_path"] or ""),
            game_id=int(row_obj["game_id"] or 0),
            game_name=str(row_obj["game_name"] or "").strip(),
            last_confidence=float(row_obj["last_confidence"] or 0.0),
            evidence_json=str(row_obj["evidence_json"] or ""),
            confirmed_at=str(row_obj["confirmed_at"] or ""),
            updated_at=str(row_obj["updated_at"] or ""),
        )

    def get_sgdb_binding(self, folder_path: str) -> SgdbGameBinding | None:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return None
        row = self.db.get_sgdb_binding(normalized)
        return self._binding_from_row(row)

    def save_sgdb_binding(
        self,
        folder_path: str,
        game_id: int,
        game_name: str,
        confidence: float,
        evidence: list[str],
    ) -> None:
        normalized = self._norm_folder_path(folder_path)
        if not normalized or int(game_id) <= 0:
            return
        self.db.upsert_sgdb_binding(
            normalized,
            int(game_id),
            str(game_name or "").strip(),
            float(confidence),
            self._serialize_evidence(evidence),
        )

    def was_uploaded_to_sgdb(
        self,
        folder_path: str,
        game_id: int,
        icon_fingerprint256: str,
    ) -> bool:
        normalized = self._norm_folder_path(folder_path)
        if not normalized or int(game_id) <= 0:
            return False
        return self.db.was_sgdb_icon_uploaded(
            normalized,
            int(game_id),
            str(icon_fingerprint256 or "").strip().casefold(),
        )

    def record_sgdb_upload_event(
        self,
        folder_path: str,
        game_id: int,
        icon_fingerprint256: str,
        status: str,
        note: str = "",
    ) -> None:
        normalized = self._norm_folder_path(folder_path)
        if not normalized or int(game_id) <= 0:
            return
        self.db.add_sgdb_upload_history(
            normalized,
            int(game_id),
            str(icon_fingerprint256 or "").strip().casefold(),
            str(status or "").strip() or "unknown",
            str(note or "").strip(),
        )

    def latest_sgdb_upload_for_folder(self, folder_path: str) -> dict[str, str]:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return {}
        row = self.db.latest_sgdb_upload_for_folder(normalized)
        if row is None:
            return {}
        return {
            "uploaded_at": str(row["uploaded_at"] or ""),
            "status": str(row["status"] or ""),
            "note": str(row["note"] or ""),
            "game_id": str(row["game_id"] or ""),
        }

    def read_folder_icon_metadata(self, folder_path: str) -> dict[str, str]:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return {}
        metadata = self.db.read_folder_metadata(normalized)
        if metadata:
            return metadata
        legacy = read_legacy_folder_icon_metadata(Path(normalized))
        if not legacy:
            return {}
        self.db.replace_folder_metadata(normalized, legacy)
        clear_folder_icon_metadata(Path(normalized))
        return legacy

    def upsert_folder_icon_metadata(self, folder_path: str, updates: dict[str, str]) -> bool:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return False
        merged = self.read_folder_icon_metadata(normalized)
        changed = False
        for key, value in dict(updates or {}).items():
            if value is None:
                key_token = str(key)
                if key_token in merged:
                    changed = True
                    merged.pop(key_token, None)
                continue
            token = str(value).strip()
            if token:
                key_token = str(key)
                if merged.get(key_token, "") != token:
                    changed = True
                merged[key_token] = token
            else:
                key_token = str(key)
                if key_token in merged:
                    changed = True
                    merged.pop(key_token, None)
        if not changed:
            return False
        self.db.replace_folder_metadata(normalized, merged)
        clear_folder_icon_metadata(Path(normalized))
        return True

    def read_assigned_steam_appid(self, folder_path: str) -> str:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return ""
        marker = Path(normalized) / "steam_appid.txt"
        if marker.exists() and marker.is_file():
            try:
                token = marker.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                token = ""
            if token:
                return token
        metadata = self.read_folder_icon_metadata(normalized)
        for key in ("SteamAppId", "steam_appid", "steamappid", "SteamID", "steamid"):
            token = str(metadata.get(key, "")).strip()
            if token:
                return token
        return ""

    def assign_store_id_hint(
        self,
        *,
        folder_path: str,
        store_name: str,
        store_id: str,
    ) -> bool:
        normalized = self._norm_folder_path(folder_path)
        canonical = normalize_store_name(store_name)
        token = str(store_id or "").strip()
        if not normalized or not canonical or not token:
            return False
        changed = persist_store_id_hint(
            inventory_path=normalized,
            store_name=canonical,
            store_id=token,
        )
        if canonical == "Steam":
            changed = (
                self.upsert_folder_icon_metadata(
                    normalized,
                    {
                        "SteamAppId": token,
                        "steam_appid": token,
                        "steamappid": token,
                        "SteamID": token,
                        "steamid": token,
                    },
                )
                or changed
            )
        return changed

    def assign_steam_appid(self, folder_path: str, steam_appid: str) -> bool:
        return self.assign_store_id_hint(
            folder_path=folder_path,
            store_name="Steam",
            store_id=steam_appid,
        )

    def clear_owned_store_info_for_inventory(self, folder_path: str) -> bool:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return False
        changed = False
        self.db.delete_store_links_for_inventory_path(normalized, include_manual=True)
        ids_file = Path(normalized) / ".gm_store_ids.json"
        if ids_file.exists():
            try:
                ids_file.unlink()
                changed = True
            except OSError:
                pass
        steam_marker = Path(normalized) / "steam_appid.txt"
        if steam_marker.exists():
            try:
                steam_marker.unlink()
                changed = True
            except OSError:
                pass
        changed = (
            self.upsert_folder_icon_metadata(
                normalized,
                {
                    "SteamAppId": None,
                    "steam_appid": None,
                    "steamappid": None,
                    "SteamID": None,
                    "steamid": None,
                },
            )
            or changed
        )
        return changed

    def processed_source_fingerprint256(self, source_image_bytes: bytes) -> str:
        return processed_fingerprint256_from_source_image(source_image_bytes)

    def icon_fingerprint256(self, icon_path: str) -> str:
        return icon_fingerprint256_from_ico(icon_path)

    def record_assigned_icon_source(
        self,
        *,
        folder_path: str,
        source_kind: str,
        source_provider: str,
        source_candidate_id: str = "",
        source_game_id: str = "",
        source_url: str = "",
        source_fingerprint256: str = "",
        source_confidence: float = 0.0,
    ) -> bool:
        normalized_folder = self._norm_folder_path(folder_path)
        normalized_kind = str(source_kind or "unknown").strip().casefold() or "unknown"
        normalized_provider = str(source_provider or "").strip()
        if normalized_kind == "web":
            normalized_provider = "Internet"
        updates = {
            "SourceKind": normalized_kind,
            "SourceProvider": normalized_provider,
            "SourceCandidateId": str(source_candidate_id or "").strip(),
            "SourceGameId": str(source_game_id or "").strip(),
            "SourceUrl": str(source_url or "").strip(),
            "SourceFingerprint256": str(source_fingerprint256 or "").strip().casefold(),
            "SourceConfidence": f"{float(source_confidence):.4f}",
            "SourceAssignedAtUtc": self._utc_now_iso(),
        }
        return self.upsert_folder_icon_metadata(normalized_folder, updates)

    def resolve_sgdb_target(
        self,
        folder_path: str,
        cleaned_name: str,
        full_name: str,
        *,
        include_assigned_hints: bool = True,
    ) -> SgdbTargetResolution:
        settings = self.icon_search_settings()
        candidates, _variants, exact_appid_game_id = resolve_target_candidates(
            settings,
            folder_path=self._norm_folder_path(folder_path),
            cleaned_name=cleaned_name,
            full_name=full_name,
            include_assigned_hints=bool(include_assigned_hints),
        )
        binding = self.get_sgdb_binding(folder_path)
        drift_reasons: list[str] = []
        selected: SgdbGameCandidate | None = None
        requires_confirmation = binding is None
        if binding is not None:
            folder_name = Path(self._norm_folder_path(folder_path)).name
            name_baseline = max(
                name_similarity(binding.game_name, cleaned_name),
                name_similarity(binding.game_name, folder_name),
            )
            if name_baseline < 0.55:
                drift_reasons.append("Saved binding title drifted from folder name.")

            saved_candidate_conf = 0.0
            top_candidate = candidates[0] if candidates else None
            for candidate in candidates:
                if int(candidate.game_id) == int(binding.game_id):
                    saved_candidate_conf = max(saved_candidate_conf, float(candidate.confidence))
                    break
            if (
                top_candidate is not None
                and int(top_candidate.game_id) != int(binding.game_id)
                and (float(top_candidate.confidence) - float(saved_candidate_conf)) >= 0.15
            ):
                drift_reasons.append("Top resolver candidate changed.")

            if (
                exact_appid_game_id is not None
                and int(exact_appid_game_id) != int(binding.game_id)
            ):
                drift_reasons.append("Exact Steam AppID mapping disagrees with saved binding.")

            requires_confirmation = bool(drift_reasons)
            if not requires_confirmation:
                selected = SgdbGameCandidate(
                    game_id=int(binding.game_id),
                    title=binding.game_name,
                    confidence=max(float(binding.last_confidence), 0.99),
                    evidence=self._deserialize_evidence(binding.evidence_json),
                    steam_appid=None,
                )

        return SgdbTargetResolution(
            selected=selected,
            candidates=candidates,
            saved_binding=binding,
            drift_reasons=drift_reasons,
            requires_confirmation=requires_confirmation,
            exact_appid_game_id=exact_appid_game_id,
        )

    def resolve_sgdb_game_by_id(self, game_id: int) -> SgdbGameCandidate:
        details = sgdb_resolve_game_by_id(self.icon_search_settings(), int(game_id))
        steam_appid = str(details.steam_appid or "").strip()
        store_ids = {"Steam": steam_appid} if steam_appid.isdigit() else {}
        return SgdbGameCandidate(
            game_id=int(details.game_id),
            title=details.title,
            confidence=1.0,
            evidence=[f"Manual SGDB ID {int(game_id)}"],
            steam_appid=details.steam_appid,
            store_ids=store_ids,
        )

    def resolve_sgdb_game_by_store_id(
        self,
        store_name: str,
        store_id: str,
    ) -> SgdbGameCandidate:
        canonical = normalize_store_name(store_name)
        token = str(store_id or "").strip()
        if not canonical or not token:
            raise ValueError("Store name and store ID are required.")
        platform_candidates: dict[str, tuple[str, ...]] = {
            "Steam": ("steam",),
            "EGS": ("epic", "egs"),
            "GOG": ("gog",),
            "Itch.io": ("itchio", "itch"),
            "Humble": ("humble",),
            "Ubisoft": ("ubisoft", "uplay"),
            "Battle.net": ("battlenet", "blizzard"),
            "Amazon Games": ("amazon", "primegaming"),
        }
        candidates = platform_candidates.get(canonical, (canonical.casefold(),))
        last_error: Exception | None = None
        for platform in candidates:
            try:
                details = sgdb_resolve_game_by_platform_id(
                    self.icon_search_settings(),
                    platform,
                    token,
                )
            except Exception as exc:
                last_error = exc
                continue
            steam_appid = str(details.steam_appid or "").strip()
            if canonical == "Steam" and token.isdigit():
                steam_appid = steam_appid or token
            return SgdbGameCandidate(
                game_id=int(details.game_id),
                title=details.title,
                confidence=1.0,
                evidence=[f"Manual {canonical} ID {token} (platform={platform})"],
                steam_appid=steam_appid or None,
                identity_store=canonical,
                identity_store_id=token,
                store_ids={canonical: token},
            )
        if canonical == "Steam" and token.isdigit():
            title = f"Steam App {token}"
            try:
                url = f"https://store.steampowered.com/api/appdetails?appids={token}&l=english"
                with request.urlopen(url, timeout=12) as response:
                    payload = json.loads(response.read().decode("utf-8", errors="ignore"))
                node = payload.get(token, {}) if isinstance(payload, dict) else {}
                data = node.get("data", {}) if isinstance(node, dict) else {}
                name = str(data.get("name") or "").strip()
                if name:
                    title = name
            except Exception:
                pass
            return SgdbGameCandidate(
                game_id=0,
                title=title,
                confidence=0.95,
                evidence=[f"Manual Steam AppID {token} (no SGDB mapping)"],
                steam_appid=token,
                identity_store=canonical,
                identity_store_id=token,
                store_ids={canonical: token},
            )
        if last_error is not None:
            raise RuntimeError(
                f"Could not resolve SGDB game using {canonical} ID '{token}': {last_error}"
            ) from last_error
        raise RuntimeError(f"Could not resolve SGDB game using {canonical} ID '{token}'.")

    def detect_sgdb_origin_status(
        self,
        *,
        folder_path: str,
        icon_path: str,
        cleaned_name: str,
        full_name: str,
        threshold: float = 0.95,
    ) -> SgdbOriginStatus:
        metadata = self.read_folder_icon_metadata(folder_path)
        source_kind = str(metadata.get("SourceKind", "unknown") or "unknown").strip().casefold()
        source_provider = str(metadata.get("SourceProvider", "")).strip()
        confidence_raw = str(metadata.get("SourceConfidence", "")).strip()
        try:
            metadata_confidence = float(confidence_raw) if confidence_raw else 0.0
        except ValueError:
            metadata_confidence = 0.0
        if source_kind == "sgdb_raw":
            return SgdbOriginStatus(
                source_kind=source_kind,
                source_provider=source_provider or "SteamGridDB",
                is_sgdb_origin=True,
                confidence=max(0.99, metadata_confidence),
                matched_icon_id=None,
                reason="metadata",
            )
        if source_kind == "sgdb_modified":
            return SgdbOriginStatus(
                source_kind=source_kind,
                source_provider=source_provider or "",
                is_sgdb_origin=False,
                confidence=max(0.0, metadata_confidence),
                matched_icon_id=None,
                reason="metadata-modified",
            )

        game_id_token = str(metadata.get("SourceGameId") or "").strip()
        game_id = int(game_id_token) if game_id_token.isdigit() else 0
        if game_id <= 0:
            target = self.resolve_sgdb_target(folder_path, cleaned_name, full_name)
            if target.selected is not None:
                game_id = int(target.selected.game_id)
            elif target.saved_binding is not None:
                game_id = int(target.saved_binding.game_id)

        if game_id <= 0:
            return SgdbOriginStatus(
                source_kind=source_kind or "unknown",
                source_provider=source_provider,
                is_sgdb_origin=False,
                confidence=0.0,
                matched_icon_id=None,
                reason="no-game-id",
            )

        visual = detect_sgdb_origin_by_visual(
            local_icon_path=icon_path,
            game_id=game_id,
            settings=self.icon_search_settings(),
            threshold=float(threshold),
        )
        is_match = float(visual.confidence) >= float(threshold)
        return SgdbOriginStatus(
            source_kind=source_kind or "unknown",
            source_provider=source_provider,
            is_sgdb_origin=is_match,
            confidence=float(visual.confidence),
            matched_icon_id=visual.matched_icon_id,
            reason="visual_match" if is_match else "visual_no_match",
        )

    def is_icon_present_on_sgdb_for_game(
        self,
        *,
        icon_path: str,
        game_id: int,
        threshold: float = 0.95,
    ) -> tuple[bool, float, int | None]:
        if int(game_id) <= 0:
            return False, 0.0, None
        visual = detect_sgdb_origin_by_visual(
            local_icon_path=icon_path,
            game_id=int(game_id),
            settings=self.icon_search_settings(),
            threshold=float(threshold),
        )
        confidence = float(getattr(visual, "confidence", 0.0) or 0.0)
        return confidence >= float(threshold), confidence, getattr(visual, "matched_icon_id", None)

    def upload_folder_icon_to_sgdb(
        self,
        *,
        folder_path: str,
        icon_path: str,
        game: SgdbGameCandidate,
    ) -> OperationReport:
        report = OperationReport(total=1)
        normalized_folder = self._norm_folder_path(folder_path)
        icon_file = Path(icon_path)
        if not normalized_folder or not icon_file.exists() or icon_file.suffix.casefold() != ".ico":
            report.failed = 1
            report.details.append("Missing local .ico icon file.")
            return report
        existing = self.read_folder_icon_metadata(normalized_folder)
        existing_kind = str(existing.get("SourceKind", "")).strip().casefold()
        existing_provider = str(existing.get("SourceProvider", "")).strip().casefold()
        if existing_kind in {"sgdb_raw", "sgdb_modified"} or existing_provider == "steamgriddb":
            report.skipped = 1
            report.details.append("Skipped: icon source is already SteamGridDB.")
            return report
        fingerprint = icon_fingerprint256_from_ico(icon_file)
        already_present, confidence, matched_icon_id = self.is_icon_present_on_sgdb_for_game(
            icon_path=str(icon_file),
            game_id=int(game.game_id),
            threshold=0.95,
        )
        if already_present:
            report.skipped = 1
            if matched_icon_id is not None:
                report.details.append(
                    "Skipped: matching icon already exists on SteamGridDB "
                    f"(icon ID {int(matched_icon_id)}, confidence {confidence:.2f})."
                )
            else:
                report.details.append(
                    "Skipped: matching icon already exists on SteamGridDB "
                    f"(confidence {confidence:.2f})."
                )
            return report
        if self.was_uploaded_to_sgdb(normalized_folder, int(game.game_id), fingerprint):
            report.skipped = 1
            report.details.append("Skipped: icon already uploaded by GameManager for this game.")
            return report

        try:
            upload_result = upload_icon_to_sgdb_in_subprocess(
                self.icon_search_settings(),
                int(game.game_id),
                str(icon_file),
            )
            if not bool(upload_result.get("success")):
                message = str(upload_result.get("error") or "Unknown upload worker error.")
                raise RuntimeError(message)
        except Exception as exc:
            self.record_sgdb_upload_event(
                normalized_folder,
                int(game.game_id),
                fingerprint,
                "failed",
                str(exc),
            )
            report.failed = 1
            report.details.append(f"Upload failed: {exc}")
            return report

        self.record_sgdb_upload_event(
            normalized_folder,
            int(game.game_id),
            fingerprint,
            "uploaded",
            "ok",
        )
        self.save_sgdb_binding(
            normalized_folder,
            int(game.game_id),
            game.title,
            float(game.confidence),
            list(game.evidence),
        )
        self.record_assigned_icon_source(
            folder_path=normalized_folder,
            source_kind="sgdb_raw",
            source_provider="SteamGridDB",
            source_candidate_id=f"upload:{int(game.game_id)}",
            source_game_id=str(int(game.game_id)),
            source_fingerprint256=fingerprint,
            source_confidence=1.0,
        )
        self.upsert_folder_icon_metadata(
            normalized_folder,
            {
                "SourceGameId": str(int(game.game_id)),
            },
        )
        game_steam_appid = str(game.steam_appid or "").strip()
        if game_steam_appid.isdigit():
            self.assign_steam_appid(normalized_folder, game_steam_appid)
        report.succeeded = 1
        report.details.append(f"Uploaded to SteamGridDB game {int(game.game_id)} ({game.title}).")
        return report

    def sgdb_resource_preferences(self) -> tuple[list[str], set[str]]:
        order_raw = self.get_ui_pref("sgdb_resource_order", "")
        enabled_raw = self.get_ui_pref("sgdb_resource_enabled", "")

        try:
            parsed_order = json.loads(order_raw) if order_raw else []
        except json.JSONDecodeError:
            parsed_order = []
        try:
            parsed_enabled = json.loads(enabled_raw) if enabled_raw else []
        except json.JSONDecodeError:
            parsed_enabled = []

        order = normalize_sgdb_resources(
            parsed_order if isinstance(parsed_order, list) else [],
            default_enabled_only=False,
        )
        enabled_list = normalize_sgdb_resources(
            parsed_enabled if isinstance(parsed_enabled, list) else [],
            default_enabled_only=True,
        )
        for value in DEFAULT_SGDB_RESOURCE_ORDER:
            if value not in order:
                order.append(value)
        enabled = {value for value in enabled_list if value in order}
        if not enabled:
            enabled = set(DEFAULT_SGDB_ENABLED_RESOURCES)
        return order, enabled

    def save_sgdb_resource_preferences(
        self, order: list[str], enabled: set[str]
    ) -> tuple[list[str], set[str]]:
        normalized_order = normalize_sgdb_resources(
            order, default_enabled_only=False
        )
        for value in DEFAULT_SGDB_RESOURCE_ORDER:
            if value not in normalized_order:
                normalized_order.append(value)
        normalized_enabled_list = normalize_sgdb_resources(
            [value for value in normalized_order if value in enabled],
            default_enabled_only=True,
        )
        normalized_enabled = set(normalized_enabled_list)
        self.set_ui_pref("sgdb_resource_order", json.dumps(normalized_order))
        self.set_ui_pref("sgdb_resource_enabled", json.dumps(normalized_enabled_list))
        return normalized_order, normalized_enabled

    def approved_tags(self) -> set[str]:
        return {row.canonical_tag for row in self.db.list_tag_rules("approved")}

    def non_tags(self) -> set[str]:
        return {row.canonical_tag for row in self.db.list_tag_rules("non_tag")}

    def refresh(
        self,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> tuple[list[RootDisplayInfo], list[InventoryItem]]:
        roots = self.list_roots()
        root_infos = list_root_display_infos(roots)
        cache_enabled = self._perf_dir_cache_enabled()
        cache_obj = self.dir_size_cache if cache_enabled else None
        if cache_obj is not None:
            cache_obj.set_max_entries(self._perf_dir_cache_max_entries())
        items = scan_roots(
            roots,
            self.approved_tags(),
            root_infos_by_id={info.root_id: info for info in root_infos},
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            dir_size_cache=cache_obj,
            size_workers=self._perf_scan_workers(),
            progress_interval_s=self._perf_progress_interval_s(),
        )
        path_map = self.db.list_store_links_for_paths(
            [item.full_path for item in items if item.is_dir],
            verified_only=True,
        )
        owned_by_path = ownership_map_from_store_links(path_map)
        for item in items:
            if not item.is_dir:
                item.owned_stores = []
                item.primary_store = None
                continue
            key = self._norm_store_inventory_path(item.full_path)
            stores = owned_by_path.get(key, [])
            ordered = sort_stores(stores)
            item.owned_stores = ordered
            item.primary_store = primary_store(ordered)
        if cache_obj is not None:
            cache_obj.save()
        return root_infos, items

    def refresh_roots_only(self) -> list[RootDisplayInfo]:
        roots = self.list_roots()
        return list_root_display_infos(roots)

    def list_store_accounts(self, *, enabled_only: bool = False) -> list[StoreAccount]:
        return self.db.list_store_accounts(enabled_only=enabled_only)

    def available_store_names(self) -> list[str]:
        return available_store_names()

    @staticmethod
    def _store_secret_key(store_name: str, account_id: str) -> str:
        return f"store_token:{normalize_store_name(store_name)}:{str(account_id or '').strip()}"

    @staticmethod
    def _store_link_name_signature(item: InventoryItem) -> str:
        source = str(item.cleaned_name or item.full_name or "").strip().casefold()
        if not source:
            return ""
        return " ".join(part for part in _NON_ALNUM_RE.sub(" ", source).split() if part)

    @staticmethod
    def _store_link_ids_signature_from_json(raw: str) -> dict[str, str]:
        token = str(raw or "").strip()
        if not token:
            return {}
        try:
            parsed = json.loads(token)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in parsed.items():
            canonical_store = normalize_store_name(str(key or "").replace("_", " ").strip())
            value_token = str(value or "").strip().casefold()
            if canonical_store and value_token:
                out[canonical_store] = value_token
        return out

    def _store_link_ids_signature_for_path(self, folder_path: str) -> dict[str, str]:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return {}
        metadata = self.read_folder_icon_metadata(normalized)
        folded_meta = {
            str(key or "").strip().casefold(): str(value or "").strip()
            for key, value in metadata.items()
            if str(key or "").strip() and str(value or "").strip()
        }
        by_store: dict[str, set[str]] = {}
        for store_name, keys in _STORE_ID_HINT_KEYS.items():
            canonical = normalize_store_name(store_name)
            if not canonical:
                continue
            numeric_only = canonical == "Steam"
            values = by_store.setdefault(canonical, set())
            for key in keys:
                token = str(folded_meta.get(str(key or "").strip().casefold(), "")).strip()
                if not token:
                    continue
                if numeric_only and not token.isdigit():
                    continue
                values.add(token.casefold())

        steam_file = Path(normalized) / "steam_appid.txt"
        if steam_file.is_file():
            try:
                token = steam_file.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                token = ""
            if token.isdigit():
                by_store.setdefault("Steam", set()).add(token.casefold())

        ids_file = Path(normalized) / _STORE_IDS_FILENAME
        if ids_file.is_file():
            try:
                parsed = json.loads(ids_file.read_text(encoding="utf-8", errors="ignore"))
            except (OSError, json.JSONDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    canonical = normalize_store_name(str(key or "").replace("_", " ").strip())
                    token = str(value or "").strip()
                    if not canonical or not token:
                        continue
                    if canonical == "Steam" and not token.isdigit():
                        continue
                    by_store.setdefault(canonical, set()).add(token.casefold())

        return {
            store_name: "|".join(sorted(values))
            for store_name, values in by_store.items()
            if values
        }

    @staticmethod
    def _store_link_row_key(row: dict[str, object]) -> tuple[str, str, str]:
        return (
            normalize_store_name(str(row.get("store_name") or "").strip()),
            str(row.get("account_id") or "").strip(),
            str(row.get("entitlement_id") or "").strip(),
        )

    @staticmethod
    def _store_link_row_payload(row: dict[str, object]) -> tuple[str, float, bool, str]:
        return (
            str(row.get("match_method") or "").strip(),
            round(float(row.get("confidence") or 0.0), 6),
            bool(row.get("verified")),
            str(row.get("notes") or "").strip(),
        )

    def connect_store_account(
        self,
        store_name: str,
        auth_payload: dict[str, str] | None = None,
    ) -> StoreAccount | None:
        connector = connector_for_store(store_name)
        if connector is None:
            return None
        result = connector.connect(auth_payload)
        if not result.success:
            return None
        canonical_store = normalize_store_name(store_name)
        account_id = str(result.account_id or "").strip()
        if not account_id:
            return None
        if result.token_secret:
            if not set_secret(self._store_secret_key(canonical_store, account_id), result.token_secret):
                raise RuntimeError(
                    "Could not store store-account token in secure secret storage (Credential Manager)."
                )
        self.db.upsert_store_account(
            canonical_store,
            account_id,
            str(result.display_name or "").strip() or account_id,
            auth_kind=str(result.auth_kind or result.status or "launcher_import"),
            enabled=True,
        )
        self.db.upsert_store_token_meta(
            canonical_store,
            account_id,
            expires_utc=str(result.expires_utc or "").strip(),
            scopes=str(result.scopes or "").strip(),
            status=str(result.status or "").strip(),
        )
        for account in self.db.list_store_accounts():
            if (
                normalize_store_name(account.store_name) == canonical_store
                and account.account_id == account_id
            ):
                return account
        return None

    def disconnect_store_account(self, store_name: str, account_id: str) -> bool:
        canonical_store = normalize_store_name(store_name)
        account = str(account_id or "").strip()
        if not canonical_store or not account:
            return False
        connector = connector_for_store(canonical_store)
        if connector is not None:
            try:
                connector.disconnect(account)
            except Exception:
                pass
        delete_secret(self._store_secret_key(canonical_store, account))
        self.db.delete_store_account(canonical_store, account)
        return True

    def sync_store_accounts(
        self,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        launch_client_by_store: dict[str, bool] | None = None,
    ) -> list[dict[str, object]]:
        coordinator = StorefrontSyncCoordinator(
            self.db,
            token_loader=lambda store, account: get_secret(
                self._store_secret_key(store, account)
            ),
            token_saver=lambda store, account, token: set_secret(
                self._store_secret_key(store, account),
                token,
            ),
        )
        return coordinator.sync_enabled_accounts(
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            launch_client_by_store=launch_client_by_store,
            keep_existing_on_unreachable=True,
        )

    def rebuild_store_links_from_inventory(
        self,
        inventory: list[InventoryItem],
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        force_rebuild_all: bool = False,
    ) -> int:
        accounts = self.db.list_store_accounts(enabled_only=True)
        if not accounts:
            return 0
        owned_by_store: dict[str, list[StoreOwnedGame]] = {}
        owned_lookup: dict[tuple[str, str, str], StoreOwnedGame] = {}
        total_accounts = max(1, len(accounts))
        if progress_cb is not None:
            progress_cb("Rebuild links: load owned libraries", 0, total_accounts)
        for idx, account in enumerate(accounts, start=1):
            if should_cancel is not None and should_cancel():
                return 0
            canonical_store = normalize_store_name(account.store_name)
            rows = self.db.list_store_owned_games(canonical_store, account.account_id)
            if rows:
                owned_by_store.setdefault(canonical_store, []).extend(rows)
                for row in rows:
                    entitlement = str(row.entitlement_id or "").strip()
                    if not entitlement:
                        continue
                    owned_lookup[(canonical_store, account.account_id, entitlement)] = row
            if progress_cb is not None:
                progress_cb("Rebuild links: load owned libraries", idx, total_accounts)
        evidence_rows = strict_match_inventory_to_owned_games(
            inventory,
            metadata_loader=lambda path: self.read_folder_icon_metadata(path),
            owned_games_by_store=owned_by_store,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
        )
        if should_cancel is not None and should_cancel():
            return 0
        dir_items = [item for item in inventory if item.is_dir]
        all_paths: list[str] = []
        current_name_sig_by_path: dict[str, str] = {}
        current_ids_sig_by_path: dict[str, dict[str, str]] = {}
        total_dirs = max(1, len(dir_items))
        if progress_cb is not None:
            progress_cb("Rebuild links: collect local signatures", 0, total_dirs)
        for idx, item in enumerate(dir_items, start=1):
            normalized_path = self._norm_store_inventory_path(item.full_path)
            if normalized_path:
                all_paths.append(normalized_path)
                current_name_sig_by_path[normalized_path] = self._store_link_name_signature(item)
                current_ids_sig_by_path[normalized_path] = self._store_link_ids_signature_for_path(
                    item.full_path
                )
            if progress_cb is not None:
                progress_cb("Rebuild links: collect local signatures", idx, total_dirs)
        all_paths = list(dict.fromkeys(all_paths))
        if not all_paths:
            return 0

        existing_rows = self.db.list_store_link_rows_for_paths(all_paths, include_manual=False)
        previous_states = self.db.list_store_link_rebuild_state_for_paths(all_paths)

        desired_by_path: dict[str, dict[tuple[str, str, str], dict[str, object]]] = {}
        for row in evidence_rows:
            normalized_path = self._norm_store_inventory_path(row.inventory_path)
            store_name = normalize_store_name(row.store_name)
            account_id = str(row.account_id or "").strip()
            entitlement_id = str(row.entitlement_id or "").strip()
            if not normalized_path or not store_name or not account_id or not entitlement_id:
                continue
            payload = {
                "inventory_path": normalized_path,
                "store_name": store_name,
                "account_id": account_id,
                "entitlement_id": entitlement_id,
                "match_method": str(row.match_method or "").strip() or "unknown",
                "confidence": float(row.confidence),
                "verified": True,
                "notes": str(row.notes or "").strip(),
            }
            desired_by_path.setdefault(normalized_path, {})[self._store_link_row_key(payload)] = payload

        existing_by_path: dict[str, dict[tuple[str, str, str], dict[str, object]]] = {}
        for path in all_paths:
            existing_map: dict[tuple[str, str, str], dict[str, object]] = {}
            for row in existing_rows.get(path, []):
                key = self._store_link_row_key(row)
                if not key[0] or not key[1] or not key[2]:
                    continue
                existing_map[key] = row
            existing_by_path[path] = existing_map

        force_all_paths: set[str] = set()
        force_stores_by_path: dict[str, set[str]] = {}
        if force_rebuild_all:
            force_all_paths = set(all_paths)
        for path in all_paths:
            if path in force_all_paths:
                continue
            prev = previous_states.get(path, {})
            prev_name_sig = str(prev.get("name_sig") or "").strip()
            prev_ids_sig = self._store_link_ids_signature_from_json(
                str(prev.get("store_ids_sig_json") or "{}")
            )
            cur_name_sig = str(current_name_sig_by_path.get(path) or "").strip()
            cur_ids_sig = current_ids_sig_by_path.get(path, {})
            if prev and prev_name_sig != cur_name_sig:
                force_all_paths.add(path)
                continue
            changed_stores = {
                store_name
                for store_name in set(prev_ids_sig.keys()) | set(cur_ids_sig.keys())
                if str(prev_ids_sig.get(store_name, "")).strip()
                != str(cur_ids_sig.get(store_name, "")).strip()
            }
            if changed_stores:
                force_stores_by_path[path] = changed_stores

        changes = 0
        delta_ops: list[tuple[str, dict[str, object]]] = []
        for path in all_paths:
            existing_map = dict(existing_by_path.get(path, {}))
            desired_map = dict(desired_by_path.get(path, {}))

            if path in force_all_paths and existing_map:
                deleted = self.db.delete_store_links_for_inventory_path(path, include_manual=False)
                if deleted > 0:
                    changes += deleted
                existing_map.clear()
            else:
                forced_stores = force_stores_by_path.get(path, set())
                for store_name in sorted(forced_stores):
                    deleted = self.db.delete_store_links_for_inventory_store(
                        path,
                        store_name,
                        include_manual=False,
                    )
                    if deleted > 0:
                        changes += deleted
                    existing_map = {
                        key: row
                        for key, row in existing_map.items()
                        if key[0] != store_name
                    }

            for key in sorted(existing_map.keys()):
                if key in desired_map:
                    continue
                deleted = self.db.delete_store_link(
                    inventory_path=path,
                    store_name=key[0],
                    account_id=key[1],
                    entitlement_id=key[2],
                )
                if deleted > 0:
                    changes += deleted

            for key, payload in desired_map.items():
                existing = existing_map.get(key)
                should_upsert = existing is None
                if existing is not None:
                    should_upsert = (
                        self._store_link_row_payload(existing)
                        != self._store_link_row_payload(payload)
                    )
                if not should_upsert:
                    continue
                delta_ops.append(("upsert", payload))

        total_delta_ops = max(1, len(delta_ops))
        if progress_cb is not None:
            progress_cb("Rebuild links: apply changed links", 0, total_delta_ops)
        for idx, (_op, payload) in enumerate(delta_ops, start=1):
            self.db.upsert_store_link(
                inventory_path=str(payload.get("inventory_path") or ""),
                store_name=str(payload.get("store_name") or ""),
                account_id=str(payload.get("account_id") or ""),
                entitlement_id=str(payload.get("entitlement_id") or ""),
                match_method=str(payload.get("match_method") or "unknown"),
                confidence=float(payload.get("confidence") or 0.0),
                verified=bool(payload.get("verified")),
                notes=str(payload.get("notes") or ""),
            )
            changes += 1
            if progress_cb is not None:
                progress_cb("Rebuild links: apply changed links", idx, total_delta_ops)

        total_rows = max(1, len(evidence_rows))
        if progress_cb is not None:
            progress_cb("Rebuild links: persist discovered IDs", 0, total_rows)
        for idx, row in enumerate(evidence_rows, start=1):
            if row.match_method == "strong_id":
                if progress_cb is not None:
                    progress_cb("Rebuild links: persist discovered IDs", idx, total_rows)
                continue
            owned = owned_lookup.get(
                (
                    normalize_store_name(row.store_name),
                    str(row.account_id or "").strip(),
                    str(row.entitlement_id or "").strip(),
                )
            )
            if owned is not None:
                token = preferred_store_id_for_owned_game(row.store_name, owned)
                if token:
                    self.assign_store_id_hint(
                        folder_path=row.inventory_path,
                        store_name=row.store_name,
                        store_id=token,
                    )
            if progress_cb is not None:
                progress_cb("Rebuild links: persist discovered IDs", idx, total_rows)

        total_state = max(1, len(all_paths))
        if progress_cb is not None:
            progress_cb("Rebuild links: save rebuild state", 0, total_state)
        for idx, path in enumerate(all_paths, start=1):
            self.db.upsert_store_link_rebuild_state(
                inventory_path=path,
                name_sig=current_name_sig_by_path.get(path, ""),
                store_ids_sig_json=json.dumps(
                    current_ids_sig_by_path.get(path, {}),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            if progress_cb is not None:
                progress_cb("Rebuild links: save rebuild state", idx, total_state)
        return changes

    def set_manual_owned_stores(self, folder_path: str, stores: list[str]) -> int:
        path = self._norm_folder_path(folder_path)
        if not path:
            return 0
        ordered = sort_stores(stores)
        self.db.delete_store_links_for_inventory_path(path, include_manual=True)
        for store_name in ordered:
            canonical = normalize_store_name(store_name)
            if not canonical:
                continue
            self.db.upsert_store_link(
                inventory_path=path,
                store_name=canonical,
                account_id="__manual__",
                entitlement_id=f"manual:{canonical.casefold()}:{path.casefold()}",
                match_method="manual_confirmed",
                confidence=1.0,
                verified=True,
                notes="Manual ownership assignment.",
            )
        return len(ordered)

    def store_page_url_for_inventory(
        self,
        folder_path: str,
        *,
        store_name: str,
        game_title: str = "",
    ) -> str:
        canonical = normalize_store_name(store_name)
        if not canonical:
            return ""
        target = self.db.first_store_link_target(folder_path, store_name=canonical) or {}
        store_game_id = str(target.get("store_game_id", "") or target.get("entitlement_id", "")).strip()
        title = str(target.get("title", "")).strip() or str(game_title or "").strip()
        return store_game_url(
            canonical,
            store_game_id=store_game_id,
            title=title,
        )

    def store_targets_for_inventory(
        self,
        folder_path: str,
        *,
        game_title: str = "",
    ) -> list[dict[str, str]]:
        rows = self.db.list_store_link_targets_for_inventory(folder_path, verified_only=True)
        by_store: dict[str, dict[str, str]] = {}
        for row in rows:
            canonical = normalize_store_name(str(row.get("store_name") or ""))
            if not canonical or canonical in by_store:
                continue
            store_id = str(row.get("store_game_id") or row.get("entitlement_id") or "").strip()
            title = str(row.get("title") or "").strip() or str(game_title or "").strip()
            by_store[canonical] = {
                "store_name": canonical,
                "store_id": store_id,
                "title": title,
                "url": store_game_url(canonical, store_game_id=store_id, title=title),
            }
        ordered = sort_stores(list(by_store.keys()))
        return [by_store[store] for store in ordered if store in by_store]

    def find_tag_candidates(self, items: list[InventoryItem]) -> list[TagCandidate]:
        names = [(item.full_name, not item.is_dir) for item in items]
        non_tags = self.non_tags()
        candidates = collect_tag_candidates(names, non_tags=non_tags)
        self.db.replace_tag_candidates(
            [
                (
                    c.canonical_tag,
                    c.observed_tag,
                    c.count,
                    c.example_name,
                )
                for c in candidates
            ]
        )
        return candidates

    def save_tag_decisions(
        self, approvals: dict[str, str], display_map: dict[str, str]
    ) -> None:
        for canonical, status in approvals.items():
            if status not in {"approved", "non_tag"}:
                continue
            display = display_map.get(canonical, canonical)
            self.db.upsert_tag_rule(
                canonical_tag=canonicalize_tag(canonical),
                display_tag=display,
                status=status,
            )

    def build_cleanup_plan(self) -> list[RenamePlanItem]:
        return build_rename_plan(self.list_roots())

    def execute_cleanup_plan(self, plan_items: list[RenamePlanItem]) -> OperationReport:
        return execute_rename_plan(plan_items)

    def execute_cleanup_plan_with_progress(
        self,
        plan_items: list[RenamePlanItem],
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> OperationReport:
        return execute_rename_plan(
            plan_items,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
        )

    def build_archive_move_plan(
        self, extensions: set[str] | None = None
    ) -> list[MovePlanItem]:
        ext = extensions or {".iso", ".zip", ".rar", ".7z"}
        return build_move_plan(self.list_roots(), ext)

    def execute_archive_move_plan(
        self, plan_items: list[MovePlanItem]
    ) -> OperationReport:
        return execute_move_plan(plan_items)

    def execute_archive_move_plan_with_progress(
        self,
        plan_items: list[MovePlanItem],
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> OperationReport:
        return execute_move_plan(
            plan_items,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
        )

    def remember_directory_size(
        self,
        folder_path: str,
        size_bytes: int,
        mtime_ns: int | None = None,
    ) -> None:
        if not self._perf_dir_cache_enabled():
            return
        path = Path(folder_path)
        if mtime_ns is None:
            try:
                mtime_ns = path.stat().st_mtime_ns
            except OSError:
                return
        if mtime_ns <= 0:
            return
        self.dir_size_cache.put(path, mtime_ns, size_bytes)
        self.dir_size_cache.save()

    def icon_search_settings(self) -> IconSearchSettings:
        steam_key = get_secret("steamgriddb_api_key")
        legacy_steam_key = self.get_ui_pref("steamgriddb_api_key", "")
        if not steam_key and legacy_steam_key:
            steam_key = legacy_steam_key
            if set_secret("steamgriddb_api_key", legacy_steam_key):
                self.set_ui_pref("steamgriddb_api_key", "")

        igdb_secret = get_secret("igdb_client_secret")
        legacy_igdb_secret = self.get_ui_pref("igdb_client_secret", "")
        if not igdb_secret and legacy_igdb_secret:
            igdb_secret = legacy_igdb_secret
            if set_secret("igdb_client_secret", legacy_igdb_secret):
                self.set_ui_pref("igdb_client_secret", "")

        return IconSearchSettings(
            steamgriddb_enabled=self.get_ui_pref("steamgriddb_enabled", "1") == "1",
            steamgriddb_api_key=steam_key,
            steamgriddb_api_base=self.get_ui_pref(
                "steamgriddb_api_base", DEFAULT_STEAMGRIDDB_API_BASE
            ),
            igdb_enabled=self.get_ui_pref("igdb_enabled", "0") == "1",
            igdb_client_id=self.get_ui_pref("igdb_client_id", ""),
            igdb_client_secret=igdb_secret,
            igdb_api_base=self.get_ui_pref("igdb_api_base", DEFAULT_IGDB_API_BASE),
            timeout_seconds=15.0,
        )

    def save_icon_search_settings(self, settings: IconSearchSettings) -> None:
        self.set_ui_pref("steamgriddb_enabled", "1" if settings.steamgriddb_enabled else "0")
        self.set_ui_pref("steamgriddb_api_base", settings.steamgriddb_api_base)
        self.set_ui_pref("igdb_enabled", "1" if settings.igdb_enabled else "0")
        self.set_ui_pref("igdb_client_id", settings.igdb_client_id)
        self.set_ui_pref("igdb_api_base", settings.igdb_api_base)
        self.set_ui_pref("steamgriddb_api_key", "")
        self.set_ui_pref("igdb_client_secret", "")
        # Retire legacy provider prefs and plaintext key.
        self.set_ui_pref("iconfinder_enabled", "0")
        self.set_ui_pref("iconfinder_api_base", "")
        self.set_ui_pref("iconfinder_api_key", "")

        steam_ok = (
            delete_secret("steamgriddb_api_key")
            if not settings.steamgriddb_api_key
            else set_secret("steamgriddb_api_key", settings.steamgriddb_api_key)
        )
        igdb_ok = (
            delete_secret("igdb_client_secret")
            if not settings.igdb_client_secret
            else set_secret("igdb_client_secret", settings.igdb_client_secret)
        )
        delete_secret("iconfinder_api_key")
        if not steam_ok or not igdb_ok:
            raise RuntimeError(
                "Could not store API keys in secure secret storage (Credential Manager)."
            )

    def search_icon_candidates(
        self,
        game_name: str,
        cleaned_name: str,
        sgdb_resources: list[str] | None = None,
    ) -> list[IconCandidate]:
        resource_order, resource_enabled = self.sgdb_resource_preferences()
        requested = (
            [value for value in resource_order if value in resource_enabled]
            if sgdb_resources is None
            else sgdb_resources
        )
        return search_icon_candidates(
            game_name,
            cleaned_name,
            self.icon_search_settings(),
            sgdb_resources=requested,
        )

    def test_icon_search_settings(self, settings: IconSearchSettings) -> str:
        candidates = search_icon_candidates("Portal", "Portal", settings)
        by_provider: dict[str, int] = {}
        for candidate in candidates:
            by_provider[candidate.provider] = by_provider.get(candidate.provider, 0) + 1
        if not by_provider:
            return "No candidates returned. Check API keys or endpoints."
        chunks = [f"{name}: {count}" for name, count in sorted(by_provider.items())]
        return f"Success. Candidates found: {', '.join(chunks)}"

    def discover_store_ids_for_title(self, title: str) -> dict[str, str]:
        return lookup_igdb_store_ids_for_title(
            self.icon_search_settings(),
            str(title or "").strip(),
        )

    def download_candidate(self, candidate: IconCandidate) -> bytes:
        local_path = Path(str(candidate.image_url or "").strip())
        if local_path.exists() and local_path.is_file():
            return local_path.read_bytes()
        cache_key = candidate.image_url
        cached = self.download_cache.read(cache_key, extension=".img")
        if cached is not None:
            return cached
        payload = download_candidate_image(candidate.image_url)
        self.download_cache.write(cache_key, payload, extension=".img")
        return payload

    def candidate_preview(
        self,
        candidate: IconCandidate,
        icon_style: str = "none",
        size: int = 64,
        bg_removal_engine: str = "none",
        border_shader: dict[str, object] | None = None,
        background_fill_mode: str = "black",
        background_fill_params: dict[str, object] | None = None,
    ) -> bytes:
        style_key = icon_style.strip().casefold() or "none"
        bg_key = bg_removal_engine.strip().casefold() or "none"
        shader_key = json.dumps(border_shader or {}, sort_keys=True)
        fill_key = normalize_background_fill_mode(background_fill_mode)
        fill_params_key = json.dumps(background_fill_params or {}, sort_keys=True)
        cache_key = (
            f"{candidate.preview_url}|{style_key}|{bg_key}|{shader_key}|f:{fill_key}|fp:{fill_params_key}|s{size}"
        )
        cached = self.preview_cache.read(cache_key, extension=".png")
        if cached is not None:
            return cached
        preview_source = candidate.preview_url or candidate.image_url
        preview_path = Path(str(preview_source or "").strip())
        if preview_path.exists() and preview_path.is_file():
            payload = preview_path.read_bytes()
        else:
            payload = download_candidate_image(preview_source)
        normalized_payload = self._normalize_preview_payload(payload)
        preview = build_preview_png(
            normalized_payload,
            size=size,
            icon_style=style_key,
            bg_removal_engine=bg_key,
            border_shader=border_shader,
            background_fill_mode=fill_key,
            background_fill_params=background_fill_params,
        )
        self.preview_cache.write(cache_key, preview, extension=".png")
        return preview

    def apply_folder_icon(
        self,
        folder_path: str,
        source_image: bytes,
        icon_name_hint: str,
        info_tip: str | None = None,
        icon_style: str = "none",
        bg_removal_engine: str = "none",
        bg_removal_params: dict[str, object] | None = None,
        text_preserve_config: dict[str, object] | None = None,
        border_shader: dict[str, object] | None = None,
        background_fill_mode: str = "black",
        background_fill_params: dict[str, object] | None = None,
        size_improvements: dict[int, dict[str, object]] | None = None,
    ) -> IconApplyResult:
        # Isolate icon build/apply in a subprocess so decoder/plugin crashes cannot
        # take down the main UI process.
        return apply_folder_icon_in_subprocess(
            folder_path=Path(folder_path),
            source_image=source_image,
            icon_name_hint=icon_name_hint,
            info_tip=info_tip,
            icon_style=icon_style,
            bg_removal_engine=bg_removal_engine,
            bg_removal_params=bg_removal_params,
            text_preserve_config=text_preserve_config,
            border_shader=border_shader,
            background_fill_mode=background_fill_mode,
            background_fill_params=background_fill_params,
            size_improvements=size_improvements,
            temp_dir=self.db.db_path.parent / "tmp",
        )

    def get_or_fetch_game_infotip(self, cleaned_name: str) -> str | None:
        name = cleaned_name.strip()
        if not name:
            return None
        cached = self.db.get_game_infotip(name)
        if cached is not None:
            tip, _source = cached
            tip = tip.strip()
            return tip or None
        fetched = fetch_game_infotip(name)
        if fetched is None:
            self.db.upsert_game_infotip(name, "", "miss")
            return None
        tip, source = fetched
        tip = tip.strip()
        if not tip:
            self.db.upsert_game_infotip(name, "", "miss")
            return None
        self.db.upsert_game_infotip(name, tip, source)
        return tip

    def refresh_game_infotip(self, cleaned_name: str) -> str | None:
        name = cleaned_name.strip()
        if not name:
            return None
        fetched = fetch_game_infotip(name)
        if fetched is None:
            cached = self.db.get_game_infotip(name)
            if cached is not None:
                cached_tip, _cached_source = cached
                cached_tip = cached_tip.strip()
                return cached_tip or None
            return None
        tip, source = fetched
        tip = tip.strip()
        if not tip:
            return None
        self.db.upsert_game_infotip(name, tip, source)
        return tip

    def ensure_folder_info_tip(
        self,
        folder_path: str,
        cleaned_name: str,
        *,
        overwrite_existing: bool = False,
        force_refresh: bool = False,
    ) -> tuple[bool, str | None]:
        path = Path(folder_path)
        existing = read_folder_info_tip(path).strip()
        if existing and not overwrite_existing:
            return False, existing
        tip = (
            self.refresh_game_infotip(cleaned_name)
            if force_refresh
            else self.get_or_fetch_game_infotip(cleaned_name)
        )
        if not tip:
            return False, existing or None
        updated = set_folder_info_tip(path, tip)
        if updated:
            return True, tip
        if existing:
            return False, existing
        return False, None

    def set_manual_folder_info_tip(
        self,
        folder_path: str,
        cleaned_name: str,
        info_tip: str,
    ) -> bool:
        path = Path(folder_path)
        tip = info_tip.strip()
        if not tip:
            return False
        existing = read_folder_info_tip(path).strip()
        if existing == tip:
            self.db.upsert_game_infotip(cleaned_name, tip, "manual")
            return True
        updated = set_folder_info_tip(path, tip)
        if not updated:
            return False
        self.db.upsert_game_infotip(cleaned_name, tip, "manual")
        return True

    def repair_absolute_icon_paths(self) -> OperationReport:
        return repair_absolute_icon_paths(self.list_roots())

    def _local_icon_targets(
        self,
        items: list[InventoryItem],
    ) -> list[tuple[Path, Path]]:
        targets: list[tuple[Path, Path]] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            if not item.is_dir or item.icon_status != "valid" or not item.folder_icon_path:
                continue
            folder_path = Path(item.full_path)
            icon_path = Path(item.folder_icon_path)
            if not is_local_folder_icon(folder_path, icon_path):
                continue
            key = (str(folder_path.resolve()), str(icon_path.resolve()))
            if key in seen:
                continue
            seen.add(key)
            targets.append((folder_path, icon_path))
        return targets

    def collect_icon_rebuild_entries(
        self,
        items: list[InventoryItem],
    ) -> tuple[OperationReport, list[IconRebuildEntry]]:
        return collect_existing_local_icons(self._local_icon_targets(items))

    def backfill_missing_icon_sources(
        self,
        items: list[InventoryItem],
        *,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        sgdb_threshold: float = 0.95,
    ) -> OperationReport:
        candidates: list[InventoryItem] = []
        seen: set[str] = set()
        for item in list(items):
            if not item.is_dir or item.icon_status != "valid" or not item.folder_icon_path:
                continue
            folder_key = self._norm_folder_path(item.full_path)
            if not folder_key or folder_key in seen:
                continue
            seen.add(folder_key)
            candidates.append(item)

        report = OperationReport(total=len(candidates))
        if progress_cb is not None:
            progress_cb("Backfill icon sources", 0, max(1, len(candidates)))

        settings = self.icon_search_settings()
        sgdb_enabled = bool(
            settings.steamgriddb_enabled and str(settings.steamgriddb_api_key or "").strip()
        )
        sgdb_payload = {
            "enabled": bool(sgdb_enabled),
            "api_base": str(settings.steamgriddb_api_base or "").strip(),
            "api_key": str(settings.steamgriddb_api_key or "").strip(),
        }

        for idx, item in enumerate(candidates, start=1):
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("Icon source backfill canceled.")
            if progress_cb is not None:
                progress_cb("Backfill icon sources", idx - 1, max(1, len(candidates)))

            folder_path = self._norm_folder_path(item.full_path)
            icon_path = os.path.normpath(str(item.folder_icon_path or "").strip())
            folder_name = str(item.cleaned_name or item.full_name or Path(folder_path).name).strip()
            if not folder_path or not icon_path:
                report.failed += 1
                report.details.append(f"{folder_name}: missing folder/icon path")
                continue

            existing = self.read_folder_icon_metadata(folder_path)
            existing_kind = str(existing.get("SourceKind", "")).strip().casefold()
            existing_provider = str(existing.get("SourceProvider", "")).strip().casefold()
            source_assigned_fp = str(existing.get("SourceFingerprint256", "")).strip().casefold()
            source_game_id = str(existing.get("SourceGameId", "")).strip()
            backfill_fp = str(existing.get("SourceBackfillFingerprint256", "")).strip().casefold()
            has_source = bool(existing_kind or existing_provider)
            is_sgdb_source = (
                existing_kind in {"sgdb_raw", "sgdb_modified"}
                or existing_provider == "steamgriddb"
            )
            if is_sgdb_source:
                report.skipped += 1
                report.details.append(f"{folder_name}: skipped (source is SteamGridDB)")
                continue

            icon_file = Path(icon_path)
            if not icon_file.exists() or icon_file.suffix.casefold() != ".ico":
                report.failed += 1
                report.details.append(f"{folder_name}: skipped (invalid local .ico icon)")
                continue
            try:
                current_fp = self.icon_fingerprint256(icon_path).strip().casefold()
            except Exception as exc:
                report.failed += 1
                report.details.append(f"{folder_name}: fingerprint failed ({exc})")
                continue

            if backfill_fp and backfill_fp == current_fp:
                report.skipped += 1
                report.details.append(f"{folder_name}: skipped (unchanged since last backfill)")
                continue
            if has_source and source_assigned_fp and source_assigned_fp == current_fp:
                report.skipped += 1
                report.details.append(
                    f"{folder_name}: skipped (unchanged since source assignment)"
                )
                continue

            source_kind = "web"
            source_provider = "Internet"
            source_confidence = 0.0
            source_note = "fallback"
            source_fingerprint = ""
            probe = probe_icon_source_in_subprocess(
                folder_path=folder_path,
                icon_path=icon_path,
                cleaned_name=item.cleaned_name or item.full_name,
                full_name=item.full_name,
                source_game_id=source_game_id,
                sgdb=sgdb_payload,
                threshold=float(sgdb_threshold),
            )
            probe_status = str(probe.get("status", "")).strip().casefold()
            if probe_status != "ok":
                report.failed += 1
                report.details.append(
                    f"{folder_name}: source probe failed ({probe.get('error', 'unknown error')})"
                )
                continue
            source_kind = str(probe.get("source_kind", source_kind)).strip() or source_kind
            source_provider = str(probe.get("source_provider", source_provider)).strip() or source_provider
            try:
                source_confidence = float(probe.get("source_confidence", source_confidence))
            except Exception:
                source_confidence = 0.0
            source_note = str(probe.get("source_note", source_note)).strip() or source_note
            source_fingerprint = str(probe.get("source_fingerprint256", "")).strip().casefold()
            effective_fp = source_fingerprint or current_fp

            changed = self.record_assigned_icon_source(
                folder_path=folder_path,
                source_kind=source_kind,
                source_provider=source_provider,
                source_candidate_id=(
                    "backfill:recheck-source"
                    if has_source
                    else "backfill:missing-source"
                ),
                source_fingerprint256=effective_fp,
                source_confidence=source_confidence,
            )
            self.upsert_folder_icon_metadata(
                folder_path,
                {
                    "SourceBackfillFingerprint256": effective_fp,
                    "SourceBackfillAtUtc": self._utc_now_iso(),
                },
            )
            if changed:
                report.succeeded += 1
                report.details.append(
                    f"{folder_name}: source set to {source_kind}/{source_provider} ({source_note})"
                )
            else:
                report.succeeded += 1
                report.details.append(
                    f"{folder_name}: source confirmed ({source_kind}/{source_provider})"
                )

            if progress_cb is not None:
                progress_cb("Backfill icon sources", idx, max(1, len(candidates)))

        if progress_cb is not None:
            progress_cb("Backfill icon sources", max(1, len(candidates)), max(1, len(candidates)))
        return report

    def rebuild_existing_icons(
        self,
        entries: list[IconRebuildEntry],
        size_improvements: dict[int, dict[str, object]] | None = None,
        *,
        force_rebuild: bool = False,
        create_backups: bool = True,
        progress_cb: Callable[[str, int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> OperationReport:
        return rebuild_existing_local_icons(
            entries,
            size_improvements=size_improvements,
            force_rebuild=force_rebuild,
            create_backups=create_backups,
            progress_cb=progress_cb,
            should_cancel=should_cancel,
            on_rebuilt=self._on_rebuilt_icon,
        )

    def _on_rebuilt_icon(self, folder_path: str, icon_path: str) -> None:
        normalized_folder = self._norm_folder_path(folder_path)
        normalized_icon = os.path.normpath(str(icon_path or "").strip())
        if not normalized_folder or not normalized_icon:
            return
        existing = self.read_folder_icon_metadata(normalized_folder)
        existing_kind = str(existing.get("SourceKind", "")).strip()
        existing_provider = str(existing.get("SourceProvider", "")).strip()
        if not (existing_kind or existing_provider):
            return
        try:
            rebuilt_fp = self.icon_fingerprint256(normalized_icon).strip().casefold()
        except Exception:
            return
        updates: dict[str, str] = {
            "SourceFingerprint256": rebuilt_fp,
        }
        if str(existing.get("SourceBackfillFingerprint256", "")).strip():
            updates["SourceBackfillFingerprint256"] = rebuilt_fp
        self.upsert_folder_icon_metadata(normalized_folder, updates)

    def icon_rebuild_preview_frames(
        self,
        entry: IconRebuildEntry,
        sizes: tuple[int, ...] = (16, 24, 32, 48),
        size_improvements: dict[int, dict[str, object]] | None = None,
    ) -> dict[int, tuple[bytes, bytes]]:
        return build_rebuild_preview_frames(
            entry,
            sizes=sizes,
            size_improvements=size_improvements,
        )

    def clean_backup_icons(self) -> OperationReport:
        roots = self.list_roots()
        root_paths = [Path(root.path) for root in roots]
        return clean_backup_icon_files(root_paths)
