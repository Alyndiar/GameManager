from __future__ import annotations

import os
from pathlib import Path

from gamemanager.db import Database
from gamemanager.models import (
    IconApplyResult,
    IconCandidate,
    InventoryItem,
    MovePlanItem,
    OperationReport,
    RenamePlanItem,
    RootDisplayInfo,
    RootFolder,
    TagCandidate,
)
from gamemanager.services.folder_icons import apply_folder_icon
from gamemanager.services.icon_cache import DiskImageCache
from gamemanager.services.icon_pipeline import build_multi_size_ico, build_preview_png
from gamemanager.services.icon_sources import (
    DEFAULT_ICONFINDER_API_BASE,
    DEFAULT_STEAMGRIDDB_API_BASE,
    IconSearchSettings,
    download_candidate_image,
    search_icon_candidates,
)
from gamemanager.services.normalization import canonicalize_tag
from gamemanager.services.operations import (
    build_move_plan,
    build_rename_plan,
    execute_move_plan,
    execute_rename_plan,
)
from gamemanager.services.scanner import list_root_display_infos, scan_roots
from gamemanager.services.secret_store import delete_secret, get_secret, set_secret
from gamemanager.services.tagging import collect_tag_candidates


class AppState:
    def __init__(self, db_path: Path):
        self.db = Database(db_path)
        cache_root = self.db.db_path.parent / "cache"
        self.preview_cache = DiskImageCache(cache_root / "candidate_previews")
        self.download_cache = DiskImageCache(cache_root / "candidate_downloads")

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

    def approved_tags(self) -> set[str]:
        return {row.canonical_tag for row in self.db.list_tag_rules("approved")}

    def non_tags(self) -> set[str]:
        return {row.canonical_tag for row in self.db.list_tag_rules("non_tag")}

    def refresh(self) -> tuple[list[RootDisplayInfo], list[InventoryItem]]:
        roots = self.list_roots()
        root_infos = list_root_display_infos(roots)
        items = scan_roots(roots, self.approved_tags())
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

    def build_archive_move_plan(
        self, extensions: set[str] | None = None
    ) -> list[MovePlanItem]:
        ext = extensions or {".iso", ".zip", ".rar", ".7z"}
        return build_move_plan(self.list_roots(), ext)

    def execute_archive_move_plan(
        self, plan_items: list[MovePlanItem]
    ) -> OperationReport:
        return execute_move_plan(plan_items)

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
        self, game_name: str, cleaned_name: str
    ) -> list[IconCandidate]:
        return search_icon_candidates(game_name, cleaned_name, self.icon_search_settings())

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
        cache_key = candidate.image_url
        cached = self.download_cache.read(cache_key, extension=".img")
        if cached is not None:
            return cached
        payload = download_candidate_image(candidate.image_url)
        self.download_cache.write(cache_key, payload, extension=".img")
        return payload

    def candidate_preview(
        self, candidate: IconCandidate, circular_ring: bool = True, size: int = 64
    ) -> bytes:
        style_key = "cr1" if circular_ring else "cr0"
        cache_key = f"{candidate.preview_url}|{style_key}|s{size}"
        cached = self.preview_cache.read(cache_key, extension=".png")
        if cached is not None:
            return cached
        preview_source = candidate.preview_url or candidate.image_url
        payload = download_candidate_image(preview_source)
        preview = build_preview_png(payload, size=size, circular_ring=circular_ring)
        self.preview_cache.write(cache_key, preview, extension=".png")
        return preview

    def apply_folder_icon(
        self,
        folder_path: str,
        source_image: bytes,
        icon_name_hint: str,
        info_tip: str | None = None,
        circular_ring: bool = True,
    ) -> IconApplyResult:
        ico_payload = build_multi_size_ico(source_image, circular_ring=circular_ring)
        return apply_folder_icon(
            folder_path=Path(folder_path),
            icon_bytes=ico_payload,
            icon_name_hint=icon_name_hint,
            info_tip=info_tip,
        )
