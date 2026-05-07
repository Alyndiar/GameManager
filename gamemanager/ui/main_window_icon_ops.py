from __future__ import annotations

from collections.abc import Callable
import copy
from datetime import datetime
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from gamemanager.models import (
    IconCandidate,
    IconRebuildEntry,
    InventoryItem,
    OperationReport,
    SgdbGameCandidate,
)
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.background_removal import normalize_background_removal_engine
from gamemanager.services.storefronts.priority import normalize_store_name, sort_stores
from gamemanager.services.steamgriddb_targeting import normalize_name_for_compare
from gamemanager.services.icon_pipeline import (
    border_shader_to_dict,
    normalize_background_fill_params,
    default_icon_size_improvements,
    normalize_background_fill_mode,
    normalize_border_shader_config,
    normalize_icon_size_improvements,
    normalize_icon_style,
)
from gamemanager.ui.dialogs import (
    IconConverterDialog,
    IconPickerDialog,
    IconRebuildPreviewItem,
    SgdbTargetPickerDialog,
    TemplatePrepDialog,
    TemplateTransparencyDialog,
)


REBUILD_PREVIEW_MODES = ("all", "sample", "off")
REBUILD_PREVIEW_MODE_LABELS = {
    "all": "All",
    "sample": "Sample",
    "off": "Off",
}
REBUILD_PREVIEW_MODE_BY_LABEL = {
    label: mode for mode, label in REBUILD_PREVIEW_MODE_LABELS.items()
}
REBUILD_PREVIEW_SAMPLE_COUNT = 8


class MainWindowIconOpsMixin:
    @staticmethod
    def _unique_full_confidence_candidate(
        candidates: list[SgdbGameCandidate],
    ) -> SgdbGameCandidate | None:
        perfect = [
            c
            for c in list(candidates or [])
            if abs(float(c.confidence) - 1.0) <= 1e-9
        ]
        if len(perfect) == 1:
            return perfect[0]
        return None

    @staticmethod
    def _current_folder_icon_candidate(entry: InventoryItem) -> IconCandidate | None:
        icon_path_text = str(entry.folder_icon_path or "").strip()
        if not icon_path_text:
            return None
        icon_path = Path(icon_path_text)
        if not icon_path.exists() or not icon_path.is_file():
            return None
        return IconCandidate(
            provider="Current Folder Icon",
            candidate_id=f"local-existing:{entry.full_path}",
            title=f"Current icon ({icon_path.name})",
            preview_url=str(icon_path),
            image_url=str(icon_path),
            width=0,
            height=0,
            has_alpha=True,
            source_url=str(icon_path),
        )

    def _icon_rebuild_target_entries(self) -> tuple[list[InventoryItem], bool]:
        selected = [
            item
            for item in self._selected_right_entries()
            if item.is_dir and item.icon_status == "valid" and item.folder_icon_path
        ]
        if selected:
            return selected, True
        return (
            [
            item
            for item in self.inventory
            if item.is_dir and item.icon_status == "valid" and item.folder_icon_path
            ],
            False,
        )

    def _icon_source_backfill_target_entries(self) -> tuple[list[InventoryItem], bool]:
        selected = [
            item
            for item in self._selected_right_entries()
            if item.is_dir and item.icon_status == "valid" and item.folder_icon_path
        ]
        if selected:
            return selected, True
        return (
            [
                item
                for item in self.inventory
                if item.is_dir and item.icon_status == "valid" and item.folder_icon_path
            ],
            False,
        )

    def _sgdb_upload_target_entries(self) -> tuple[list[InventoryItem], bool]:
        selected = [
            item
            for item in self._selected_right_entries()
            if item.is_dir and item.icon_status == "valid" and item.folder_icon_path
        ]
        if selected:
            return selected, True
        return (
            [
                item
                for item in self.inventory
                if item.is_dir and item.icon_status == "valid" and item.folder_icon_path
            ],
            False,
        )

    @staticmethod
    def _normalize_rebuild_preview_mode(value: str) -> str:
        normalized = str(value or "").strip().casefold()
        if normalized in REBUILD_PREVIEW_MODES:
            return normalized
        return "sample"

    def _load_rebuild_preview_mode_pref(self) -> str:
        value = self.state.get_ui_pref("icon_rebuild_preview_mode", "sample")
        return self._normalize_rebuild_preview_mode(value)

    def _save_rebuild_preview_mode_pref(self, value: str) -> None:
        self.state.set_ui_pref(
            "icon_rebuild_preview_mode",
            self._normalize_rebuild_preview_mode(value),
        )

    def _load_rebuild_create_backups_pref(self) -> bool:
        value = self.state.get_ui_pref("icon_rebuild_create_backups", "1").strip().casefold()
        return value not in {"0", "false", "no", "off"}

    def _save_rebuild_create_backups_pref(self, enabled: bool) -> None:
        self.state.set_ui_pref("icon_rebuild_create_backups", "1" if enabled else "0")

    def _load_icon_rebuild_mode_pref(self) -> str:
        mode = self.state.get_ui_pref("icon_rebuild_mode", "guided").strip().casefold()
        return mode if mode in {"guided", "automatic"} else "guided"

    def _save_icon_rebuild_mode_pref(self, mode: str) -> None:
        normalized = str(mode or "").strip().casefold()
        self.state.set_ui_pref(
            "icon_rebuild_mode",
            normalized if normalized in {"guided", "automatic"} else "guided",
        )

    def _load_rebuild_size_improvement_defaults(self) -> dict[int, dict[str, object]]:
        fallback = default_icon_size_improvements()
        raw = self.state.get_ui_pref("icon_rebuild_size_improvements_defaults", "").strip()
        if not raw:
            return normalize_icon_size_improvements(fallback)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return normalize_icon_size_improvements(fallback)
        if not isinstance(parsed, dict):
            return normalize_icon_size_improvements(fallback)
        return normalize_icon_size_improvements(parsed)

    def _save_rebuild_size_improvement_defaults(
        self,
        defaults: dict[int, dict[str, object]],
    ) -> None:
        normalized = normalize_icon_size_improvements(defaults)
        self.state.set_ui_pref(
            "icon_rebuild_size_improvements_defaults",
            json.dumps(normalized, sort_keys=True),
        )

    def _prompt_rebuild_preview_mode(self, candidate_count: int) -> str | None:
        current_mode = self._load_rebuild_preview_mode_pref()
        labels = [REBUILD_PREVIEW_MODE_LABELS[mode] for mode in REBUILD_PREVIEW_MODES]
        current_label = REBUILD_PREVIEW_MODE_LABELS[current_mode]
        current_index = labels.index(current_label) if current_label in labels else 1
        selected_label, accepted = QInputDialog.getItem(
            self,
            "Rebuild Existing Icons",
            (
                "Preview mode before rebuild (single-icon rebuild previews remain on for "
                "All/Sample):"
            ),
            labels,
            current_index,
            False,
        )
        if not accepted:
            return None
        mode = REBUILD_PREVIEW_MODE_BY_LABEL.get(str(selected_label), "sample")
        self._save_rebuild_preview_mode_pref(mode)
        if candidate_count <= 1 and mode in {"all", "sample"}:
            return "all"
        return mode

    @staticmethod
    def _preview_entries_for_mode(
        entries: list[IconRebuildEntry],
        mode: str,
    ) -> list[IconRebuildEntry]:
        normalized = str(mode).strip().casefold()
        if normalized == "off":
            return []
        ordered = sorted(
            entries,
            key=lambda entry: (
                0 if not bool(getattr(entry, "already_rebuilt", False)) else 1,
                str(getattr(entry, "folder_path", "")).casefold(),
            ),
        )
        if normalized == "all":
            return ordered
        if len(ordered) <= 1:
            return ordered
        return ordered[: min(REBUILD_PREVIEW_SAMPLE_COUNT, len(ordered))]

    def _show_rebuild_preview_dialog(
        self,
        entries: list[IconRebuildEntry],
        size_improvements: dict[int, dict[str, object]],
        default_size_improvements: dict[int, dict[str, object]],
        create_backups: bool,
    ) -> tuple[dict[int, dict[str, object]], dict[int, dict[str, object]], bool] | None:
        preview_items: list[IconRebuildPreviewItem] = []
        for entry in entries:
            folder_name = Path(str(entry.folder_path)).name or str(entry.folder_path)
            preview_items.append(
                IconRebuildPreviewItem(
                    label=folder_name,
                    folder_path=str(entry.folder_path),
                    icon_path=str(entry.icon_path),
                    already_rebuilt=bool(entry.already_rebuilt),
                    summary=str(entry.summary),
                    entry=entry,
                )
            )
        if not preview_items:
            return (
                normalize_icon_size_improvements(size_improvements),
                normalize_icon_size_improvements(default_size_improvements),
                bool(create_backups),
            )

        dialog = self._icon_rebuild_preview_dialog_cls(
            preview_items,
            frame_loader=lambda entry, profile: self.state.icon_rebuild_preview_frames(
                entry,
                size_improvements=profile,
            ),
            size_improvements=size_improvements,
            default_size_improvements=default_size_improvements,
            create_backups=create_backups,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return (
            dialog.size_improvements(),
            dialog.default_size_improvements(),
            dialog.create_backups_enabled(),
        )

    def _on_rebuild_existing_icons(self) -> None:
        targets, used_selected_scope = self._icon_rebuild_target_entries()
        if not targets:
            QMessageBox.information(
                self,
                "Rebuild Existing Icons",
                "No local folder icons found. Select folders with existing local icons or refresh inventory.",
            )
            return

        collect_report, entries = self.state.collect_icon_rebuild_entries(targets)
        if not entries:
            QMessageBox.information(
                self,
                "Rebuild Existing Icons",
                "No local folder icons were eligible for rebuild.",
            )
            return
        single_icon_mode = len(targets) == 1 and len(entries) == 1
        rebuild_mode = self._load_icon_rebuild_mode_pref()
        automatic_mode = rebuild_mode == "automatic"
        if single_icon_mode:
            rebuild_candidates = [entries[0]]
        else:
            rebuild_candidates = [
                entry
                for entry in entries
                if not bool(getattr(entry, "already_rebuilt", False))
            ]
        already_rebuilt_count = sum(1 for entry in entries if entry.already_rebuilt)
        if not rebuild_candidates:
            QMessageBox.information(
                self,
                "Rebuild Existing Icons",
                "All eligible local icons are already marked as rebuilt (desktop.ini Rebuilt=true).",
            )
            return

        preview_mode = "off" if automatic_mode else "all"
        if (not single_icon_mode) and (not automatic_mode):
            selected_mode = self._prompt_rebuild_preview_mode(len(rebuild_candidates))
            if selected_mode is None:
                return
            preview_mode = selected_mode
        preview_entries = self._preview_entries_for_mode(
            rebuild_candidates,
            preview_mode,
        )
        previewed_count = len(preview_entries)
        default_size_improvements = self._load_rebuild_size_improvement_defaults()
        size_improvements = copy.deepcopy(default_size_improvements)
        create_backups = self._load_rebuild_create_backups_pref()
        if preview_entries and (not automatic_mode):
            preview_result = self._show_rebuild_preview_dialog(
                preview_entries,
                size_improvements=size_improvements,
                default_size_improvements=default_size_improvements,
                create_backups=create_backups,
            )
            if preview_result is None:
                return
            size_improvements, updated_defaults, create_backups = preview_result
            self._save_rebuild_size_improvement_defaults(updated_defaults)
            self._save_rebuild_create_backups_pref(create_backups)
        elif automatic_mode:
            self._save_icon_rebuild_mode_pref("automatic")

        preview_scope_text = "Off"
        if preview_mode == "all":
            preview_scope_text = f"All ({previewed_count}/{len(rebuild_candidates)})"
        elif preview_mode == "sample":
            preview_scope_text = f"Sample ({previewed_count}/{len(rebuild_candidates)})"
        prompt_lines = [
            f"Scope: {'selected' if used_selected_scope else 'all local icons'}",
            "",
            f"Eligible local icons: {collect_report.total}",
            f"Already rebuilt: {already_rebuilt_count}",
            "",
            f"Rebuild candidates now: {len(rebuild_candidates)}",
            f"Rebuild previews: {preview_scope_text}",
            f"Rebuild mode: {'Automatic (saved defaults)' if automatic_mode else 'Guided'}",
            (
                "Single-icon mode can force a rebuild even when Rebuilt=true."
                if single_icon_mode
                else ""
            ),
            (
                "Proceed with rebuild now? Backup files (*.gm_backup_YYYYMMDDHHMMSS.ico) will be created first."
                if create_backups
                else "Proceed with rebuild now? Backups are disabled."
            ),
        ]
        prompt_lines = [line for line in prompt_lines if line]
        decision = QMessageBox.question(
            self,
            "Rebuild Existing Icons",
            "\n".join(prompt_lines),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return

        def _run(progress_cb, should_cancel):
            return self.state.rebuild_existing_icons(
                rebuild_candidates,
                size_improvements=size_improvements,
                force_rebuild=single_icon_mode,
                create_backups=create_backups,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def _done(report):
            if report.succeeded > 0:
                self._folder_icon_cache.clear()
                self._folder_icon_preview_cache.clear()
                try:
                    self._populate_right(self.inventory)
                except Exception:
                    self._set_refresh_needed(True)
            if not single_icon_mode:
                lines = [
                    f"Rebuilt: {report.succeeded}",
                    f"Failed: {report.failed}",
                    f"Skipped: {report.skipped}",
                ]
                if report.details:
                    lines.append("")
                    lines.extend(report.details[:12])
                if int(report.failed) > 0:
                    QMessageBox.warning(self, "Rebuild Existing Icons", "\n".join(lines))
                else:
                    self._show_success_popup("Rebuild Existing Icons", "\n".join(lines))

        self._start_report_operation(
            "Rebuild Existing Icons",
            _run,
            _done,
        )

    def _on_backfill_missing_icon_sources(self) -> None:
        targets, used_selected_scope = self._icon_source_backfill_target_entries()
        if not targets:
            QMessageBox.information(
                self,
                "Backfill Missing Icon Sources",
                "No folders with valid icons were found.",
            )
            return

        decision = QMessageBox.question(
            self,
            "Backfill Missing Icon Sources",
            "\n".join(
                [
                    f"Scope: {'selected' if used_selected_scope else 'all valid icons'}",
                    f"Candidates: {len(targets)}",
                    "",
                    "Only folders without SourceKind/SourceProvider metadata are updated.",
                    "Rule: if not identified as SteamGridDB, source is set to Internet.",
                    "",
                    "Proceed now?",
                ]
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return

        target_paths = {os.path.normpath(item.full_path) for item in targets}

        def _run(progress_cb, should_cancel):
            return self.state.backfill_missing_icon_sources(
                targets,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def _done(report: OperationReport):
            for entry in self._visible_right_items:
                if os.path.normpath(entry.full_path) in target_paths:
                    self._refresh_visible_entry_tooltip(entry.full_path)
            lines = [
                f"Updated: {report.succeeded}",
                f"Skipped: {report.skipped}",
                f"Failed: {report.failed}",
            ]
            if report.details:
                lines.append("")
                lines.extend(report.details[:12])
            if int(report.failed) > 0:
                QMessageBox.warning(self, "Backfill Missing Icon Sources", "\n".join(lines))
            else:
                self._show_success_popup("Backfill Missing Icon Sources", "\n".join(lines))

        self._start_report_operation("Backfill Missing Icon Sources", _run, _done)

    def _single_selected_icon_entry(self, action_name: str) -> InventoryItem | None:
        selected = [item for item in self._selected_right_entries() if item.is_dir]
        if len(selected) != 1:
            QMessageBox.information(
                self,
                action_name,
                "Select exactly one game folder.",
            )
            return None
        entry = selected[0]
        if entry.icon_status != "valid" or not entry.folder_icon_path:
            QMessageBox.information(
                self,
                action_name,
                "Selected folder has no valid local icon.",
            )
            return None
        return entry

    def _ensure_sgdb_configured(self, action_name: str) -> bool:
        settings = self.state.icon_search_settings()
        if settings.steamgriddb_enabled and settings.steamgriddb_api_key.strip():
            return True
        QMessageBox.warning(
            self,
            action_name,
            "SteamGridDB is not configured. Enable it and add an API key in Icon Provider Settings.",
        )
        return False

    @staticmethod
    def _perfect_confidence_candidates(
        candidates: list[SgdbGameCandidate],
    ) -> list[SgdbGameCandidate]:
        return [
            c
            for c in list(candidates or [])
            if abs(float(c.confidence) - 1.0) <= 1e-9
        ]

    def _on_assign_steam_appid_selected(self) -> None:
        self._start_assign_steam_appids_flow(all_visible=False)

    def _on_assign_steam_appid_all_visible(self) -> None:
        self._start_assign_steam_appids_flow(all_visible=True)

    def _confirm_single_steamid_reselection(self, entry: InventoryItem) -> tuple[bool, bool]:
        existing = self.state.read_assigned_steam_appid(entry.full_path).strip()
        if not existing:
            return True, False
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Assign SteamID")
        box.setText(
            f"Current SteamID for '{entry.cleaned_name or entry.full_name}' is {existing}."
        )
        box.setInformativeText(
            "Reselect now, keep automatic selection, or cancel."
        )
        reselect_btn = box.addButton("Reselect...", QMessageBox.ButtonRole.YesRole)
        auto_btn = box.addButton("Auto", QMessageBox.ButtonRole.NoRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(auto_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked == cancel_btn:
            return False, False
        if clicked == reselect_btn:
            return True, True
        return True, False

    @staticmethod
    def _resolve_candidate_with_steam_appid(
        state,
        candidate: SgdbGameCandidate,
    ) -> tuple[SgdbGameCandidate, str]:
        selected = candidate
        appid = str(candidate.steam_appid or "").strip()
        if not appid:
            identity_store = normalize_store_name(str(candidate.identity_store or ""))
            identity_store_id = str(candidate.identity_store_id or "").strip()
            if identity_store == "Steam" and identity_store_id.isdigit():
                appid = identity_store_id
        if not appid:
            store_ids = dict(getattr(candidate, "store_ids", {}) or {})
            steam_from_map = str(store_ids.get("Steam", "")).strip()
            if steam_from_map.isdigit():
                appid = steam_from_map
        if appid.isdigit():
            return selected, appid
        if int(getattr(candidate, "game_id", 0) or 0) <= 0:
            return selected, ""
        details = state.resolve_sgdb_game_by_id(int(candidate.game_id))
        selected = details
        appid = str(details.steam_appid or "").strip()
        if appid.isdigit():
            return selected, appid
        return selected, ""

    @staticmethod
    def _is_exact_normalized_name_match(
        entry: InventoryItem,
        candidate: SgdbGameCandidate,
    ) -> bool:
        left = normalize_name_for_compare(entry.cleaned_name or entry.full_name)
        right = normalize_name_for_compare(candidate.title)
        return bool(left and right and left == right)

    def _unique_exact_normalized_full_confidence_candidate(
        self,
        entry: InventoryItem,
        candidates: list[SgdbGameCandidate],
    ) -> SgdbGameCandidate | None:
        exact = [
            c
            for c in list(candidates or [])
            if abs(float(c.confidence) - 1.0) <= 1e-9
            and self._is_exact_normalized_name_match(entry, c)
        ]
        if len(exact) == 1:
            return exact[0]
        return None

    def _apply_candidate_identity(
        self,
        *,
        entry: InventoryItem,
        selected_candidate: SgdbGameCandidate,
        steam_appid: str,
    ) -> tuple[bool, list[str]]:
        identity_store = normalize_store_name(str(selected_candidate.identity_store or ""))
        identity_store_id = str(selected_candidate.identity_store_id or "").strip()
        discovered_store_ids: dict[str, str] = {}
        try:
            discovered_store_ids = self.state.discover_store_ids_for_title(
                selected_candidate.title
            )
        except Exception:
            discovered_store_ids = {}
        merged_store_ids: dict[str, str] = {}
        for raw_store, raw_id in dict(getattr(selected_candidate, "store_ids", {}) or {}).items():
            canonical = normalize_store_name(str(raw_store or "").strip())
            token = str(raw_id or "").strip()
            if canonical and token:
                merged_store_ids[canonical] = token
        for raw_store, raw_id in dict(discovered_store_ids or {}).items():
            canonical = normalize_store_name(str(raw_store or "").strip())
            token = str(raw_id or "").strip()
            if canonical and token and canonical not in merged_store_ids:
                merged_store_ids[canonical] = token
        if identity_store and identity_store_id:
            merged_store_ids[identity_store] = identity_store_id
        if steam_appid:
            merged_store_ids["Steam"] = steam_appid
        changed = False
        applied: list[str] = []
        self.state.clear_owned_store_info_for_inventory(entry.full_path)
        for store_name in sort_stores(list(merged_store_ids.keys())):
            store_id = str(merged_store_ids.get(store_name, "")).strip()
            if not store_id:
                continue
            changed = (
                self.state.assign_store_id_hint(
                    folder_path=entry.full_path,
                    store_name=store_name,
                    store_id=store_id,
                )
                or changed
            )
            applied.append(f"{store_name}={store_id}")
        game_id = int(getattr(selected_candidate, "game_id", 0) or 0)
        if game_id > 0:
            self.state.save_sgdb_binding(
                entry.full_path,
                game_id,
                selected_candidate.title,
                float(selected_candidate.confidence),
                list(selected_candidate.evidence),
            )
            self.state.upsert_folder_icon_metadata(
                entry.full_path,
                {"SourceGameId": str(game_id)},
            )
        else:
            self.state.upsert_folder_icon_metadata(
                entry.full_path,
                {"SourceGameId": None},
            )
        return changed, applied

    def _resolve_manual_target_id(self, source: str, raw_id: str) -> SgdbGameCandidate:
        source_token = normalize_store_name(str(source or "").strip())
        id_token = str(raw_id or "").strip()
        if not source_token or not id_token:
            raise ValueError("Manual source and ID are required.")
        if source_token == "SGDB":
            if not id_token.isdigit():
                raise ValueError("SGDB Game ID must be numeric.")
            return self._run_ui_pumped_call(
                "Resolve manual SGDB ID",
                lambda: self.state.resolve_sgdb_game_by_id(int(id_token)),
            )
        return self._run_ui_pumped_call(
            f"Resolve manual {source_token} ID",
            lambda: self.state.resolve_sgdb_game_by_store_id(source_token, id_token),
        )

    @staticmethod
    def _manual_store_options_for_entry(entry: InventoryItem) -> list[str]:
        options = ["Steam"]
        options.extend(list(entry.owned_stores or []))
        return sort_stores(options)

    def _owned_store_targets_for_entry(self, entry: InventoryItem) -> list[dict[str, str]]:
        title = entry.cleaned_name or entry.full_name
        return self.state.store_targets_for_inventory(entry.full_path, game_title=title)

    def _active_sgdb_picker_flow_state(self) -> dict[str, object] | None:
        state = getattr(self, "_sgdb_picker_flow_state", None)
        if isinstance(state, dict):
            return state
        return None

    def _confirm_interrupt_active_sgdb_picker_flow(self, next_flow_title: str) -> bool:
        active = self._active_sgdb_picker_flow_state()
        if active is None:
            return True
        current_title = str(active.get("flow_title") or "Current flow").strip() or "Current flow"
        requested_title = str(next_flow_title or "New flow").strip() or "New flow"
        answer = QMessageBox.question(
            self,
            "Flow Already Running",
            "\n".join(
                [
                    f"'{current_title}' is currently open/minimized.",
                    "",
                    f"Starting '{requested_title}' will cancel the remaining items in '{current_title}'.",
                    "Do you want to continue?",
                ]
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._interrupt_active_sgdb_picker_flow()
        return True

    def _interrupt_active_sgdb_picker_flow(self) -> None:
        active = self._active_sgdb_picker_flow_state()
        if active is None:
            return
        self._finish_modeless_sgdb_picker_flow(
            active,
            canceled=True,
            interrupted=True,
        )

    def _dialog_payload_for_manual_job(
        self,
        job: dict[str, object],
    ) -> tuple[str, str, list[SgdbGameCandidate], list[str], str, list[str], list[dict[str, str]]]:
        entry_obj = job.get("entry")
        entry = entry_obj if isinstance(entry_obj, InventoryItem) else None
        folder_name = str(job.get("folder_name") or "").strip()
        if not folder_name and entry is not None:
            folder_name = entry.cleaned_name or entry.full_name
        folder_path = str(job.get("folder_path") or "").strip()
        if not folder_path and entry is not None:
            folder_path = entry.full_path
        icon_path = str(job.get("icon_path") or "").strip()
        if not icon_path and entry is not None:
            icon_path = str(entry.folder_icon_path or "").strip()
        candidates = [
            candidate
            for candidate in list(job.get("candidates") or [])
            if isinstance(candidate, SgdbGameCandidate)
        ]
        drift_reasons = [
            str(value).strip()
            for value in list(job.get("drift_reasons") or [])
            if str(value).strip()
        ]
        manual_store_options = [
            str(value).strip()
            for value in list(job.get("manual_store_options") or [])
            if str(value).strip()
        ]
        if not manual_store_options and entry is not None:
            manual_store_options = self._manual_store_options_for_entry(entry)
        owned_store_targets = [
            row
            for row in list(job.get("owned_store_targets") or [])
            if isinstance(row, dict)
        ]
        if not owned_store_targets and entry is not None:
            owned_store_targets = self._owned_store_targets_for_entry(entry)
        return (
            folder_name,
            folder_path,
            candidates,
            drift_reasons,
            icon_path,
            manual_store_options,
            owned_store_targets,
        )

    def _start_modeless_sgdb_picker_flow(
        self,
        *,
        flow_title: str,
        jobs: list[dict[str, object]],
        on_job_result: Callable[
            [dict[str, object], SgdbGameCandidate | None, bool, bool],
            None,
        ],
        on_complete: Callable[[bool, bool], None],
    ) -> None:
        if not jobs:
            on_complete(False, False)
            return
        state: dict[str, object] = {
            "flow_title": str(flow_title or "Select target").strip() or "Select target",
            "jobs": list(jobs),
            "index": 0,
            "dialog": None,
            "on_job_result": on_job_result,
            "on_complete": on_complete,
        }
        self._sgdb_picker_flow_state = state
        self._begin_interactive_operation(str(state["flow_title"]), len(jobs))
        QTimer.singleShot(
            0,
            lambda _state=state: self._continue_modeless_sgdb_picker_flow(_state),
        )

    def _continue_modeless_sgdb_picker_flow(self, state: dict[str, object]) -> None:
        if self._active_sgdb_picker_flow_state() is not state:
            return
        jobs = list(state.get("jobs") or [])
        total = len(jobs)
        index = int(state.get("index", 0))
        flow_title = str(state.get("flow_title") or "Select target").strip() or "Select target"
        if index >= total:
            self._finish_modeless_sgdb_picker_flow(
                state,
                canceled=False,
                interrupted=False,
            )
            return
        if self._step_interactive_operation(flow_title, index, max(1, total)):
            self._finish_modeless_sgdb_picker_flow(
                state,
                canceled=True,
                interrupted=False,
            )
            return
        job = jobs[index]
        (
            folder_name,
            folder_path,
            candidates,
            drift_reasons,
            icon_path,
            manual_store_options,
            owned_store_targets,
        ) = self._dialog_payload_for_manual_job(job)
        dialog = SgdbTargetPickerDialog(
            folder_name=folder_name,
            folder_path=folder_path,
            candidates=candidates,
            drift_reasons=drift_reasons,
            icon_path=icon_path,
            manual_store_options=manual_store_options,
            owned_store_targets=owned_store_targets,
            manual_id_resolver=self._resolve_manual_target_id,
            parent=self,
        )
        dialog.setModal(False)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        state["dialog"] = dialog
        dialog.finished.connect(
            lambda result, _state=state, _dialog=dialog: self._on_modeless_sgdb_picker_finished(
                _state,
                _dialog,
                int(result),
            )
        )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_modeless_sgdb_picker_finished(
        self,
        state: dict[str, object],
        dialog: SgdbTargetPickerDialog,
        result: int,
    ) -> None:
        if self._active_sgdb_picker_flow_state() is not state:
            return
        state["dialog"] = None
        jobs = list(state.get("jobs") or [])
        index = int(state.get("index", 0))
        if index < 0 or index >= len(jobs):
            self._finish_modeless_sgdb_picker_flow(
                state,
                canceled=True,
                interrupted=False,
            )
            return
        job = jobs[index]
        accepted = result == int(QDialog.DialogCode.Accepted)
        cancel_all_requested = bool(getattr(dialog, "cancel_all_requested", False))
        candidate = dialog.selected_candidate() if accepted else None
        on_job_result = state.get("on_job_result")
        if callable(on_job_result):
            on_job_result(
                job,
                candidate,
                cancel_all_requested,
                accepted,
            )
        if cancel_all_requested:
            self._finish_modeless_sgdb_picker_flow(
                state,
                canceled=True,
                interrupted=False,
            )
            return
        state["index"] = index + 1
        QTimer.singleShot(
            0,
            lambda _state=state: self._continue_modeless_sgdb_picker_flow(_state),
        )

    def _finish_modeless_sgdb_picker_flow(
        self,
        state: dict[str, object],
        *,
        canceled: bool,
        interrupted: bool,
    ) -> None:
        if self._active_sgdb_picker_flow_state() is not state:
            return
        dialog_obj = state.get("dialog")
        state["dialog"] = None
        self._sgdb_picker_flow_state = None
        if isinstance(dialog_obj, QDialog):
            dialog_obj.close()
            dialog_obj.deleteLater()
        self._end_interactive_operation()
        on_complete = state.get("on_complete")
        if callable(on_complete):
            on_complete(canceled, interrupted)

    def _start_assign_steam_appids_flow(self, *, all_visible: bool) -> None:
        title = "Assign SteamID"
        if not self._ensure_sgdb_configured(title):
            return
        if all_visible:
            targets = [entry for entry in self._visible_right_items if entry.is_dir]
            scope_label = "all visible"
        else:
            targets = [entry for entry in self._selected_right_entries() if entry.is_dir]
            scope_label = "selected"
        if not targets:
            QMessageBox.information(
                self,
                title,
                "No game folders found in the chosen scope.",
            )
            return
        single_target = len(targets) == 1
        active_flow_exists = self._active_sgdb_picker_flow_state() is not None
        parallel_single_mode = active_flow_exists and single_target
        if active_flow_exists and (not parallel_single_mode):
            if not self._confirm_interrupt_active_sgdb_picker_flow(title):
                return

        force_manual_paths: set[str] = set()
        if single_target:
            proceed, force_manual = self._confirm_single_steamid_reselection(targets[0])
            if not proceed:
                return
            if force_manual:
                force_manual_paths.add(os.path.normpath(targets[0].full_path))
        else:
            decision = QMessageBox.question(
                self,
                title,
                "\n".join(
                    [
                        f"Scope: {scope_label}",
                        f"Games: {len(targets)}",
                        "",
                        "Rules:",
                        "- Exactly one exact normalized 1.00 candidate: auto-assign",
                        "- Otherwise: selection popup",
                        "",
                        "Proceed?",
                    ]
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if decision != QMessageBox.StandardButton.Yes:
                return

        auto_jobs: list[dict[str, object]] = []
        manual_jobs: list[dict[str, object]] = []

        def _resolve_run(progress_cb, should_cancel):
            report = OperationReport(total=len(targets))
            total = len(targets)
            progress_cb("Resolve SteamID targets", 0, max(1, total))
            for idx, entry in enumerate(targets, start=1):
                if should_cancel():
                    raise OperationCancelled("SteamID assignment canceled.")
                progress_cb("Resolve SteamID targets", idx - 1, max(1, total))
                try:
                    resolution = self.state.resolve_sgdb_target(
                        entry.full_path,
                        entry.cleaned_name or entry.full_name,
                        entry.full_name,
                    )
                except Exception as exc:
                    report.failed += 1
                    report.details.append(
                        f"{entry.full_name}: target resolution failed ({exc})"
                    )
                    progress_cb("Resolve SteamID targets", idx, max(1, total))
                    continue
                exact_auto = self._unique_exact_normalized_full_confidence_candidate(
                    entry,
                    resolution.candidates,
                )
                force_manual = os.path.normpath(entry.full_path) in force_manual_paths
                if not force_manual and exact_auto is not None:
                    auto_jobs.append(
                        {
                            "entry": entry,
                            "candidate": exact_auto,
                            "reason": "exact normalized 1.00 match",
                        }
                    )
                else:
                    reason = "manual reselection"
                    if not force_manual:
                        reason = "no unique exact normalized 1.00 match"
                    manual_jobs.append(
                        {
                            "entry": entry,
                            "candidates": resolution.candidates,
                            "drift_reasons": resolution.drift_reasons,
                            "reason": reason,
                        }
                    )
                progress_cb("Resolve SteamID targets", idx, max(1, total))
            progress_cb("Resolve SteamID targets", max(1, total), max(1, total))
            return report

        def _resolve_done(resolve_report: OperationReport):
            selected_jobs: list[dict[str, object]] = list(auto_jobs)
            manual_skipped = 0
            combined_details: list[str] = list(resolve_report.details)

            def _start_apply_phase() -> None:
                def _apply_run(progress_cb, should_cancel):
                    report = OperationReport(total=len(selected_jobs))
                    total = len(selected_jobs)
                    progress_cb("Assign SteamIDs", 0, max(1, total))
                    for idx, job in enumerate(selected_jobs, start=1):
                        if should_cancel():
                            raise OperationCancelled("SteamID assignment canceled.")
                        progress_cb("Assign SteamIDs", idx - 1, max(1, total))
                        entry = job.get("entry")
                        candidate = job.get("candidate")
                        if not isinstance(entry, InventoryItem) or not isinstance(candidate, SgdbGameCandidate):
                            report.failed += 1
                            report.details.append("Skipped invalid assignment payload.")
                            progress_cb("Assign SteamIDs", idx, max(1, total))
                            continue
                        try:
                            selected_candidate, steam_appid = self._resolve_candidate_with_steam_appid(
                                self.state,
                                candidate,
                            )
                        except Exception as exc:
                            report.failed += 1
                            report.details.append(f"{entry.full_name}: lookup failed ({exc})")
                            progress_cb("Assign SteamIDs", idx, max(1, total))
                            continue
                        changed, applied = self._apply_candidate_identity(
                            entry=entry,
                            selected_candidate=selected_candidate,
                            steam_appid=steam_appid,
                        )
                        if not applied:
                            report.skipped += 1
                            report.details.append(
                                f"{entry.full_name}: no resolvable store IDs for selected match."
                            )
                            progress_cb("Assign SteamIDs", idx, max(1, total))
                            continue
                        report.succeeded += 1
                        if changed:
                            report.details.append(
                                f"{entry.full_name}: IDs set ({', '.join(applied)})"
                            )
                        else:
                            report.details.append(
                                f"{entry.full_name}: IDs unchanged ({', '.join(applied)})"
                            )
                        progress_cb("Assign SteamIDs", idx, max(1, total))
                    linked = self.state.rebuild_store_links_from_inventory(list(self.inventory))
                    report.details.append(f"Ownership links rebuilt: {linked}")
                    progress_cb("Assign SteamIDs", max(1, total), max(1, total))
                    return report

                def _apply_done(apply_report: OperationReport):
                    succeeded = int(apply_report.succeeded)
                    failed = int(resolve_report.failed) + int(apply_report.failed)
                    skipped = int(resolve_report.skipped) + int(manual_skipped) + int(apply_report.skipped)
                    final_details = combined_details + list(apply_report.details)
                    lines = [
                        f"Succeeded: {succeeded}",
                        f"Skipped: {skipped}",
                        f"Failed: {failed}",
                    ]
                    if final_details:
                        lines.append("")
                        lines.extend(final_details[:12])
                    if not (single_target and succeeded > 0 and failed == 0 and skipped == 0):
                        if failed > 0:
                            QMessageBox.warning(self, title, "\n".join(lines))
                        elif skipped > 0:
                            QMessageBox.information(self, title, "\n".join(lines))
                        else:
                            self._show_success_popup(title, "\n".join(lines))
                    QTimer.singleShot(0, self.refresh_all)

                started = self._start_report_operation(
                    title,
                    _apply_run,
                    _apply_done,
                )
                if not started:
                    QTimer.singleShot(50, _start_apply_phase)

            def _finish_manual_phase(canceled: bool, interrupted: bool) -> None:
                if canceled:
                    if not interrupted:
                        QMessageBox.information(self, title, "Operation canceled for remaining items.")
                    return
                if not selected_jobs:
                    failed = int(resolve_report.failed)
                    skipped = int(resolve_report.skipped) + int(manual_skipped)
                    lines = [
                        "Succeeded: 0",
                        f"Skipped: {skipped}",
                        f"Failed: {failed}",
                    ]
                    if combined_details:
                        lines.append("")
                        lines.extend(combined_details[:12])
                    if failed > 0:
                        QMessageBox.warning(self, title, "\n".join(lines))
                    elif skipped > 0:
                        QMessageBox.information(self, title, "\n".join(lines))
                    else:
                        self._show_success_popup(title, "\n".join(lines))
                    return
                QTimer.singleShot(0, _start_apply_phase)

            if not manual_jobs:
                _finish_manual_phase(False, False)
                return

            if parallel_single_mode:
                cancel_all_requested = False
                for job in manual_jobs:
                    entry = job.get("entry")
                    if not isinstance(entry, InventoryItem):
                        manual_skipped += 1
                        continue
                    dialog = SgdbTargetPickerDialog(
                        folder_name=entry.cleaned_name or entry.full_name,
                        folder_path=entry.full_path,
                        candidates=list(job.get("candidates") or []),
                        drift_reasons=list(job.get("drift_reasons") or []),
                        icon_path=str(entry.folder_icon_path or ""),
                        manual_store_options=self._manual_store_options_for_entry(entry),
                        owned_store_targets=self._owned_store_targets_for_entry(entry),
                        manual_id_resolver=self._resolve_manual_target_id,
                        parent=self,
                    )
                    if dialog.exec() != QDialog.DialogCode.Accepted:
                        if getattr(dialog, "cancel_all_requested", False):
                            cancel_all_requested = True
                            break
                        manual_skipped += 1
                        combined_details.append(f"{entry.full_name}: skipped by user")
                        continue
                    candidate = dialog.selected_candidate()
                    if candidate is None:
                        manual_skipped += 1
                        combined_details.append(f"{entry.full_name}: skipped (no game selected)")
                        continue
                    selected_jobs.append({"entry": entry, "candidate": candidate, "reason": "manual"})
                _finish_manual_phase(cancel_all_requested, False)
                return

            def _on_manual_result(
                job: dict[str, object],
                candidate: SgdbGameCandidate | None,
                cancel_all_requested: bool,
                accepted: bool,
            ) -> None:
                nonlocal manual_skipped
                entry = job.get("entry")
                if not isinstance(entry, InventoryItem):
                    manual_skipped += 1
                    return
                if cancel_all_requested:
                    return
                if candidate is None:
                    manual_skipped += 1
                    if accepted:
                        combined_details.append(f"{entry.full_name}: skipped (no game selected)")
                    else:
                        combined_details.append(f"{entry.full_name}: skipped by user")
                    return
                selected_jobs.append({"entry": entry, "candidate": candidate, "reason": "manual"})

            for job in manual_jobs:
                entry = job.get("entry")
                if not isinstance(entry, InventoryItem):
                    continue
                reasons = [str(job.get("reason") or "").strip()]
                reasons.extend(
                    [
                        str(value).strip()
                        for value in list(job.get("drift_reasons") or [])
                        if str(value).strip()
                    ]
                )
                job["drift_reasons"] = [value for value in reasons if value]

            self._start_modeless_sgdb_picker_flow(
                flow_title=title,
                jobs=manual_jobs,
                on_job_result=_on_manual_result,
                on_complete=_finish_manual_phase,
            )

        self._start_report_operation(title, _resolve_run, _resolve_done)

    def _on_recheck_ids_and_stores_selected(self) -> None:
        self._start_recheck_ids_and_stores_flow(all_visible=False)

    def _on_recheck_ids_and_stores_all_visible(self) -> None:
        self._start_recheck_ids_and_stores_flow(all_visible=True)

    def _current_identity_for_entry(self, entry: InventoryItem) -> tuple[int, str]:
        binding = self.state.get_sgdb_binding(entry.full_path)
        game_id = int(binding.game_id) if binding is not None else 0
        steam_appid = str(self.state.read_assigned_steam_appid(entry.full_path) or "").strip()
        return game_id, steam_appid

    def _start_recheck_ids_and_stores_flow(self, *, all_visible: bool) -> None:
        title = "Recheck IDs and Stores"
        if not self._ensure_sgdb_configured(title):
            return
        if all_visible:
            targets = [entry for entry in self._visible_right_items if entry.is_dir]
            scope_label = "all visible"
        else:
            targets = [entry for entry in self._selected_right_entries() if entry.is_dir]
            scope_label = "selected"
        if not targets:
            QMessageBox.information(
                self,
                title,
                "No game folders found in the chosen scope.",
            )
            return
        single_target = len(targets) == 1
        active_flow_exists = self._active_sgdb_picker_flow_state() is not None
        parallel_single_mode = active_flow_exists and single_target
        if active_flow_exists and (not parallel_single_mode):
            if not self._confirm_interrupt_active_sgdb_picker_flow(title):
                return
        decision = QMessageBox.question(
            self,
            title,
            "\n".join(
                [
                    f"Scope: {scope_label}",
                    f"Games: {len(targets)}",
                    "",
                    "Rules:",
                    "- Re-evaluate IDs from scratch (ignores assigned ID hints).",
                    "- If IDs differ, picker opens unless new result is exact normalized name + confidence 1.00.",
                    "",
                    "Proceed?",
                ]
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return

        auto_jobs: list[dict[str, object]] = []
        manual_jobs: list[dict[str, object]] = []

        def _resolve_run(progress_cb, should_cancel):
            report = OperationReport(total=len(targets))
            total = len(targets)
            progress_cb("Recheck IDs", 0, max(1, total))
            for idx, entry in enumerate(targets, start=1):
                if should_cancel():
                    raise OperationCancelled("Recheck IDs and stores canceled.")
                progress_cb("Recheck IDs", idx - 1, max(1, total))
                current_game_id, current_steam = self._current_identity_for_entry(entry)
                try:
                    resolution = self.state.resolve_sgdb_target(
                        entry.full_path,
                        entry.cleaned_name or entry.full_name,
                        entry.full_name,
                        include_assigned_hints=False,
                    )
                except Exception as exc:
                    report.failed += 1
                    report.details.append(
                        f"{entry.full_name}: target recheck failed ({exc})"
                    )
                    progress_cb("Recheck IDs", idx, max(1, total))
                    continue
                exact_auto = self._unique_exact_normalized_full_confidence_candidate(
                    entry,
                    resolution.candidates,
                )
                top = exact_auto or (resolution.candidates[0] if resolution.candidates else None)
                if top is None:
                    if current_game_id > 0 or current_steam:
                        manual_jobs.append(
                            {
                                "entry": entry,
                                "candidates": [],
                                "drift_reasons": ["No candidate found from scratch; manual review required."],
                                "reason": "no candidate",
                            }
                        )
                    else:
                        report.skipped += 1
                        report.details.append(f"{entry.full_name}: unchanged (no current IDs, no candidate)")
                    progress_cb("Recheck IDs", idx, max(1, total))
                    continue
                try:
                    proposed_candidate, proposed_steam = self._resolve_candidate_with_steam_appid(
                        self.state,
                        top,
                    )
                except Exception:
                    proposed_candidate = top
                    proposed_steam = str(top.steam_appid or "").strip()
                proposed_game_id = int(getattr(proposed_candidate, "game_id", 0) or 0)
                differs = (proposed_game_id != current_game_id) or (proposed_steam != current_steam)
                if not differs:
                    report.skipped += 1
                    report.details.append(f"{entry.full_name}: unchanged")
                    progress_cb("Recheck IDs", idx, max(1, total))
                    continue
                exact_confident = exact_auto is not None
                if exact_confident:
                    auto_jobs.append(
                        {
                            "entry": entry,
                            "candidate": proposed_candidate,
                            "reason": "exact normalized 1.00",
                        }
                    )
                else:
                    manual_jobs.append(
                        {
                            "entry": entry,
                            "candidates": resolution.candidates,
                            "drift_reasons": resolution.drift_reasons,
                            "reason": (
                                f"IDs changed (current SGDB={current_game_id or '-'}, Steam={current_steam or '-'}; "
                                f"new SGDB={proposed_game_id or '-'}, Steam={proposed_steam or '-'})"
                            ),
                        }
                    )
                progress_cb("Recheck IDs", idx, max(1, total))
            progress_cb("Recheck IDs", max(1, total), max(1, total))
            return report

        def _resolve_done(resolve_report: OperationReport):
            selected_jobs: list[dict[str, object]] = list(auto_jobs)
            manual_skipped = 0
            combined_details: list[str] = list(resolve_report.details)

            def _start_apply_phase() -> None:
                def _apply_run(progress_cb, should_cancel):
                    report = OperationReport(total=len(selected_jobs))
                    total = len(selected_jobs)
                    progress_cb("Apply rechecked IDs", 0, max(1, total))
                    for idx, job in enumerate(selected_jobs, start=1):
                        if should_cancel():
                            raise OperationCancelled("Recheck IDs and stores canceled.")
                        progress_cb("Apply rechecked IDs", idx - 1, max(1, total))
                        entry = job.get("entry")
                        candidate = job.get("candidate")
                        if not isinstance(entry, InventoryItem) or not isinstance(candidate, SgdbGameCandidate):
                            report.failed += 1
                            report.details.append("Skipped invalid recheck payload.")
                            progress_cb("Apply rechecked IDs", idx, max(1, total))
                            continue
                        try:
                            selected_candidate, steam_appid = self._resolve_candidate_with_steam_appid(
                                self.state,
                                candidate,
                            )
                        except Exception as exc:
                            report.failed += 1
                            report.details.append(f"{entry.full_name}: lookup failed ({exc})")
                            progress_cb("Apply rechecked IDs", idx, max(1, total))
                            continue
                        changed, applied = self._apply_candidate_identity(
                            entry=entry,
                            selected_candidate=selected_candidate,
                            steam_appid=steam_appid,
                        )
                        if not applied:
                            report.skipped += 1
                            report.details.append(f"{entry.full_name}: no resolvable IDs from selection.")
                            progress_cb("Apply rechecked IDs", idx, max(1, total))
                            continue
                        report.succeeded += 1
                        if changed:
                            report.details.append(f"{entry.full_name}: IDs updated ({', '.join(applied)})")
                        else:
                            report.details.append(f"{entry.full_name}: IDs unchanged ({', '.join(applied)})")
                        progress_cb("Apply rechecked IDs", idx, max(1, total))
                    linked = self.state.rebuild_store_links_from_inventory(list(self.inventory))
                    report.details.append(f"Ownership links rebuilt: {linked}")
                    progress_cb("Apply rechecked IDs", max(1, total), max(1, total))
                    return report

                def _apply_done(apply_report: OperationReport):
                    succeeded = int(apply_report.succeeded)
                    failed = int(resolve_report.failed) + int(apply_report.failed)
                    skipped = int(resolve_report.skipped) + int(manual_skipped) + int(apply_report.skipped)
                    final_details = combined_details + list(apply_report.details)
                    lines = [
                        f"Succeeded: {succeeded}",
                        f"Skipped: {skipped}",
                        f"Failed: {failed}",
                    ]
                    if final_details:
                        lines.append("")
                        lines.extend(final_details[:12])
                    if not (single_target and succeeded > 0 and failed == 0 and skipped == 0):
                        if failed > 0:
                            QMessageBox.warning(self, title, "\n".join(lines))
                        elif skipped > 0:
                            QMessageBox.information(self, title, "\n".join(lines))
                        else:
                            self._show_success_popup(title, "\n".join(lines))
                    QTimer.singleShot(0, self.refresh_all)

                started = self._start_report_operation(
                    title,
                    _apply_run,
                    _apply_done,
                )
                if not started:
                    QTimer.singleShot(50, _start_apply_phase)

            def _finish_manual_phase(canceled: bool, interrupted: bool) -> None:
                if canceled:
                    if not interrupted:
                        QMessageBox.information(self, title, "Operation canceled for remaining items.")
                    return
                if not selected_jobs:
                    failed = int(resolve_report.failed)
                    skipped = int(resolve_report.skipped) + int(manual_skipped)
                    lines = [
                        "Succeeded: 0",
                        f"Skipped: {skipped}",
                        f"Failed: {failed}",
                    ]
                    if combined_details:
                        lines.append("")
                        lines.extend(combined_details[:12])
                    if not (single_target and failed == 0 and skipped == 0):
                        if failed > 0:
                            QMessageBox.warning(self, title, "\n".join(lines))
                        elif skipped > 0:
                            QMessageBox.information(self, title, "\n".join(lines))
                        else:
                            self._show_success_popup(title, "\n".join(lines))
                    return
                QTimer.singleShot(0, _start_apply_phase)

            if not manual_jobs:
                _finish_manual_phase(False, False)
                return

            if parallel_single_mode:
                cancel_all_requested = False
                for job in manual_jobs:
                    entry = job.get("entry")
                    if not isinstance(entry, InventoryItem):
                        manual_skipped += 1
                        continue
                    dialog = SgdbTargetPickerDialog(
                        folder_name=entry.cleaned_name or entry.full_name,
                        folder_path=entry.full_path,
                        candidates=list(job.get("candidates") or []),
                        drift_reasons=list(job.get("drift_reasons") or []),
                        icon_path=str(entry.folder_icon_path or ""),
                        manual_store_options=self._manual_store_options_for_entry(entry),
                        owned_store_targets=self._owned_store_targets_for_entry(entry),
                        manual_id_resolver=self._resolve_manual_target_id,
                        parent=self,
                    )
                    if dialog.exec() != QDialog.DialogCode.Accepted:
                        if getattr(dialog, "cancel_all_requested", False):
                            cancel_all_requested = True
                            break
                        manual_skipped += 1
                        combined_details.append(f"{entry.full_name}: skipped by user")
                        continue
                    candidate = dialog.selected_candidate()
                    if candidate is None:
                        manual_skipped += 1
                        combined_details.append(f"{entry.full_name}: skipped (no game selected)")
                        continue
                    selected_jobs.append({"entry": entry, "candidate": candidate, "reason": "manual"})
                _finish_manual_phase(cancel_all_requested, False)
                return

            def _on_manual_result(
                job: dict[str, object],
                candidate: SgdbGameCandidate | None,
                cancel_all_requested: bool,
                accepted: bool,
            ) -> None:
                nonlocal manual_skipped
                entry = job.get("entry")
                if not isinstance(entry, InventoryItem):
                    manual_skipped += 1
                    return
                if cancel_all_requested:
                    return
                if candidate is None:
                    manual_skipped += 1
                    if accepted:
                        combined_details.append(f"{entry.full_name}: skipped (no game selected)")
                    else:
                        combined_details.append(f"{entry.full_name}: skipped by user")
                    return
                selected_jobs.append({"entry": entry, "candidate": candidate, "reason": "manual"})

            for job in manual_jobs:
                entry = job.get("entry")
                if not isinstance(entry, InventoryItem):
                    continue
                reasons = [str(job.get("reason") or "").strip()]
                reasons.extend(
                    [
                        str(value).strip()
                        for value in list(job.get("drift_reasons") or [])
                        if str(value).strip()
                    ]
                )
                job["drift_reasons"] = [value for value in reasons if value]

            self._start_modeless_sgdb_picker_flow(
                flow_title=title,
                jobs=manual_jobs,
                on_job_result=_on_manual_result,
                on_complete=_finish_manual_phase,
            )

        self._start_report_operation(title, _resolve_run, _resolve_done)

    def _resolve_upload_target_for_entry(
        self,
        entry: InventoryItem,
        icon_path: str,
    ) -> SgdbGameCandidate | None:
        try:
            resolution = self._run_ui_pumped_call(
                "Resolve SGDB target",
                lambda: self.state.resolve_sgdb_target(
                    entry.full_path,
                    entry.cleaned_name or entry.full_name,
                    entry.full_name,
                ),
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Upload Icon to SteamGridDB",
                f"Could not resolve SGDB target:\n{exc}",
            )
            return None
        auto = self._unique_exact_normalized_full_confidence_candidate(
            entry,
            resolution.candidates,
        )
        if auto is not None:
            return auto

        dialog = SgdbTargetPickerDialog(
            folder_name=entry.cleaned_name or entry.full_name,
            folder_path=entry.full_path,
            candidates=resolution.candidates,
            drift_reasons=resolution.drift_reasons,
            icon_path=icon_path,
            manual_store_options=self._manual_store_options_for_entry(entry),
            owned_store_targets=self._owned_store_targets_for_entry(entry),
            manual_id_resolver=self._resolve_manual_target_id,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        candidate = dialog.selected_candidate()
        if candidate is None:
            return None
        if candidate.title.startswith("SGDB Game "):
            try:
                resolved = self.state.resolve_sgdb_game_by_id(int(candidate.game_id))
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Upload Icon to SteamGridDB",
                    f"Could not resolve manual SGDB game ID {int(candidate.game_id)}:\n{exc}",
                )
                return None
            return resolved
        return candidate

    def _on_steamgriddb_status_selected(self) -> None:
        if not self._ensure_sgdb_configured("SteamGridDB Status"):
            return
        entry = self._single_selected_icon_entry("SteamGridDB Status")
        if entry is None:
            return
        icon_path = str(entry.folder_icon_path or "").strip()
        if not icon_path:
            return

        def _run(progress_cb, should_cancel):
            progress_cb("SteamGridDB status", 0, 3)
            report = OperationReport(total=1)
            try:
                resolution = self.state.resolve_sgdb_target(
                    entry.full_path,
                    entry.cleaned_name or entry.full_name,
                    entry.full_name,
                )
                progress_cb("SteamGridDB status", 1, 3)
                origin = self.state.detect_sgdb_origin_status(
                    folder_path=entry.full_path,
                    icon_path=icon_path,
                    cleaned_name=entry.cleaned_name or entry.full_name,
                    full_name=entry.full_name,
                    threshold=0.95,
                )
                progress_cb("SteamGridDB status", 2, 3)
                latest = self.state.latest_sgdb_upload_for_folder(entry.full_path)
                report.succeeded = 1
                lines = []
                if resolution.saved_binding is not None:
                    lines.append(
                        "Saved binding: "
                        f"{resolution.saved_binding.game_name} (ID {resolution.saved_binding.game_id})"
                    )
                else:
                    lines.append("Saved binding: none")
                lines.append(
                    "Target confirmation required: "
                    f"{'yes' if resolution.requires_confirmation else 'no'}"
                )
                if resolution.drift_reasons:
                    lines.append("Drift: " + "; ".join(resolution.drift_reasons))
                if resolution.candidates:
                    top = resolution.candidates[0]
                    lines.append(
                        f"Top candidate: {top.title} (ID {top.game_id}, conf {top.confidence:.2f})"
                    )
                lines.append(
                    "Origin: "
                    f"{'SteamGridDB' if origin.is_sgdb_origin else 'Not confirmed'} "
                    f"(kind={origin.source_kind}, confidence={origin.confidence:.2f}, reason={origin.reason})"
                )
                if latest:
                    lines.append(
                        "Last upload: "
                        f"{latest.get('uploaded_at', '')} | status={latest.get('status', '')} | "
                        f"game_id={latest.get('game_id', '')}"
                    )
                else:
                    lines.append("Last upload: none")
                report.details = lines
            except Exception as exc:
                report.failed = 1
                report.details = [f"Status lookup failed: {exc}"]
            progress_cb("SteamGridDB status", 3, 3)
            return report

        def _done(report: OperationReport):
            title = "SteamGridDB Status"
            if report.failed > 0:
                QMessageBox.warning(self, title, "\n".join(report.details[:12]))
                return
            self._show_success_popup(title, "\n".join(report.details[:12]))

        self._start_report_operation("SteamGridDB Status", _run, _done)

    def _on_upload_icon_to_steamgriddb_selected(self) -> None:
        title = "Upload Icon to SteamGridDB"
        if not self._ensure_sgdb_configured(title):
            return
        entry = self._single_selected_icon_entry(title)
        if entry is None:
            return
        icon_path = str(entry.folder_icon_path or "").strip()
        if not icon_path:
            return
        self._start_upload_icon_to_steamgriddb_flow(
            entry=entry,
            icon_path=icon_path,
            title=title,
            show_already_sgdb_message=True,
        )

    def _start_upload_icon_to_steamgriddb_flow(
        self,
        *,
        entry: InventoryItem,
        icon_path: str,
        title: str,
        show_already_sgdb_message: bool = True,
    ) -> None:
        metadata = self.state.read_folder_icon_metadata(entry.full_path)
        source_kind = str(metadata.get("SourceKind", "")).strip().casefold()
        source_provider = str(metadata.get("SourceProvider", "")).strip().casefold()
        if source_kind in {"sgdb_raw", "sgdb_modified"} or source_provider == "steamgriddb":
            if show_already_sgdb_message:
                QMessageBox.information(
                    self,
                    title,
                    "Upload disabled: this icon source is already SteamGridDB.",
                )
            return

        target = self._resolve_upload_target_for_entry(entry, icon_path)
        if target is None:
            return

        def _after_presence_check() -> None:
            if not self._confirm_sgdb_upload_with_preview(entry, target, icon_path):
                return

            def _run(progress_cb, should_cancel):
                progress_cb("Upload icon", 0, 1)
                report = self.state.upload_folder_icon_to_sgdb(
                    folder_path=entry.full_path,
                    icon_path=icon_path,
                    game=target,
                )
                progress_cb("Upload icon", 1, 1)
                return report

            def _done(report: OperationReport):
                lines = [
                    f"Succeeded: {report.succeeded}",
                    f"Skipped: {report.skipped}",
                    f"Failed: {report.failed}",
                ]
                if report.details:
                    lines.append("")
                    lines.extend(report.details[:8])
                if report.succeeded > 0:
                    self._refresh_visible_entry_tooltip(entry.full_path)
                if report.succeeded > 0 and report.failed == 0 and report.skipped == 0:
                    return
                if int(report.failed) > 0:
                    QMessageBox.warning(self, title, "\n".join(lines))
                else:
                    QMessageBox.information(self, title, "\n".join(lines))

            self._start_report_operation(title, _run, _done)

        self._precheck_icon_not_already_on_sgdb(
            entry=entry,
            icon_path=icon_path,
            game=target,
            on_clear=_after_presence_check,
        )

    def _on_upload_missing_icons_to_steamgriddb_selected(self) -> None:
        title = "Upload Missing Icons to SteamGridDB"
        if not self._ensure_sgdb_configured(title):
            return
        targets, selected_scope = self._sgdb_upload_target_entries()
        if not targets:
            QMessageBox.information(
                self,
                title,
                "No folders with valid local icons were found.",
            )
            return
        single_target = len(targets) == 1
        active_flow_exists = self._active_sgdb_picker_flow_state() is not None
        parallel_single_mode = active_flow_exists and single_target
        if active_flow_exists and (not parallel_single_mode):
            if not self._confirm_interrupt_active_sgdb_picker_flow(title):
                return
        decision = QMessageBox.question(
            self,
            title,
            "\n".join(
                [
                    f"Scope: {'selected' if selected_scope else 'all valid icons'}",
                    f"Candidates: {len(targets)}",
                    "",
                    "Rules:",
                    "- Source already SGDB: skipped",
                    "- Exactly one candidate at confidence 1.00: auto-upload",
                    "- Otherwise: game picker is shown (Select or Skip)",
                    "",
                    "Proceed with bulk upload?",
                ]
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return

        target_paths = {os.path.normpath(item.full_path) for item in targets}
        auto_jobs: list[dict[str, object]] = []
        manual_jobs: list[dict[str, object]] = []

        def _resolve_run(progress_cb, should_cancel):
            report = OperationReport(total=len(targets))
            total = len(targets)
            progress_cb("Resolve SGDB targets", 0, max(1, total))
            for idx, entry in enumerate(targets, start=1):
                if should_cancel():
                    raise OperationCancelled("Bulk SGDB upload canceled.")
                progress_cb("Resolve SGDB targets", idx - 1, max(1, total))
                icon_path = str(entry.folder_icon_path or "").strip()
                if not icon_path:
                    report.skipped += 1
                    report.details.append(f"{entry.full_name}: skipped (no icon)")
                    continue
                metadata = self.state.read_folder_icon_metadata(entry.full_path)
                source_kind = str(metadata.get("SourceKind", "")).strip().casefold()
                source_provider = str(metadata.get("SourceProvider", "")).strip().casefold()
                if source_kind in {"sgdb_raw", "sgdb_modified"} or source_provider == "steamgriddb":
                    report.skipped += 1
                    report.details.append(f"{entry.full_name}: skipped (source already SGDB)")
                    continue
                try:
                    resolution = self.state.resolve_sgdb_target(
                        entry.full_path,
                        entry.cleaned_name or entry.full_name,
                        entry.full_name,
                    )
                except Exception as exc:
                    report.failed += 1
                    report.details.append(
                        f"{entry.full_name}: target resolution failed ({exc})"
                    )
                    continue
                auto = self._unique_exact_normalized_full_confidence_candidate(
                    entry,
                    resolution.candidates,
                )
                if auto is not None:
                    auto_jobs.append(
                        {
                            "name": entry.full_name,
                            "folder_path": entry.full_path,
                            "icon_path": icon_path,
                            "game": auto,
                        }
                    )
                else:
                    manual_jobs.append(
                        {
                            "name": entry.full_name,
                            "folder_name": entry.cleaned_name or entry.full_name,
                            "folder_path": entry.full_path,
                            "icon_path": icon_path,
                            "candidates": resolution.candidates,
                            "drift_reasons": resolution.drift_reasons,
                        }
                    )
                progress_cb("Resolve SGDB targets", idx, max(1, total))
            progress_cb("Resolve SGDB targets", max(1, total), max(1, total))
            return report

        def _resolve_done(resolve_report: OperationReport):
            selected_jobs = list(auto_jobs)
            manual_skipped = 0
            combined_details: list[str] = list(resolve_report.details)

            def _start_upload_phase() -> None:
                def _upload_run(progress_cb, should_cancel):
                    report = OperationReport(total=len(selected_jobs))
                    total = len(selected_jobs)
                    progress_cb("Upload missing SGDB icons", 0, max(1, total))
                    for idx, job in enumerate(selected_jobs, start=1):
                        if should_cancel():
                            raise OperationCancelled("Bulk SGDB upload canceled.")
                        progress_cb("Upload missing SGDB icons", idx - 1, max(1, total))
                        game = job.get("game")
                        if not isinstance(game, SgdbGameCandidate):
                            report.failed += 1
                            report.details.append(f"{job.get('name')}: upload skipped (invalid target)")
                            progress_cb("Upload missing SGDB icons", idx, max(1, total))
                            continue
                        result = self.state.upload_folder_icon_to_sgdb(
                            folder_path=str(job.get("folder_path") or ""),
                            icon_path=str(job.get("icon_path") or ""),
                            game=game,
                        )
                        report.succeeded += int(result.succeeded)
                        report.skipped += int(result.skipped)
                        report.failed += int(result.failed)
                        if result.details:
                            report.details.append(f"{job.get('name')}: {result.details[0]}")
                        progress_cb("Upload missing SGDB icons", idx, max(1, total))
                    progress_cb("Upload missing SGDB icons", max(1, total), max(1, total))
                    return report

                def _upload_done(upload_report: OperationReport):
                    for entry in self._visible_right_items:
                        if os.path.normpath(entry.full_path) in target_paths:
                            self._refresh_visible_entry_tooltip(entry.full_path)
                    succeeded = int(upload_report.succeeded)
                    failed = int(resolve_report.failed) + int(upload_report.failed)
                    skipped = int(resolve_report.skipped) + int(manual_skipped) + int(upload_report.skipped)
                    final_details = combined_details + list(upload_report.details)
                    if succeeded > 0 and failed == 0 and skipped == 0:
                        return
                    lines = [
                        f"Succeeded: {succeeded}",
                        f"Skipped: {skipped}",
                        f"Failed: {failed}",
                    ]
                    if final_details:
                        lines.append("")
                        lines.extend(final_details[:12])
                    if failed > 0:
                        QMessageBox.warning(self, title, "\n".join(lines))
                    elif skipped > 0:
                        QMessageBox.information(self, title, "\n".join(lines))
                    else:
                        self._show_success_popup(title, "\n".join(lines))

                started = self._start_report_operation(
                    title,
                    _upload_run,
                    _upload_done,
                )
                if not started:
                    QTimer.singleShot(50, _start_upload_phase)

            def _finish_manual_phase(canceled: bool, interrupted: bool) -> None:
                if canceled:
                    if not interrupted:
                        QMessageBox.information(self, title, "Operation canceled for remaining items.")
                    return
                if not selected_jobs:
                    succeeded = 0
                    failed = int(resolve_report.failed)
                    skipped = int(resolve_report.skipped) + int(manual_skipped)
                    if succeeded > 0 and failed == 0 and skipped == 0:
                        return
                    lines = [
                        f"Succeeded: {succeeded}",
                        f"Skipped: {skipped}",
                        f"Failed: {failed}",
                    ]
                    if combined_details:
                        lines.append("")
                        lines.extend(combined_details[:12])
                    if failed > 0:
                        QMessageBox.warning(self, title, "\n".join(lines))
                    elif skipped > 0:
                        QMessageBox.information(self, title, "\n".join(lines))
                    else:
                        self._show_success_popup(title, "\n".join(lines))
                    return
                QTimer.singleShot(0, _start_upload_phase)

            if not manual_jobs:
                _finish_manual_phase(False, False)
                return

            if parallel_single_mode:
                cancel_all_requested = False
                for job in manual_jobs:
                    dialog = SgdbTargetPickerDialog(
                        folder_name=str(job.get("folder_name") or ""),
                        folder_path=str(job.get("folder_path") or ""),
                        candidates=list(job.get("candidates") or []),
                        drift_reasons=list(job.get("drift_reasons") or []),
                        icon_path=str(job.get("icon_path") or ""),
                        manual_store_options=list(job.get("manual_store_options") or []),
                        owned_store_targets=list(job.get("owned_store_targets") or []),
                        manual_id_resolver=self._resolve_manual_target_id,
                        parent=self,
                    )
                    if dialog.exec() != QDialog.DialogCode.Accepted:
                        if getattr(dialog, "cancel_all_requested", False):
                            cancel_all_requested = True
                            break
                        manual_skipped += 1
                        combined_details.append(f"{job.get('name')}: skipped by user")
                        continue
                    candidate = dialog.selected_candidate()
                    if candidate is None:
                        manual_skipped += 1
                        combined_details.append(f"{job.get('name')}: skipped (no game selected)")
                        continue
                    name = str(job.get("name") or job.get("folder_name") or "Game")
                    try:
                        present, confidence, matched_icon_id = self._run_ui_pumped_call(
                            "Check SGDB duplicate",
                            lambda: self.state.is_icon_present_on_sgdb_for_game(
                                icon_path=str(job.get("icon_path") or ""),
                                game_id=int(candidate.game_id),
                                threshold=0.95,
                            ),
                        )
                    except Exception as exc:
                        manual_skipped += 1
                        combined_details.append(f"{name}: skipped (presence check failed: {exc})")
                        continue
                    if present:
                        manual_skipped += 1
                        if matched_icon_id is not None:
                            combined_details.append(
                                f"{name}: skipped (already on SGDB, icon ID {int(matched_icon_id)}, conf {confidence:.2f})"
                            )
                        else:
                            combined_details.append(
                                f"{name}: skipped (already on SGDB, conf {confidence:.2f})"
                            )
                        continue
                    selected_jobs.append(
                        {
                            "name": str(job.get("name") or ""),
                            "folder_path": str(job.get("folder_path") or ""),
                            "icon_path": str(job.get("icon_path") or ""),
                            "game": candidate,
                        }
                    )
                _finish_manual_phase(cancel_all_requested, False)
                return

            def _on_manual_result(
                job: dict[str, object],
                candidate: SgdbGameCandidate | None,
                cancel_all_requested: bool,
                accepted: bool,
            ) -> None:
                nonlocal manual_skipped
                name = str(job.get("name") or job.get("folder_name") or "Game")
                if cancel_all_requested:
                    return
                if candidate is None:
                    manual_skipped += 1
                    if accepted:
                        combined_details.append(f"{name}: skipped (no game selected)")
                    else:
                        combined_details.append(f"{name}: skipped by user")
                    return
                try:
                    present, confidence, matched_icon_id = self._run_ui_pumped_call(
                        "Check SGDB duplicate",
                        lambda: self.state.is_icon_present_on_sgdb_for_game(
                            icon_path=str(job.get("icon_path") or ""),
                            game_id=int(candidate.game_id),
                            threshold=0.95,
                        ),
                    )
                except Exception as exc:
                    manual_skipped += 1
                    combined_details.append(f"{name}: skipped (presence check failed: {exc})")
                    return
                if present:
                    manual_skipped += 1
                    if matched_icon_id is not None:
                        combined_details.append(
                            f"{name}: skipped (already on SGDB, icon ID {int(matched_icon_id)}, conf {confidence:.2f})"
                        )
                    else:
                        combined_details.append(
                            f"{name}: skipped (already on SGDB, conf {confidence:.2f})"
                        )
                    return
                selected_jobs.append(
                    {
                        "name": str(job.get("name") or ""),
                        "folder_path": str(job.get("folder_path") or ""),
                        "icon_path": str(job.get("icon_path") or ""),
                        "game": candidate,
                    }
                )

            for job in manual_jobs:
                folder_path = str(job.get("folder_path") or "")
                folder_name = str(job.get("folder_name") or "")
                store_targets = self.state.store_targets_for_inventory(
                    folder_path,
                    game_title=folder_name,
                )
                job["manual_store_options"] = sort_stores(
                    ["Steam"]
                    + [str(row.get("store_name") or "").strip() for row in store_targets]
                )
                job["owned_store_targets"] = store_targets

            self._start_modeless_sgdb_picker_flow(
                flow_title=title,
                jobs=manual_jobs,
                on_job_result=_on_manual_result,
                on_complete=_finish_manual_phase,
            )

        self._start_report_operation(title, _resolve_run, _resolve_done)

    def _precheck_icon_not_already_on_sgdb(
        self,
        *,
        entry: InventoryItem,
        icon_path: str,
        game: SgdbGameCandidate,
        on_clear,
    ) -> None:
        def _run(progress_cb, should_cancel):
            progress_cb("Check existing SGDB icon", 0, 1)
            report = OperationReport(total=1)
            try:
                present, confidence, matched_icon_id = self.state.is_icon_present_on_sgdb_for_game(
                    icon_path=icon_path,
                    game_id=int(game.game_id),
                    threshold=0.95,
                )
                if present:
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
                else:
                    report.succeeded = 1
            except Exception as exc:
                report.failed = 1
                report.details = [f"Presence check failed: {exc}"]
            progress_cb("Check existing SGDB icon", 1, 1)
            return report

        def _done(report: OperationReport):
            if report.failed > 0:
                QMessageBox.warning(
                    self,
                    "Upload Icon to SteamGridDB",
                    "\n".join(report.details[:8]) or "Could not verify existing SGDB icons.",
                )
                return
            if report.skipped > 0:
                QMessageBox.information(
                    self,
                    "Upload Icon to SteamGridDB",
                    "\n".join(report.details[:8]),
                )
                return
            on_clear()

        self._start_report_operation("Check existing SGDB icon", _run, _done)

    def _confirm_sgdb_upload_with_preview(
        self,
        entry: InventoryItem,
        target: SgdbGameCandidate,
        icon_path: str,
    ) -> bool:
        dialog = QDialog(self)
        dialog.setWindowTitle("Upload Icon to SteamGridDB")
        dialog.setModal(True)

        root_layout = QVBoxLayout(dialog)
        body_layout = QHBoxLayout()

        preview = QLabel()
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        preview.setFixedSize(272, 272)
        preview.setStyleSheet("QLabel { background: #101010; border: 1px solid #303030; }")
        pixmap = QIcon(icon_path).pixmap(256, 256)
        if pixmap.isNull():
            pixmap = QPixmap(icon_path)
            if not pixmap.isNull():
                pixmap = pixmap.scaled(
                    256,
                    256,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
        if pixmap.isNull():
            preview.setText("No preview")
        else:
            preview.setPixmap(pixmap)

        evidence = "; ".join(target.evidence[:3])
        details = QLabel(
            "\n".join(
                [
                    f"Folder: {entry.cleaned_name or entry.full_name}",
                    f"Target: {target.title}",
                    f"SGDB Game ID: {int(target.game_id)}",
                    f"Confidence: {float(target.confidence):.2f}",
                    f"Evidence: {evidence or 'n/a'}",
                    "",
                    "Proceed with upload now?",
                ]
            )
        )
        details.setWordWrap(True)
        details.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        body_layout.addWidget(preview, 0)
        body_layout.addWidget(details, 1)
        root_layout.addLayout(body_layout)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        upload_btn = QPushButton("Upload")
        upload_btn.setDefault(True)
        cancel_btn.clicked.connect(dialog.reject)
        upload_btn.clicked.connect(dialog.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(upload_btn)
        root_layout.addLayout(buttons)

        dialog.resize(720, 380)
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _save_icon_style_pref(self, value: str) -> None:
        self.state.set_ui_pref("icon_style", value.strip() or "none")

    def _save_bg_engine_pref(self, value: str) -> None:
        normalized = normalize_background_removal_engine(value)
        self.state.set_ui_pref("icon_bg_removal_engine", normalized)
        self._request_gpu_status_update()

    def _load_background_fill_mode_pref(self) -> str:
        return normalize_background_fill_mode(
            self.state.get_ui_pref("icon_background_fill_mode", "black")
        )

    def _save_background_fill_mode_pref(self, value: str) -> None:
        self.state.set_ui_pref(
            "icon_background_fill_mode",
            normalize_background_fill_mode(value),
        )

    def _load_background_fill_params_pref(self) -> dict[str, int]:
        raw = self.state.get_ui_pref("icon_background_fill_params", "").strip()
        if not raw:
            return normalize_background_fill_params(None)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return normalize_background_fill_params(None)
        if not isinstance(parsed, dict):
            return normalize_background_fill_params(None)
        return normalize_background_fill_params(parsed)

    def _save_background_fill_params_pref(self, payload: dict[str, object]) -> None:
        normalized = normalize_background_fill_params(payload)
        self.state.set_ui_pref(
            "icon_background_fill_params",
            json.dumps(normalized, ensure_ascii=False, sort_keys=True),
        )

    def _load_web_capture_download_dir_pref(self) -> str:
        return self.state.get_ui_pref("web_capture_download_dir", "").strip()

    def _save_web_capture_download_dir_pref(self, value: str) -> None:
        self.state.set_ui_pref("web_capture_download_dir", str(value or "").strip())

    def _load_web_capture_download_mode_pref(self) -> str:
        mode = self.state.get_ui_pref("web_capture_download_mode", "").strip().casefold()
        if mode in {"auto", "manual"}:
            return mode
        if self._load_web_capture_download_dir_pref():
            return "manual"
        return "auto"

    def _save_web_capture_download_mode_pref(self, value: str) -> None:
        normalized = str(value or "").strip().casefold()
        if normalized not in {"auto", "manual"}:
            normalized = "auto"
        self.state.set_ui_pref("web_capture_download_mode", normalized)

    def _apply_icon_result_to_entry(
        self,
        entry: InventoryItem,
        ico_path: str | None,
        desktop_ini_path: str | None,
        info_tip: str | None = None,
    ) -> None:
        target = self._find_inventory_item_by_path(entry.full_path) or entry
        target.icon_status = "valid"
        target.folder_icon_path = (
            os.path.normpath(ico_path) if ico_path else target.folder_icon_path
        )
        target.desktop_ini_path = (
            os.path.normpath(desktop_ini_path)
            if desktop_ini_path
            else target.desktop_ini_path
        )
        if info_tip is not None and info_tip.strip():
            target.info_tip = info_tip.strip()
        try:
            stat = Path(target.full_path).stat()
            target.modified_at = datetime.fromtimestamp(stat.st_mtime)
            if target.is_dir:
                self.state.remember_directory_size(
                    target.full_path,
                    target.size_bytes,
                    mtime_ns=int(stat.st_mtime_ns),
                )
        except OSError:
            return

    def _on_assign_folder_icon_selected(self) -> None:
        selected = [item for item in self._selected_right_entries() if item.is_dir]
        if not selected:
            QMessageBox.information(
                self,
                "No Folder Selected",
                "Select one or more folder rows to assign an icon.",
            )
            return

        allow_replace_existing = len(selected) == 1
        selected_count = len(selected)
        cancelled = 0
        cancel_all_requested = False
        skipped = 0
        applied = 0
        replaced = 0
        auto_upload_entry: InventoryItem | None = None
        auto_upload_icon_path = ""
        failed: list[str] = []
        creation_size_improvements = self._load_rebuild_size_improvement_defaults()
        if selected_count > 1:
            self._begin_interactive_operation("Assign icons", selected_count)
        try:
            for idx, entry in enumerate(selected, start=1):
                if selected_count > 1 and self._step_interactive_operation(
                    "Assign icons", idx - 1, selected_count
                ):
                    cancel_all_requested = True
                    break
                had_valid_icon = entry.icon_status == "valid"
                if had_valid_icon and not allow_replace_existing:
                    skipped += 1
                    continue
                query_name = entry.cleaned_name.strip() or "Game"
                resource_order, enabled_resources = self.state.sgdb_resource_preferences()
                requested_resources = [
                    value for value in resource_order if value in enabled_resources
                ]
                icon_style_pref = normalize_icon_style(
                    self.state.get_ui_pref("icon_style", "none"),
                    circular_ring=False,
                )
                bg_engine_pref = normalize_background_removal_engine(
                    self.state.get_ui_pref("icon_bg_removal_engine", "none")
                )
                border_shader_pref = self._load_border_shader_pref()
                background_fill_mode_pref = self._load_background_fill_mode_pref()
                background_fill_params_pref = self._load_background_fill_params_pref()

                try:
                    candidates = self._run_ui_pumped_call(
                        f"Search icon candidates ({idx}/{selected_count})",
                        lambda q=query_name, r=requested_resources: self.state.search_icon_candidates(
                            q,
                            q,
                            sgdb_resources=r,
                        ),
                    )
                except Exception as exc:
                    candidates = []
                    failed.append(f"{entry.full_name}: search failed ({exc})")
                current_icon_candidate = self._current_folder_icon_candidate(entry)
                if current_icon_candidate is not None:
                    candidates = [current_icon_candidate, *candidates]

                dialog = IconPickerDialog(
                    folder_name=query_name,
                    candidates=candidates,
                    preview_loader=self.state.candidate_preview,
                    image_loader=self.state.download_candidate,
                    search_callback=lambda resources, q=query_name: self.state.search_icon_candidates(
                        q,
                        q,
                        sgdb_resources=resources,
                    ),
                    initial_resource_order=resource_order,
                    initial_enabled_resources=enabled_resources,
                    resource_prefs_saver=self.state.save_sgdb_resource_preferences,
                    show_cancel_all=selected_count > 1,
                    initial_icon_style=icon_style_pref,
                    icon_style_saver=self._save_icon_style_pref,
                    initial_bg_removal_engine=bg_engine_pref,
                    bg_removal_engine_saver=self._save_bg_engine_pref,
                    initial_background_fill_mode=background_fill_mode_pref,
                    background_fill_mode_saver=self._save_background_fill_mode_pref,
                    initial_background_fill_params=background_fill_params_pref,
                    initial_border_shader=border_shader_pref,
                    border_shader_saver=self._save_border_shader_pref,
                    initial_web_download_dir=self._load_web_capture_download_dir_pref(),
                    web_download_dir_saver=self._save_web_capture_download_dir_pref,
                    initial_web_download_mode=self._load_web_capture_download_mode_pref(),
                    web_download_mode_saver=self._save_web_capture_download_mode_pref,
                    processing_controls_visible=False,
                    size_improvements=creation_size_improvements,
                    parent=self,
                )
                if dialog.exec() != dialog.DialogCode.Accepted:
                    cancelled += 1
                    if dialog.cancel_all_requested:
                        cancel_all_requested = True
                        break
                    continue
                payload = dialog.result_payload()
                self._save_background_fill_params_pref(payload.background_fill_params)
                image_bytes: bytes
                selected_candidate = payload.candidate
                candidate_provider = (
                    str(selected_candidate.provider).strip()
                    if selected_candidate is not None
                    else ""
                )
                candidate_id = (
                    str(selected_candidate.candidate_id).strip()
                    if selected_candidate is not None
                    else ""
                )
                candidate_url = (
                    str(selected_candidate.source_url).strip()
                    if selected_candidate is not None
                    else ""
                )
                applied_manual_composite = bool(
                    payload.prepared_is_final_composite
                    or payload.prepared_image_bytes is not None
                )
                icon_style = "none"
                bg_removal_engine = "none"
                bg_removal_params: dict[str, object] = {}
                text_preserve_config: dict[str, object] = {"enabled": False, "method": "none"}
                border_shader: dict[str, object] = {"enabled": False}
                background_fill_mode = "black"
                background_fill_params: dict[str, object] = {}
                if payload.prepared_is_final_composite:
                    icon_style = "none"
                    bg_removal_engine = "none"
                    bg_removal_params = {}
                    text_preserve_config = {"enabled": False, "method": "none"}
                    border_shader = {"enabled": False}
                    background_fill_mode = "black"
                    background_fill_params = {}
                else:
                    background_fill_mode = normalize_background_fill_mode(
                        payload.background_fill_mode
                    )
                    background_fill_params = normalize_background_fill_params(
                        payload.background_fill_params
                    )
                text_method_pref = str(text_preserve_config.get("method", "none") or "none")
                self.state.set_ui_pref("icon_text_extraction_method", text_method_pref)
                if payload.prepared_image_bytes is not None:
                    image_bytes = payload.prepared_image_bytes
                elif payload.source_image_bytes is not None:
                    image_bytes = payload.source_image_bytes
                elif payload.local_image_path:
                    try:
                        image_bytes = Path(payload.local_image_path).read_bytes()
                    except OSError as exc:
                        failed.append(f"{entry.full_name}: cannot read local image ({exc})")
                        continue
                elif payload.candidate is not None:
                    try:
                        image_bytes = self.state.download_candidate(payload.candidate)
                    except Exception as exc:
                        failed.append(f"{entry.full_name}: download failed ({exc})")
                        continue
                else:
                    failed.append(f"{entry.full_name}: no candidate selected")
                    continue
                info_tip_value = (payload.info_tip or "").strip()
                if not info_tip_value:
                    try:
                        auto_tip = self._run_ui_pumped_call(
                            f"Fetch InfoTip ({idx}/{selected_count})",
                            lambda: self.state.get_or_fetch_game_infotip(
                                entry.cleaned_name or entry.full_name
                            ),
                        )
                    except Exception:
                        auto_tip = None
                    if auto_tip:
                        info_tip_value = auto_tip

                try:
                    result = self._run_ui_pumped_call(
                        f"Apply icon ({idx}/{selected_count})",
                        lambda: self.state.apply_folder_icon(
                            folder_path=entry.full_path,
                            source_image=image_bytes,
                            icon_name_hint=entry.cleaned_name or entry.full_name,
                            info_tip=info_tip_value,
                            icon_style=icon_style,
                            bg_removal_engine=bg_removal_engine,
                            bg_removal_params=bg_removal_params,
                            text_preserve_config=text_preserve_config,
                            border_shader=border_shader,
                            background_fill_mode=background_fill_mode,
                            background_fill_params=background_fill_params,
                            size_improvements=creation_size_improvements,
                        ),
                    )
                except Exception as exc:
                    failed.append(f"{entry.full_name}: apply failed ({exc})")
                    continue
                if result.status != "applied":
                    failed.append(f"{entry.full_name}: {result.message}")
                    continue
                applied += 1
                source_kind = "unknown"
                source_provider = ""
                if candidate_provider == "SteamGridDB":
                    if applied_manual_composite:
                        source_kind = "sgdb_modified"
                        source_provider = "Derived"
                    else:
                        source_kind = "sgdb_raw"
                        source_provider = "SteamGridDB"
                elif candidate_provider:
                    source_kind = "web"
                    source_provider = "Internet"
                else:
                    source_kind = "web"
                    source_provider = "Internet"
                try:
                    self._apply_icon_result_to_entry(
                        entry,
                        result.ico_path,
                        result.desktop_ini_path,
                        info_tip_value,
                    )
                    if result.ico_path:
                        icon_fingerprint = self.state.icon_fingerprint256(result.ico_path)
                    else:
                        icon_fingerprint = self.state.processed_source_fingerprint256(image_bytes)
                    self.state.record_assigned_icon_source(
                        folder_path=entry.full_path,
                        source_kind=source_kind,
                        source_provider=source_provider,
                        source_candidate_id=candidate_id,
                        source_url=candidate_url,
                        source_fingerprint256=icon_fingerprint,
                        source_confidence=1.0 if source_kind == "sgdb_raw" else 0.0,
                    )
                    if (
                        selected_count == 1
                        and source_kind not in {"sgdb_raw", "sgdb_modified"}
                    ):
                        auto_upload_entry = entry
                        auto_upload_icon_path = str(result.ico_path or "").strip()
                except Exception as exc:
                    failed.append(f"{entry.full_name}: post-apply update failed ({exc})")
                if had_valid_icon:
                    replaced += 1
        finally:
            if selected_count > 1:
                self._end_interactive_operation()

        if applied > 0:
            self._folder_icon_cache.clear()
            self._folder_icon_preview_cache.clear()
            try:
                self._populate_right(self.inventory)
            except Exception as exc:
                failed.append(f"UI refresh failed ({exc})")
                self._set_refresh_needed(True)
        if selected_count == 1:
            if failed:
                QMessageBox.warning(
                    self,
                    "Assign Folder Icon",
                    "\n".join(failed[:8]),
                )
                return
            if auto_upload_entry is not None and auto_upload_icon_path:
                settings = self.state.icon_search_settings()
                if settings.steamgriddb_enabled and settings.steamgriddb_api_key.strip():
                    QTimer.singleShot(
                        0,
                        lambda e=auto_upload_entry, p=auto_upload_icon_path: self._start_upload_icon_to_steamgriddb_flow(
                            entry=e,
                            icon_path=p,
                            title="Upload Icon to SteamGridDB",
                            show_already_sgdb_message=False,
                        ),
                    )
            return
        if (
            applied == 0
            and replaced == 0
            and skipped == 0
            and not failed
            and cancelled > 0
        ):
            return
        lines = [f"Applied: {applied}"]
        if replaced:
            lines.append(f"Replaced existing: {replaced}")
        lines.append(f"Skipped existing: {skipped}")
        if cancelled:
            lines.append(f"Cancelled: {cancelled}")
        if cancel_all_requested:
            lines.append("Run cancelled by user.")
        if failed:
            lines.append(f"Failed: {len(failed)}")
            lines.append("")
            lines.extend(failed[:8])
        if failed:
            QMessageBox.warning(self, "Assign Folder Icon", "\n".join(lines))
            return
        only_success = applied > 0 and replaced == 0 and skipped == 0 and cancelled == 0 and not cancel_all_requested
        if only_success:
            self._show_success_popup("Assign Folder Icon", "\n".join(lines))
        else:
            QMessageBox.information(self, "Assign Folder Icon", "\n".join(lines))

    def _on_open_icon_converter(self) -> None:
        if self._icon_converter_dialog is not None:
            self._icon_converter_dialog.show()
            self._icon_converter_dialog.raise_()
            self._icon_converter_dialog.activateWindow()
            return
        icon_style_pref = normalize_icon_style(
            self.state.get_ui_pref("icon_style", "none"),
            circular_ring=False,
        )
        bg_engine_pref = normalize_background_removal_engine(
            self.state.get_ui_pref("icon_bg_removal_engine", "none")
        )
        border_shader_pref = self._load_border_shader_pref()
        background_fill_mode_pref = self._load_background_fill_mode_pref()
        background_fill_params_pref = self._load_background_fill_params_pref()
        dialog = IconConverterDialog(
            initial_icon_style=icon_style_pref,
            icon_style_saver=self._save_icon_style_pref,
            initial_bg_removal_engine=bg_engine_pref,
            bg_removal_engine_saver=self._save_bg_engine_pref,
            initial_background_fill_mode=background_fill_mode_pref,
            background_fill_mode_saver=self._save_background_fill_mode_pref,
            initial_background_fill_params=background_fill_params_pref,
            initial_border_shader=border_shader_pref,
            border_shader_saver=self._save_border_shader_pref,
            processing_controls_visible=False,
            size_improvements=self._load_rebuild_size_improvement_defaults(),
            parent=self,
        )
        dialog.finished.connect(self._on_icon_converter_closed)
        self._icon_converter_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_icon_converter_closed(self, _result: int) -> None:
        self._icon_converter_dialog = None

    def _on_open_template_prep(self) -> None:
        if self._template_prep_dialog is not None:
            self._template_prep_dialog.show()
            self._template_prep_dialog.raise_()
            self._template_prep_dialog.activateWindow()
            return
        dialog = TemplatePrepDialog(parent=self)
        dialog.finished.connect(self._on_template_prep_closed)
        self._template_prep_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_template_prep_closed(self, _result: int) -> None:
        self._template_prep_dialog = None

    def _on_open_template_transparency(self) -> None:
        if self._template_transparency_dialog is not None:
            self._template_transparency_dialog.show()
            self._template_transparency_dialog.raise_()
            self._template_transparency_dialog.activateWindow()
            return
        dialog = TemplateTransparencyDialog(parent=self)
        dialog.finished.connect(self._on_template_transparency_closed)
        self._template_transparency_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_template_transparency_closed(self, _result: int) -> None:
        self._template_transparency_dialog = None

    def _on_repair_absolute_icon_paths(self) -> None:
        report = self.state.repair_absolute_icon_paths()
        self.refresh_all()
        lines = [
            f"Repaired: {report.succeeded}",
            f"Failed: {report.failed}",
            f"Unchanged: {report.skipped}",
        ]
        if report.details:
            lines.append("")
            lines.extend(report.details[:12])
        if int(report.failed) > 0:
            QMessageBox.warning(self, "Repair Icon Paths", "\n".join(lines))
        else:
            self._show_success_popup("Repair Icon Paths", "\n".join(lines))

    def _load_border_shader_pref(self) -> dict[str, object]:
        raw = self.state.get_ui_pref("icon_border_shader", "").strip()
        if not raw:
            return border_shader_to_dict(None)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return border_shader_to_dict(None)
        if not isinstance(parsed, dict):
            return border_shader_to_dict(None)
        return border_shader_to_dict(normalize_border_shader_config(parsed))

    def _save_border_shader_pref(self, payload: dict[str, object]) -> None:
        normalized = border_shader_to_dict(normalize_border_shader_config(payload))
        self.state.set_ui_pref(
            "icon_border_shader",
            json.dumps(normalized, ensure_ascii=False, sort_keys=True),
        )

