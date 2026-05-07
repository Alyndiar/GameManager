from __future__ import annotations

import os

from PySide6.QtWidgets import QInputDialog, QMessageBox

from gamemanager.models import InventoryItem, OperationReport
from gamemanager.services.cancellation import OperationCancelled


class MainWindowInfoTipOpsMixin:
    def _run_infotip_refresh_operation(
        self,
        targets: list[tuple[str, str]],
        *,
        title: str,
        show_summary: bool = True,
    ) -> bool:
        normalized_targets = [
            (os.path.normpath(path), (name or "").strip())
            for path, name in targets
            if str(path).strip() and str(name).strip()
        ]
        if not normalized_targets:
            return False

        def _run(progress_cb, should_cancel):
            report = OperationReport(total=len(normalized_targets))
            total = len(normalized_targets)
            for idx, (folder_path, cleaned_name) in enumerate(normalized_targets, start=1):
                if should_cancel():
                    raise OperationCancelled("InfoTip refresh canceled.")
                progress_cb("Refresh InfoTip", idx - 1, total)
                try:
                    updated, tip = self.state.ensure_folder_info_tip(
                        folder_path,
                        cleaned_name,
                        overwrite_existing=True,
                        force_refresh=True,
                    )
                except Exception as exc:
                    report.failed += 1
                    report.details.append(f"{cleaned_name}: {exc}")
                    continue
                if tip:
                    if updated:
                        report.succeeded += 1
                    else:
                        report.skipped += 1
                    continue
                report.failed += 1
                report.details.append(f"{cleaned_name}: no description found")
            progress_cb("Refresh InfoTip", total, total)
            return report

        def _done(report: OperationReport) -> None:
            if show_summary:
                lines = [
                    f"Attempted: {report.total}",
                    f"Updated: {report.succeeded}",
                    f"Unchanged: {report.skipped}",
                    f"Failed: {report.failed}",
                ]
                if report.details:
                    lines.append("")
                    lines.extend(report.details[:8])
                if int(report.failed) > 0:
                    QMessageBox.warning(self, "InfoTip Refresh", "\n".join(lines))
                else:
                    self._show_success_popup("InfoTip Refresh", "\n".join(lines))
            self.refresh_all()

        return self._start_report_operation(title, _run, _done)

    def _on_refresh_selected_infotips(self) -> None:
        selected = [item for item in self._selected_right_entries() if item.is_dir]
        if not selected:
            QMessageBox.information(
                self,
                "InfoTip Refresh",
                "Select at least one game folder first.",
            )
            return
        targets = [
            (
                entry.full_path,
                (entry.cleaned_name or entry.full_name).strip(),
            )
            for entry in selected
            if entry.icon_status == "valid"
        ]
        if not targets:
            QMessageBox.information(
                self,
                "InfoTip Refresh",
                "Selected entries do not have folder icons yet.",
            )
            return
        self._run_infotip_refresh_operation(
            targets,
            title="Refresh InfoTips",
            show_summary=True,
        )

    def _refresh_visible_entry_tooltip(self, full_path: str) -> None:
        normalized = os.path.normpath(full_path)
        root_info_by_id = {info.root_id: info for info in self.root_infos}
        for row, entry in enumerate(self._visible_right_items):
            if os.path.normpath(entry.full_path) != normalized:
                continue
            tooltip = self._entry_tooltip_text(
                entry,
                self._source_for_item(entry, root_info_by_id),
            )
            for col in range(self.right_table.columnCount()):
                cell = self.right_table.item(row, col)
                if cell is not None:
                    cell.setToolTip(tooltip)
            tile = self.right_icon_list.item(row)
            if tile is not None:
                tile.setToolTip(tooltip)
            break

    def _on_edit_selected_infotip(self) -> None:
        selected = [item for item in self._selected_right_entries() if item.is_dir]
        if len(selected) != 1:
            QMessageBox.information(
                self,
                "Manual InfoTip Entry",
                "Select exactly one game folder to edit its InfoTip.",
            )
            return
        entry = selected[0]
        if entry.icon_status != "valid":
            QMessageBox.information(
                self,
                "Manual InfoTip Entry",
                "The selected game does not have a folder icon yet.",
            )
            return
        current_tip = (entry.info_tip or "").strip()
        new_tip, ok = QInputDialog.getMultiLineText(
            self,
            "Manual InfoTip Entry",
            "InfoTip:",
            current_tip,
        )
        if not ok:
            return
        normalized_tip = new_tip.strip()
        if not normalized_tip:
            QMessageBox.warning(
                self,
                "Manual InfoTip Entry",
                "InfoTip cannot be empty.",
            )
            return
        cleaned = (entry.cleaned_name or entry.full_name).strip()
        try:
            changed = self.state.set_manual_folder_info_tip(
                entry.full_path,
                cleaned,
                normalized_tip,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Manual InfoTip Entry",
                f"Could not update InfoTip:\n{exc}",
            )
            return
        if not changed:
            QMessageBox.warning(
                self,
                "Manual InfoTip Entry",
                "Could not update InfoTip on disk.",
            )
            return
        target = self._find_inventory_item_by_path(entry.full_path) or entry
        target.info_tip = normalized_tip
        self._refresh_visible_entry_tooltip(entry.full_path)
        self._update_counts_status()

    def _entry_tooltip_text(self, entry: InventoryItem, row_source: str) -> str:
        lines = [
            f"Name: {entry.full_name}",
            f"Cleaned: {entry.cleaned_name}",
            f"Path: {entry.full_path}",
            f"Source: {row_source}",
        ]
        if entry.owned_stores:
            lines.append(f"Owned Stores: {', '.join(entry.owned_stores)}")
        if entry.is_dir:
            metadata = self.state.read_folder_icon_metadata(entry.full_path)
            source_kind = str(metadata.get("SourceKind", "")).strip()
            source_provider = str(metadata.get("SourceProvider", "")).strip()
            source_game_id = str(metadata.get("SourceGameId", "")).strip()
            if source_kind:
                lines.append(f"Icon Source Kind: {source_kind}")
            if source_provider:
                lines.append(f"Icon Source Provider: {source_provider}")
            if source_game_id:
                lines.append(f"Icon Source Game ID: {source_game_id}")
        tip = (entry.info_tip or "").strip()
        if tip:
            lines.append(f"InfoTip: {tip}")
        return "\n".join(lines)

    def _update_selected_info_box(self) -> None:
        selected = self._selected_right_entries()
        if not selected:
            self.selected_info_label.setText(
                "Select a game to view its one-line description."
            )
            return
        entry = selected[0]
        tip = (entry.info_tip or "").strip()
        if tip:
            self.selected_info_label.setText(tip)
            return
        self.selected_info_label.setText("No description available for this game yet.")

