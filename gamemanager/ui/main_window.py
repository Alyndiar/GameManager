from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import os
import shutil
import subprocess
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from gamemanager.app_state import AppState
from gamemanager.models import InventoryItem, RootDisplayInfo
from gamemanager.services.sorting import natural_key
from gamemanager.services.storage import mountpoint_sort_key
from gamemanager.ui.dialogs import (
    CleanupPreviewDialog,
    DeleteGroupDialog,
    MovePreviewDialog,
    TagReviewDialog,
)


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

RIGHT_COLUMNS = [
    ("full_name", "Name"),
    ("cleaned_name", "Cleaned Name"),
    ("modified_at", "Modified"),
    ("created_at", "Created"),
    ("size_bytes", "Size"),
    ("source", "Source"),
]

DEFAULT_RIGHT_SORT_CHAIN: list[tuple[str, bool]] = [
    ("cleaned_name", True),
    ("modified_at", False),
    ("full_name", True),
    ("created_at", False),
    ("size_bytes", False),
    ("source", True),
]


def _source_display_text(mode: str, source_label: str, drive_name: str) -> str:
    source = source_label.strip()
    drive = drive_name.strip()
    if mode == "source":
        return source
    if mode == "name":
        return drive
    if not source:
        return drive
    if not drive:
        return source
    if source.casefold() == drive.casefold():
        return source
    return f"{source} | {drive}"


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


def _format_size_and_free(total_size_bytes: int, free_size_bytes: int) -> str:
    total_gb_rounded = int((total_size_bytes / (1024**3)) + 0.5)
    free_gb = free_size_bytes / (1024**3)
    return f"Size : {total_gb_rounded} GB    Free : {free_gb:.2f} GB"


def _column_index_for_field(field_name: str) -> int:
    for idx, (field, _) in enumerate(RIGHT_COLUMNS):
        if field == field_name:
            return idx
    return 1


def _build_sort_chain(primary_field: str, primary_ascending: bool) -> list[tuple[str, bool]]:
    ordered = [(primary_field, primary_ascending)] + DEFAULT_RIGHT_SORT_CHAIN
    seen: set[str] = set()
    chain: list[tuple[str, bool]] = []
    for field, asc in ordered:
        if field in seen:
            continue
        seen.add(field)
        chain.append((field, asc))
    return chain


def _filter_only_duplicate_cleaned_names(items: list[InventoryItem]) -> list[InventoryItem]:
    counts = Counter(item.cleaned_name.strip().casefold() for item in items)
    return [
        item
        for item in items
        if counts[item.cleaned_name.strip().casefold()] > 1
    ]


class MainWindow(QMainWindow):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.setWindowTitle("Game Backup Manager")
        self.root_infos: list[RootDisplayInfo] = []
        self.inventory: list[InventoryItem] = []
        self._refresh_needed = False
        self._right_sort_column = _column_index_for_field("cleaned_name")
        self._right_sort_ascending = True
        self._initial_split_applied = False
        self._show_only_duplicates = False
        self._visible_right_items: list[InventoryItem] = []

        root = QWidget(self)
        root_layout = QVBoxLayout(root)

        top_controls = QHBoxLayout()
        self.add_root_btn = QPushButton("Add Root")
        self.remove_root_btn = QPushButton("Remove Selected Root")
        self.refresh_btn = QPushButton("Refresh")
        self.reset_sort_btn = QPushButton("Reset Sort")
        self.show_duplicates_btn = QPushButton("Show Only Duplicates")
        self.show_duplicates_btn.setCheckable(True)
        self.delete_selected_btn = QPushButton("Delete Selected")
        self.cleanup_btn = QPushButton("Cleanup Names (Disk)")
        self.tags_btn = QPushButton("Find Tags")
        self.move_btn = QPushButton("Move ISO/Archives")
        top_controls.addWidget(self.add_root_btn)
        top_controls.addWidget(self.remove_root_btn)
        top_controls.addWidget(self.refresh_btn)
        top_controls.addWidget(self.reset_sort_btn)
        top_controls.addWidget(self.show_duplicates_btn)
        top_controls.addWidget(self.delete_selected_btn)
        top_controls.addWidget(self.cleanup_btn)
        top_controls.addWidget(self.tags_btn)
        top_controls.addWidget(self.move_btn)
        top_controls.addStretch(1)
        root_layout.addLayout(top_controls)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, root)
        root_layout.addWidget(self.splitter, 1)

        self.left_panel = QWidget(self.splitter)
        left_layout = QVBoxLayout(self.left_panel)
        left_controls = QHBoxLayout()
        left_controls.addWidget(QLabel("Sort by:"))
        self.left_sort_combo = QComboBox(self.left_panel)
        self.left_sort_combo.addItems(LEFT_SORT_OPTIONS.keys())
        left_controls.addWidget(self.left_sort_combo)
        left_controls.addWidget(QLabel("Display:"))
        self.left_label_combo = QComboBox(self.left_panel)
        self.left_label_combo.addItems(LEFT_LABEL_OPTIONS.keys())
        left_controls.addWidget(self.left_label_combo)
        left_controls.addStretch(1)
        left_layout.addLayout(left_controls)

        self.left_table = QTableWidget(0, 1, self.left_panel)
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
        self.right_table = QTableWidget(0, len(RIGHT_COLUMNS), right_panel)
        self._update_right_headers()
        self.right_table.verticalHeader().setVisible(False)
        self.right_table.horizontalHeader().setStretchLastSection(True)
        self.right_table.horizontalHeader().setSectionsClickable(True)
        self.right_table.setWordWrap(True)
        self.right_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.right_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.right_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        right_layout.addWidget(self.right_table, 1)

        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([300, 1000])

        self.setCentralWidget(root)
        self.statusBar().showMessage("Ready")

        self._wire_events()
        self._load_prefs()
        self.refresh_all()
        QTimer.singleShot(0, self._apply_initial_splitter_sizes)

    def _wire_events(self) -> None:
        self.add_root_btn.clicked.connect(self._on_add_root)
        self.remove_root_btn.clicked.connect(self._on_remove_root)
        self.refresh_btn.clicked.connect(self.refresh_all)
        self.reset_sort_btn.clicked.connect(self._on_reset_right_sort)
        self.show_duplicates_btn.toggled.connect(self._on_toggle_show_duplicates)
        self.delete_selected_btn.clicked.connect(self._on_delete_selected_entries)
        self.cleanup_btn.clicked.connect(self._on_cleanup)
        self.tags_btn.clicked.connect(self._on_find_tags)
        self.move_btn.clicked.connect(self._on_move_archives)
        self.left_sort_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_label_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.right_table.horizontalHeader().sectionClicked.connect(
            self._on_right_header_clicked
        )
        self.right_table.itemDoubleClicked.connect(self._on_right_item_double_clicked)
        self.right_table.customContextMenuRequested.connect(self._on_right_context_menu)

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
        self._populate_right(self.inventory)

    def _on_reset_right_sort(self) -> None:
        self._right_sort_column = _column_index_for_field("cleaned_name")
        self._right_sort_ascending = True
        self._update_right_headers()
        self._populate_right(self.inventory)

    def _on_toggle_show_duplicates(self, checked: bool) -> None:
        self._show_only_duplicates = checked
        self._populate_right(self.inventory)
        if checked:
            self.statusBar().showMessage("Showing only duplicate cleaned names.")
        else:
            self.statusBar().showMessage("Showing all entries.")

    def _on_right_header_clicked(self, column: int) -> None:
        if column < 0 or column >= len(RIGHT_COLUMNS):
            return
        if column == self._right_sort_column:
            self._right_sort_ascending = not self._right_sort_ascending
        else:
            self._right_sort_column = column
            self._right_sort_ascending = True
        self._update_right_headers()
        self._populate_right(self.inventory)

    def _update_right_headers(self) -> None:
        headers: list[str] = []
        for idx, (_, label) in enumerate(RIGHT_COLUMNS):
            if idx == self._right_sort_column:
                arrow = "↑" if self._right_sort_ascending else "↓"
                headers.append(f"{label} {arrow}")
            else:
                headers.append(label)
        self.right_table.setHorizontalHeaderLabels(headers)

    def _on_right_item_double_clicked(self, table_item: QTableWidgetItem) -> None:
        row = table_item.row()
        if row < 0 or row >= len(self._visible_right_items):
            return
        entry = self._visible_right_items[row]
        self._open_in_explorer(entry.full_path)

    def _open_in_explorer(self, full_path: str) -> None:
        path = os.path.normpath(full_path)
        try:
            if os.path.isdir(path):
                subprocess.Popen(["explorer", path])
            else:
                subprocess.Popen(["explorer", f"/select,{path}"])
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Cannot Open in Explorer",
                f"Could not open path:\n{path}\n\n{exc}",
            )

    def _on_right_context_menu(self, pos) -> None:
        index = self.right_table.indexAt(pos)
        if index.isValid() and index.row() >= 0 and not self.right_table.item(
            index.row(), 0
        ).isSelected():
            self.right_table.selectRow(index.row())

        menu = QMenu(self.right_table)
        delete_action = QAction("Delete Selected", menu)
        delete_action.triggered.connect(self._on_delete_selected_entries)
        menu.addAction(delete_action)
        menu.exec(self.right_table.viewport().mapToGlobal(pos))

    def _selected_right_entries(self) -> list[InventoryItem]:
        rows = sorted({idx.row() for idx in self.right_table.selectionModel().selectedRows()})
        return [
            self._visible_right_items[row]
            for row in rows
            if 0 <= row < len(self._visible_right_items)
        ]

    def _delete_path(self, full_path: str) -> None:
        if os.path.isdir(full_path):
            shutil.rmtree(full_path)
            return
        os.remove(full_path)

    def _delete_path_with_elevation(self, full_path: str) -> tuple[bool, str]:
        path = os.path.normpath(full_path)
        if not os.path.exists(path):
            return True, ""

        def _ps_quote(value: str) -> str:
            return value.replace("'", "''")

        py_exe = _ps_quote(sys.executable)
        target = _ps_quote(path)
        script = (
            "$p = Start-Process "
            f"-FilePath '{py_exe}' "
            "-ArgumentList @('-m','gamemanager.services.elevated_delete','--path',"
            f"'{target}') "
            "-Verb RunAs -PassThru -Wait; "
            "exit $p.ExitCode"
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return False, str(exc)
        if proc.returncode == 0 and not os.path.exists(path):
            return True, ""
        err = (proc.stderr or proc.stdout or "").strip()
        if not err:
            err = f"elevated delete returned code {proc.returncode}"
        return False, err

    def _on_delete_selected_entries(self) -> None:
        selected = self._selected_right_entries()
        if not selected:
            QMessageBox.information(self, "No Selection", "Select one or more rows to delete.")
            return

        root_info_by_id = {info.root_id: info for info in self.root_infos}
        selected_groups: dict[str, list[InventoryItem]] = {}
        visible_groups: dict[str, list[InventoryItem]] = {}
        for entry in self._visible_right_items:
            key = entry.cleaned_name.strip().casefold()
            visible_groups.setdefault(key, []).append(entry)
        for entry in selected:
            key = entry.cleaned_name.strip().casefold()
            selected_groups.setdefault(key, []).append(entry)

        final_delete_paths: set[str] = set()
        for key, group_items in selected_groups.items():
            visible_group = visible_groups.get(key, [])
            all_group_selected = (
                len(visible_group) > 1 and len(group_items) == len(visible_group)
            )
            if not all_group_selected:
                for entry in group_items:
                    final_delete_paths.add(entry.full_path)
                continue

            rows = [
                (entry, self._source_for_item(entry, root_info_by_id))
                for entry in visible_group
            ]
            cleaned_title = visible_group[0].cleaned_name or "(No cleaned name)"
            dialog = DeleteGroupDialog(cleaned_title, rows, self)
            if dialog.exec() != dialog.DialogCode.Accepted:
                if dialog.cancel_all_requested:
                    return
                continue
            for entry in dialog.selected_for_delete():
                final_delete_paths.add(entry.full_path)

        if not final_delete_paths:
            QMessageBox.information(self, "No Deletion", "No items selected for deletion.")
            return

        answer = QMessageBox.warning(
            self,
            "Confirm Deletion",
            f"Delete {len(final_delete_paths)} selected item(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        deleted = 0
        failed: list[str] = []
        for full_path in sorted(final_delete_paths):
            try:
                self._delete_path(full_path)
                deleted += 1
            except OSError as exc:
                elevated_ok, elevated_err = self._delete_path_with_elevation(full_path)
                if elevated_ok:
                    deleted += 1
                    continue
                details = f"{full_path}: {exc}"
                if elevated_err:
                    details += f" | elevated retry failed: {elevated_err}"
                failed.append(details)
        self.refresh_all()
        if failed:
            details = "\n".join(failed[:8])
            QMessageBox.warning(
                self,
                "Deletion Completed with Errors",
                f"Deleted: {deleted}\nFailed: {len(failed)}\n\n{details}",
            )
            return
        QMessageBox.information(self, "Deletion Completed", f"Deleted: {deleted}")

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
            return
        self.root_infos = self.state.refresh_roots_only()
        self._populate_left(self.root_infos)
        self._mark_refresh_needed(True)
        self.statusBar().showMessage(
            "Root added. Manual refresh required to scan inventory."
        )

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
        self._mark_refresh_needed(False)
        self.statusBar().showMessage(
            f"Loaded {len(self.root_infos)} roots and {len(self.inventory)} root entries"
        )

    def _apply_initial_splitter_sizes(self) -> None:
        if self._initial_split_applied:
            return
        total = max(self.splitter.width(), self.width(), 2)
        # Ask splitter for the true minimum by forcing an extreme small left size.
        self.splitter.setSizes([1, total - 1])
        sizes = self.splitter.sizes()
        left_needed = sizes[0] if sizes and sizes[0] > 0 else self.left_panel.minimumSizeHint().width()
        self.splitter.setSizes([left_needed, max(1, total - left_needed)])
        self._initial_split_applied = True

    def _mark_refresh_needed(self, needed: bool) -> None:
        self._refresh_needed = needed
        if needed:
            self.refresh_btn.setStyleSheet(
                "QPushButton { background-color: #6b1d1d; color: #ffffff; font-weight: 600; }"
            )
            self.refresh_btn.setToolTip("Manual refresh required.")
            return
        self.refresh_btn.setStyleSheet("")
        self.refresh_btn.setToolTip("")

    def _sorted_roots(self, roots: list[RootDisplayInfo]) -> list[RootDisplayInfo]:
        sort_key = LEFT_SORT_OPTIONS[self.left_sort_combo.currentText()]
        if sort_key == "source_label":
            return sorted(
                roots,
                key=lambda x: (mountpoint_sort_key(x.mountpoint), x.source_label.casefold()),
            )
        if sort_key == "drive_name":
            return sorted(
                roots,
                key=lambda x: (mountpoint_sort_key(x.mountpoint), x.drive_name.casefold()),
            )
        return sorted(roots, key=lambda x: x.free_space_bytes, reverse=True)

    def _root_text(self, info: RootDisplayInfo) -> str:
        mode = LEFT_LABEL_OPTIONS[self.left_label_combo.currentText()]
        return _source_display_text(mode, info.source_label, info.drive_name)

    def _source_for_item(
        self, entry: InventoryItem, root_info_by_id: dict[int, RootDisplayInfo]
    ) -> str:
        mode = LEFT_LABEL_OPTIONS[self.left_label_combo.currentText()]
        info = root_info_by_id.get(entry.root_id)
        if info is None:
            return entry.source_label
        return _source_display_text(mode, info.source_label, info.drive_name)

    def _sort_value_for_field(
        self,
        entry: InventoryItem,
        field: str,
        root_info_by_id: dict[int, RootDisplayInfo],
    ):
        if field == "full_name":
            return natural_key(entry.full_name)
        if field == "cleaned_name":
            return natural_key(entry.cleaned_name)
        if field == "created_at":
            return entry.created_at
        if field == "modified_at":
            return entry.modified_at
        if field == "size_bytes":
            return entry.size_bytes
        if field == "source":
            return natural_key(self._source_for_item(entry, root_info_by_id))
        return natural_key(entry.full_name)

    def _sorted_inventory(
        self, items: list[InventoryItem], root_info_by_id: dict[int, RootDisplayInfo]
    ) -> list[InventoryItem]:
        primary_field = RIGHT_COLUMNS[self._right_sort_column][0]
        chain = _build_sort_chain(primary_field, self._right_sort_ascending)
        ordered = list(items)
        for field, asc in reversed(chain):
            key_func: Callable[[InventoryItem], object] = (
                lambda item, f=field: self._sort_value_for_field(
                    item, f, root_info_by_id
                )
            )
            ordered.sort(key=key_func, reverse=not asc)
        return ordered

    def _build_root_cell_widget(self, info: RootDisplayInfo) -> QWidget:
        container = QFrame(self.left_table)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(2)

        top_line = QHBoxLayout()
        top_line.setContentsMargins(0, 0, 0, 0)
        top_line.setSpacing(8)

        left_label = QLabel(self._root_text(info), container)
        left_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        free_label = QLabel(
            _format_size_and_free(info.total_size_bytes, info.free_space_bytes),
            container,
        )
        free_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        top_line.addWidget(left_label, 1)
        top_line.addWidget(free_label, 0)
        layout.addLayout(top_line)

        path_label = QLabel(info.root_path, container)
        path_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(path_label)
        return container

    def _populate_left(self, roots: list[RootDisplayInfo]) -> None:
        ordered = self._sorted_roots(roots)
        self.left_table.setRowCount(len(ordered))
        for row, info in enumerate(ordered):
            item = QTableWidgetItem("")
            item.setData(Qt.ItemDataRole.UserRole, info.root_id)
            item.setToolTip(
                f"Source: {info.source_label}\nDrive: {info.drive_name}\nRoot: {info.root_path}\nMountpoint: {info.mountpoint}"
            )
            item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            self.left_table.setItem(row, 0, item)
            self.left_table.setCellWidget(row, 0, self._build_root_cell_widget(info))
        self.left_table.resizeRowsToContents()

    def _populate_right(self, items: list[InventoryItem]) -> None:
        root_info_by_id = {info.root_id: info for info in self.root_infos}
        visible_items = (
            _filter_only_duplicate_cleaned_names(items)
            if self._show_only_duplicates
            else items
        )
        sorted_items = self._sorted_inventory(visible_items, root_info_by_id)
        self._visible_right_items = sorted_items
        self.right_table.setRowCount(len(sorted_items))
        for row, entry in enumerate(sorted_items):
            self.right_table.setItem(row, 0, QTableWidgetItem(entry.full_name))
            self.right_table.setItem(row, 1, QTableWidgetItem(entry.cleaned_name))
            self.right_table.setItem(
                row, 2, QTableWidgetItem(entry.modified_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.right_table.setItem(
                row, 3, QTableWidgetItem(entry.created_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.right_table.setItem(row, 4, QTableWidgetItem(_format_bytes(entry.size_bytes)))
            self.right_table.setItem(
                row, 5, QTableWidgetItem(self._source_for_item(entry, root_info_by_id))
            )
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
