from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path

from PySide6.QtWidgets import QMessageBox

from gamemanager.models import InventoryItem
from gamemanager.services.background_removal import normalize_background_removal_engine
from gamemanager.services.icon_pipeline import (
    border_shader_to_dict,
    normalize_border_shader_config,
    normalize_icon_style,
)
from gamemanager.ui.dialogs import (
    IconConverterDialog,
    IconPickerDialog,
    TemplatePrepDialog,
    TemplateTransparencyDialog,
)


class MainWindowIconOpsMixin:
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
                icon_style = payload.icon_style
                bg_removal_engine = payload.bg_removal_engine
                bg_removal_params = dict(payload.bg_removal_params or {})
                text_preserve_config = dict(payload.text_preserve_config or {})
                border_shader = border_shader_to_dict(payload.border_shader)
                if payload.prepared_is_final_composite:
                    icon_style = "none"
                    bg_removal_engine = "none"
                    bg_removal_params = {}
                    text_preserve_config = {"enabled": False, "strength": 45, "feather": 1}
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

