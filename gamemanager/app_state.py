from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import os
from pathlib import Path
from typing import Callable

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
from gamemanager.services.icon_pipeline import build_preview_png
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
    read_folder_icon_metadata,
    read_folder_info_tip,
    set_folder_icon_metadata,
    set_folder_info_tip,
)
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.icon_sources import (
    DEFAULT_ICONFINDER_API_BASE,
    DEFAULT_SGDB_ENABLED_RESOURCES,
    DEFAULT_SGDB_RESOURCE_ORDER,
    DEFAULT_STEAMGRIDDB_API_BASE,
    IconSearchSettings,
    download_candidate_image,
    normalize_sgdb_resources,
    search_icon_candidates,
)
from gamemanager.services.steamgriddb_targeting import (
    name_similarity,
    resolve_target_candidates,
)
from gamemanager.services.steamgriddb_upload import (
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
from gamemanager.services.tagging import collect_tag_candidates


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
        return read_folder_icon_metadata(Path(normalized))

    def upsert_folder_icon_metadata(self, folder_path: str, updates: dict[str, str]) -> bool:
        normalized = self._norm_folder_path(folder_path)
        if not normalized:
            return False
        merged = read_folder_icon_metadata(Path(normalized))
        for key, value in dict(updates or {}).items():
            if value is None:
                merged.pop(str(key), None)
                continue
            token = str(value).strip()
            if token:
                merged[str(key)] = token
            else:
                merged.pop(str(key), None)
        return set_folder_icon_metadata(Path(normalized), merged)

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
    ) -> SgdbTargetResolution:
        settings = self.icon_search_settings()
        candidates, _variants, exact_appid_game_id = resolve_target_candidates(
            settings,
            folder_path=self._norm_folder_path(folder_path),
            cleaned_name=cleaned_name,
            full_name=full_name,
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
        return SgdbGameCandidate(
            game_id=int(details.game_id),
            title=details.title,
            confidence=1.0,
            evidence=[f"Manual SGDB ID {int(game_id)}"],
            steam_appid=details.steam_appid,
        )

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
        if cache_obj is not None:
            cache_obj.save()
        return root_infos, items

    def refresh_roots_only(self) -> list[RootDisplayInfo]:
        roots = self.list_roots()
        return list_root_display_infos(roots)

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

        iconfinder_key = get_secret("iconfinder_api_key")
        legacy_iconfinder_key = self.get_ui_pref("iconfinder_api_key", "")
        if not iconfinder_key and legacy_iconfinder_key:
            iconfinder_key = legacy_iconfinder_key
            if set_secret("iconfinder_api_key", legacy_iconfinder_key):
                self.set_ui_pref("iconfinder_api_key", "")

        return IconSearchSettings(
            steamgriddb_enabled=self.get_ui_pref("steamgriddb_enabled", "1") == "1",
            steamgriddb_api_key=steam_key,
            steamgriddb_api_base=self.get_ui_pref(
                "steamgriddb_api_base", DEFAULT_STEAMGRIDDB_API_BASE
            ),
            iconfinder_enabled=self.get_ui_pref("iconfinder_enabled", "1") == "1",
            iconfinder_api_key=iconfinder_key,
            iconfinder_api_base=self.get_ui_pref(
                "iconfinder_api_base", DEFAULT_ICONFINDER_API_BASE
            ),
            timeout_seconds=15.0,
        )

    def save_icon_search_settings(self, settings: IconSearchSettings) -> None:
        self.set_ui_pref("steamgriddb_enabled", "1" if settings.steamgriddb_enabled else "0")
        self.set_ui_pref("steamgriddb_api_base", settings.steamgriddb_api_base)
        self.set_ui_pref("iconfinder_enabled", "1" if settings.iconfinder_enabled else "0")
        self.set_ui_pref("iconfinder_api_base", settings.iconfinder_api_base)
        self.set_ui_pref("steamgriddb_api_key", "")
        self.set_ui_pref("iconfinder_api_key", "")

        steam_ok = (
            delete_secret("steamgriddb_api_key")
            if not settings.steamgriddb_api_key
            else set_secret("steamgriddb_api_key", settings.steamgriddb_api_key)
        )
        iconfinder_ok = (
            delete_secret("iconfinder_api_key")
            if not settings.iconfinder_api_key
            else set_secret("iconfinder_api_key", settings.iconfinder_api_key)
        )
        if not steam_ok or not iconfinder_ok:
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
    ) -> bytes:
        style_key = icon_style.strip().casefold() or "none"
        bg_key = bg_removal_engine.strip().casefold() or "none"
        shader_key = json.dumps(border_shader or {}, sort_keys=True)
        cache_key = f"{candidate.preview_url}|{style_key}|{bg_key}|{shader_key}|s{size}"
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
