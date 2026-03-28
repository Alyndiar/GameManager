from __future__ import annotations

import copy
from datetime import datetime
import json
import os
from pathlib import Path

from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox

from gamemanager.models import IconCandidate, IconRebuildEntry, InventoryItem
from gamemanager.services.background_removal import normalize_background_removal_engine
from gamemanager.services.icon_pipeline import (
    border_shader_to_dict,
    default_icon_size_improvements,
    normalize_border_shader_config,
    normalize_icon_size_improvements,
    normalize_icon_style,
)
from gamemanager.ui.dialogs import (
    IconConverterDialog,
    IconPickerDialog,
    IconRebuildPreviewItem,
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
                QMessageBox.information(self, "Rebuild Existing Icons", "\n".join(lines))

        self._start_report_operation(
            "Rebuild Existing Icons",
            _run,
            _done,
        )

    def _save_icon_style_pref(self, value: str) -> None:
        self.state.set_ui_pref("icon_style", value.strip() or "none")

    def _save_bg_engine_pref(self, value: str) -> None:
        normalized = normalize_background_removal_engine(value)
        self.state.set_ui_pref("icon_bg_removal_engine", normalized)
        self._request_gpu_status_update()

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

                try:
                    candidates = self.state.search_icon_candidates(
                        query_name, query_name, sgdb_resources=requested_resources
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
                image_bytes: bytes
                icon_style = "none"
                bg_removal_engine = "none"
                bg_removal_params: dict[str, object] = {}
                text_preserve_config: dict[str, object] = {"enabled": False, "method": "none"}
                border_shader: dict[str, object] = {"enabled": False}
                if payload.prepared_is_final_composite:
                    icon_style = "none"
                    bg_removal_engine = "none"
                    bg_removal_params = {}
                    text_preserve_config = {"enabled": False, "method": "none"}
                    border_shader = {"enabled": False}
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
                        auto_tip = self.state.get_or_fetch_game_infotip(
                            entry.cleaned_name or entry.full_name
                        )
                    except Exception:
                        auto_tip = None
                    if auto_tip:
                        info_tip_value = auto_tip

                try:
                    result = self.state.apply_folder_icon(
                        folder_path=entry.full_path,
                        source_image=image_bytes,
                        icon_name_hint=entry.cleaned_name or entry.full_name,
                        info_tip=info_tip_value,
                        icon_style=icon_style,
                        bg_removal_engine=bg_removal_engine,
                        bg_removal_params=bg_removal_params,
                        text_preserve_config=text_preserve_config,
                        border_shader=border_shader,
                        size_improvements=creation_size_improvements,
                    )
                except Exception as exc:
                    failed.append(f"{entry.full_name}: apply failed ({exc})")
                    continue
                if result.status != "applied":
                    failed.append(f"{entry.full_name}: {result.message}")
                    continue
                applied += 1
                try:
                    self._apply_icon_result_to_entry(
                        entry,
                        result.ico_path,
                        result.desktop_ini_path,
                        info_tip_value,
                    )
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
        dialog = IconConverterDialog(
            initial_icon_style=icon_style_pref,
            icon_style_saver=self._save_icon_style_pref,
            initial_bg_removal_engine=bg_engine_pref,
            bg_removal_engine_saver=self._save_bg_engine_pref,
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
        QMessageBox.information(self, "Repair Icon Paths", "\n".join(lines))

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

