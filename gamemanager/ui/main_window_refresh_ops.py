from __future__ import annotations

import os

from PySide6.QtCore import QThread, QTimer
from PySide6.QtWidgets import QFileDialog, QMessageBox


class MainWindowRefreshOpsMixin:
    def _on_add_root(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select Root Folder")
        if not selected:
            return
        try:
            result = self.state.add_root(selected)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot Add Root", str(exc))
            return
        except OSError as exc:
            QMessageBox.warning(self, "Cannot Add Root", f"Filesystem error: {exc}")
            return
        if result == "duplicate":
            QMessageBox.information(
                self,
                "Already Added",
                f"Root is already in the list:\n{selected}",
            )
            return
        self.root_infos = self.state.refresh_roots_only()
        self._loaded_roots_count = len(self.root_infos)
        self._populate_left(self.root_infos)
        self._mark_refresh_needed(True)
        self._update_counts_status()

    def _on_remove_root(self) -> None:
        root_id = self._selected_root_id()
        if root_id is None:
            QMessageBox.information(self, "No Selection", "Select a root row first.")
            return
        self.state.remove_root(root_id)
        self.refresh_all()

    def refresh_all(self) -> None:
        if self._refresh_in_progress:
            self._refresh_queued = True
            self._set_operation_progress("Refresh queued", 1, 1)
            return

        self._refresh_in_progress = True
        self._set_refresh_busy_ui(True)
        self._set_operation_progress("Starting refresh", 0, 1)
        thread = QThread(self)
        worker = self._refresh_worker_cls(self.state)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_refresh_progress)
        worker.completed.connect(self._on_refresh_completed)
        worker.canceled.connect(self._on_refresh_canceled)
        worker.failed.connect(self._on_refresh_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_refresh_finished)
        self._refresh_thread = thread
        self._refresh_worker = worker
        thread.start()

    def _set_refresh_busy_ui(self, busy: bool) -> None:
        self.refresh_btn.setEnabled(not busy)
        self.refresh_btn.setText("Refreshing..." if busy else "Refresh")
        self._update_cancel_button_state()

    def _on_refresh_progress(self, stage: str, current: int, total: int) -> None:
        self._set_operation_progress(stage, current, total)

    def _on_refresh_completed(self, root_infos: object, inventory: object) -> None:
        if not self._initial_refresh_done:
            self._initial_refresh_done = True
        self.root_infos = list(root_infos) if isinstance(root_infos, list) else []
        self.inventory = list(inventory) if isinstance(inventory, list) else []
        self._prune_icon_caches()
        self._hide_right_icon_hover_preview()
        self._loaded_roots_count = len(self.root_infos)
        self._loaded_entries_count = len(self.inventory)
        self._populate_left(self.root_infos)
        self._populate_right(self.inventory)
        self._mark_refresh_needed(False)
        self._update_counts_status()
        self._set_operation_progress("Refresh complete", 1, 1)
        self._schedule_startup_prewarm_if_ready()
        self._start_info_tip_backfill_if_needed()

    def _on_refresh_failed(self, message: str) -> None:
        err = message.strip() or "Unknown refresh error."
        QMessageBox.warning(self, "Refresh Failed", err)
        self._set_operation_progress("Refresh failed", 1, 1)

    def _on_refresh_canceled(self, message: str) -> None:
        msg = message.strip() or "Refresh canceled."
        self._set_operation_progress(msg, 1, 1)

    def _on_refresh_finished(self) -> None:
        self._refresh_thread = None
        self._refresh_worker = None
        self._refresh_in_progress = False
        self._set_refresh_busy_ui(False)
        if self._refresh_queued:
            self._refresh_queued = False
            QTimer.singleShot(0, self.refresh_all)
            return
        if (
            not self._operation_in_progress
            and not self._interactive_operation_active
            and not self._teracopy_processes
        ):
            self._clear_operation_progress()

    def _start_info_tip_backfill_if_needed(self) -> None:
        if self._infotip_backfill_in_progress:
            return
        if self.state.get_ui_pref("icon_infotip_backfill_done_v1", "0").strip() == "1":
            return
        candidates: list[tuple[str, str]] = []
        for entry in self.inventory:
            if not entry.is_dir:
                continue
            if entry.icon_status != "valid":
                continue
            if (entry.info_tip or "").strip():
                continue
            cleaned = (entry.cleaned_name or entry.full_name).strip()
            if not cleaned:
                continue
            candidates.append((entry.full_path, cleaned))
        if not candidates:
            self.state.set_ui_pref("icon_infotip_backfill_done_v1", "1")
            return
        thread = QThread(self)
        worker = self._infotip_backfill_worker_cls(self.state, candidates)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_info_tip_backfill_progress)
        worker.completed.connect(self._on_info_tip_backfill_completed)
        worker.failed.connect(self._on_info_tip_backfill_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_info_tip_backfill_finished)
        self._infotip_backfill_in_progress = True
        self._infotip_backfill_thread = thread
        self._infotip_backfill_worker = worker
        thread.start()

    def _on_info_tip_backfill_progress(self, stage: str, current: int, total: int) -> None:
        self._set_background_progress(stage, current, total)

    def _on_info_tip_backfill_completed(self, payload: object) -> None:
        data = payload if isinstance(payload, dict) else {}
        tip_map_raw = data.get("tips_by_path", {}) if isinstance(data, dict) else {}
        tip_map = (
            {
                os.path.normpath(str(path)): str(text).strip()
                for path, text in tip_map_raw.items()
                if str(text).strip()
            }
            if isinstance(tip_map_raw, dict)
            else {}
        )
        if tip_map:
            for entry in self.inventory:
                key = os.path.normpath(entry.full_path)
                tip = tip_map.get(key)
                if tip:
                    entry.info_tip = tip
            self._populate_right(self.inventory)
        updated = int(data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = int(data.get("failed", 0)) if isinstance(data, dict) else 0
        attempted = int(data.get("attempted", 0)) if isinstance(data, dict) else 0
        self._set_background_progress(
            f"InfoTip backfill done (updated {updated}, failed {failed})",
            attempted,
            attempted,
        )
        self.state.set_ui_pref("icon_infotip_backfill_done_v1", "1")
        QTimer.singleShot(2000, self._clear_background_progress)

    def _on_info_tip_backfill_failed(self, message: str) -> None:
        err = message.strip() or "InfoTip backfill failed"
        self._set_background_progress(err, 1, 1)
        self.state.set_ui_pref("icon_infotip_backfill_done_v1", "1")
        QTimer.singleShot(2000, self._clear_background_progress)

    def _on_info_tip_backfill_finished(self) -> None:
        self._infotip_backfill_in_progress = False
        self._infotip_backfill_thread = None
        self._infotip_backfill_worker = None

    def _mark_refresh_needed(self, needed: bool) -> None:
        self._refresh_needed = needed
        if needed:
            self.refresh_btn.setStyleSheet(
                "QPushButton { background-color: #6b1d1d; color: #ffffff; font-weight: 600; }"
            )
            self.refresh_btn.setToolTip("Manual refresh required.\nShortcut: Ctrl+R")
            return
        self.refresh_btn.setStyleSheet("")
        self.refresh_btn.setToolTip("Refresh\nShortcut: Ctrl+R")
