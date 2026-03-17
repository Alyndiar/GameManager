from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gamemanager.app_state import AppState
from gamemanager.models import InventoryItem, RootDisplayInfo
from gamemanager.ui.dialogs import CleanupPreviewDialog, MovePreviewDialog, TagReviewDialog


LEFT_SORT_OPTIONS = {
    "Source label": "source_label",
    "Drive name": "drive_name",
    "Free space": "free_space",
}

LEFT_LABEL_OPTIONS = {
    "Letter/mountpoint": "source",
    "Drive name": "name",
    "Both": "both",
}


def _format_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.setWindowTitle("Game Backup Manager")
        self.resize(1360, 840)
        self.root_infos: list[RootDisplayInfo] = []
        self.inventory: list[InventoryItem] = []

        root = QWidget(self)
        root_layout = QVBoxLayout(root)

        top_controls = QHBoxLayout()
        self.add_root_btn = QPushButton("Add Root")
        self.remove_root_btn = QPushButton("Remove Selected Root")
        self.refresh_btn = QPushButton("Refresh")
        self.cleanup_btn = QPushButton("Cleanup Names (Disk)")
        self.tags_btn = QPushButton("Find Tags")
        self.move_btn = QPushButton("Move ISO/Archives")
        top_controls.addWidget(self.add_root_btn)
        top_controls.addWidget(self.remove_root_btn)
        top_controls.addWidget(self.refresh_btn)
        top_controls.addWidget(self.cleanup_btn)
        top_controls.addWidget(self.tags_btn)
        top_controls.addWidget(self.move_btn)
        top_controls.addStretch(1)
        root_layout.addLayout(top_controls)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, root)
        root_layout.addWidget(self.splitter, 1)

        left_panel = QWidget(self.splitter)
        left_layout = QVBoxLayout(left_panel)
        left_controls = QHBoxLayout()
        left_controls.addWidget(QLabel("Sort by:"))
        self.left_sort_combo = QComboBox(left_panel)
        self.left_sort_combo.addItems(LEFT_SORT_OPTIONS.keys())
        left_controls.addWidget(self.left_sort_combo)
        left_controls.addWidget(QLabel("Display:"))
        self.left_label_combo = QComboBox(left_panel)
        self.left_label_combo.addItems(LEFT_LABEL_OPTIONS.keys())
        left_controls.addWidget(self.left_label_combo)
        left_controls.addStretch(1)
        left_layout.addLayout(left_controls)

        self.left_table = QTableWidget(0, 1, left_panel)
        self.left_table.setHorizontalHeaderLabels(["Root Storage"])
        self.left_table.setWordWrap(True)
        self.left_table.verticalHeader().setVisible(False)
        self.left_table.horizontalHeader().setStretchLastSection(True)
        self.left_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.left_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.left_table.setShowGrid(True)
        left_layout.addWidget(self.left_table, 1)

        right_panel = QWidget(self.splitter)
        right_layout = QVBoxLayout(right_panel)
        self.right_table = QTableWidget(0, 5, right_panel)
        self.right_table.setHorizontalHeaderLabels(
            ["Name", "Created", "Modified", "Size", "Source"]
        )
        self.right_table.verticalHeader().setVisible(False)
        self.right_table.horizontalHeader().setStretchLastSection(True)
        self.right_table.setWordWrap(True)
        self.right_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.right_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        right_layout.addWidget(self.right_table, 1)

        self.splitter.addWidget(left_panel)
        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([430, 900])

        self.setCentralWidget(root)
        self.statusBar().showMessage("Ready")

        self._wire_events()
        self._load_prefs()
        self.refresh_all()

    def _wire_events(self) -> None:
        self.add_root_btn.clicked.connect(self._on_add_root)
        self.remove_root_btn.clicked.connect(self._on_remove_root)
        self.refresh_btn.clicked.connect(self.refresh_all)
        self.cleanup_btn.clicked.connect(self._on_cleanup)
        self.tags_btn.clicked.connect(self._on_find_tags)
        self.move_btn.clicked.connect(self._on_move_archives)
        self.left_sort_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_label_combo.currentTextChanged.connect(self._on_left_pref_changed)

    def _load_prefs(self) -> None:
        sort_pref = self.state.get_ui_pref("left_sort", "source_label")
        label_pref = self.state.get_ui_pref("left_label_mode", "source")

        for text, value in LEFT_SORT_OPTIONS.items():
            if value == sort_pref:
                self.left_sort_combo.setCurrentText(text)
                break
        for text, value in LEFT_LABEL_OPTIONS.items():
            if value == label_pref:
                self.left_label_combo.setCurrentText(text)
                break

    def _on_left_pref_changed(self) -> None:
        sort_key = LEFT_SORT_OPTIONS[self.left_sort_combo.currentText()]
        label_mode = LEFT_LABEL_OPTIONS[self.left_label_combo.currentText()]
        self.state.set_ui_pref("left_sort", sort_key)
        self.state.set_ui_pref("left_label_mode", label_mode)
        self._populate_left(self.root_infos)

    def _selected_root_id(self) -> int | None:
        rows = self.left_table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        item = self.left_table.item(row, 0)
        if item is None:
            return None
        return item.data(Qt.ItemDataRole.UserRole)

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
        self.refresh_all()

    def _on_remove_root(self) -> None:
        root_id = self._selected_root_id()
        if root_id is None:
            QMessageBox.information(self, "No Selection", "Select a root row first.")
            return
        self.state.remove_root(root_id)
        self.refresh_all()

    def refresh_all(self) -> None:
        self.root_infos, self.inventory = self.state.refresh()
        self._populate_left(self.root_infos)
        self._populate_right(self.inventory)
        self.statusBar().showMessage(
            f"Loaded {len(self.root_infos)} roots and {len(self.inventory)} root entries"
        )

    def _sorted_roots(self, roots: list[RootDisplayInfo]) -> list[RootDisplayInfo]:
        sort_key = LEFT_SORT_OPTIONS[self.left_sort_combo.currentText()]
        if sort_key == "source_label":
            return sorted(roots, key=lambda x: x.source_label.casefold())
        if sort_key == "drive_name":
            return sorted(roots, key=lambda x: x.drive_name.casefold())
        return sorted(roots, key=lambda x: x.free_space_bytes, reverse=True)

    def _root_text(self, info: RootDisplayInfo) -> str:
        mode = LEFT_LABEL_OPTIONS[self.left_label_combo.currentText()]
        free_line = f"Free: {_format_bytes(info.free_space_bytes)}"
        root_line = f"Root: {info.root_path}"
        if mode == "source":
            return f"{info.source_label}\n{free_line}\n{root_line}"
        if mode == "name":
            return f"{info.drive_name}\n{free_line}\n{root_line}"
        return f"{info.source_label} | {info.drive_name}\n{free_line}\n{root_line}"

    def _populate_left(self, roots: list[RootDisplayInfo]) -> None:
        ordered = self._sorted_roots(roots)
        self.left_table.setRowCount(len(ordered))
        for row, info in enumerate(ordered):
            text = self._root_text(info)
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, info.root_id)
            item.setToolTip(
                f"Source: {info.source_label}\nDrive: {info.drive_name}\nMountpoint: {info.mountpoint}"
            )
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.left_table.setItem(row, 0, item)
        self.left_table.resizeRowsToContents()

    def _populate_right(self, items: list[InventoryItem]) -> None:
        self.right_table.setRowCount(len(items))
        for row, entry in enumerate(items):
            self.right_table.setItem(row, 0, QTableWidgetItem(entry.full_name))
            self.right_table.setItem(
                row, 1, QTableWidgetItem(entry.created_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.right_table.setItem(
                row, 2, QTableWidgetItem(entry.modified_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.right_table.setItem(row, 3, QTableWidgetItem(_format_bytes(entry.size_bytes)))
            self.right_table.setItem(row, 4, QTableWidgetItem(entry.source_label))
            self.right_table.item(row, 0).setToolTip(
                f"Path: {entry.full_path}\nCleaned: {entry.cleaned_name}"
            )
        self.right_table.resizeColumnsToContents()

    def _on_find_tags(self) -> None:
        if not self.inventory:
            QMessageBox.information(self, "No Entries", "No inventory entries to scan for tags.")
            return
        candidates = self.state.find_tag_candidates(self.inventory)
        if not candidates:
            QMessageBox.information(
                self,
                "No Candidates",
                "No suffix tag candidates found (non-tags are already suppressed).",
            )
            return
        dialog = TagReviewDialog(
            candidates=candidates,
            approved_tags=self.state.approved_tags(),
            non_tags=self.state.non_tags(),
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload()
        self.state.save_tag_decisions(payload.decisions, payload.display_map)
        self.refresh_all()
        self.statusBar().showMessage("Tag decisions saved and inventory refreshed.")

    def _on_cleanup(self) -> None:
        plan = self.state.build_cleanup_plan()
        if not plan:
            QMessageBox.information(self, "Nothing to Rename", "No root-level entries found.")
            return
        dialog = CleanupPreviewDialog(plan, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        safe_items = dialog.safe_items()
        if not safe_items:
            QMessageBox.information(
                self, "No Safe Renames", "No non-conflicting rename items to apply."
            )
            return
        report = self.state.execute_cleanup_plan(safe_items)
        conflict_count = len([x for x in plan if x.status == "conflict"])
        lines = [
            f"Attempted: {report.total}",
            f"Succeeded: {report.succeeded}",
            f"Failed: {report.failed}",
            f"Manual rename conflicts: {conflict_count}",
        ]
        if report.details:
            lines.append("")
            lines.extend(report.details[:8])
        QMessageBox.information(self, "Cleanup Result", "\n".join(lines))
        self.refresh_all()

    def _on_move_archives(self) -> None:
        plan = self.state.build_archive_move_plan({".iso", ".zip", ".rar", ".7z"})
        if not plan:
            QMessageBox.information(
                self, "Nothing to Move", "No root-level ISO/ZIP/RAR/7Z files found."
            )
            return
        dialog = MovePreviewDialog(plan, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        report = self.state.execute_archive_move_plan(dialog.applied_items())
        lines = [
            f"Attempted: {report.total}",
            f"Succeeded: {report.succeeded}",
            f"Skipped: {report.skipped}",
            f"Conflicts: {report.conflicts}",
            f"Failed: {report.failed}",
        ]
        if report.details:
            lines.append("")
            lines.extend(report.details[:8])
        QMessageBox.information(self, "Move Result", "\n".join(lines))
        self.refresh_all()
