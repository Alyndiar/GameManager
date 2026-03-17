from __future__ import annotations

import os
from pathlib import Path

from gamemanager.db import Database
from gamemanager.models import (
    InventoryItem,
    MovePlanItem,
    OperationReport,
    RenamePlanItem,
    RootDisplayInfo,
    RootFolder,
    TagCandidate,
)
from gamemanager.services.normalization import canonicalize_tag
from gamemanager.services.operations import (
    build_move_plan,
    build_rename_plan,
    execute_move_plan,
    execute_rename_plan,
)
from gamemanager.services.scanner import list_root_display_infos, scan_roots
from gamemanager.services.tagging import collect_tag_candidates


class AppState:
    def __init__(self, db_path: Path):
        self.db = Database(db_path)

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
