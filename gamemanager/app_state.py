from __future__ import annotations

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
from gamemanager.services.game_infotips import fetch_game_infotip
from gamemanager.services.folder_icons import read_folder_info_tip, set_folder_info_tip
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
            from PIL import Image, ImageOps

            with Image.open(BytesIO(payload)) as image:
                image.load()
                image = ImageOps.exif_transpose(image)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA")
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
        )

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
