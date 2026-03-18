from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from PySide6.QtCore import QEvent, QPoint, QProcess, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QAction, QIcon, QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLayout,
    QLayoutItem,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gamemanager.app_state import AppState
from gamemanager.models import InventoryItem, MovePlanItem, OperationReport, RootDisplayInfo
from gamemanager.services.icon_cache import icon_cache_key
from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services.sorting import natural_key
from gamemanager.services.storage import mountpoint_sort_key
from gamemanager.services.teracopy import DEFAULT_TERACOPY_PATH, resolve_teracopy_path
from gamemanager.ui.dialogs import (
    CleanupPreviewDialog,
    DeleteGroupDialog,
    IconPickerDialog,
    IconProviderSettingsDialog,
    IconProviderSettingsResult,
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

MOVE_BACKEND_OPTIONS = {
    "System": "system",
    "TeraCopy": "teracopy",
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


def _filter_by_root_id(
    items: list[InventoryItem], selected_root_id: int | None
) -> list[InventoryItem]:
    if selected_root_id is None:
        return items
    return [item for item in items if item.root_id == selected_root_id]


def _filter_by_cleaned_name_query(
    items: list[InventoryItem], query_text: str
) -> list[InventoryItem]:
    needle = query_text.strip().casefold()
    if not needle:
        return items
    return [item for item in items if needle in item.cleaned_name.casefold()]


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 4):
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setSpacing(spacing)

    def __del__(self):
        while self.count():
            self.takeAt(0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.sizeHint())
        left, top, right, bottom = self.getContentsMargins()
        size += QSize(left + right, top + bottom)
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(+left, +top, -right, -bottom)
        x = effective.x()
        y = effective.y()
        line_height = 0

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self.spacing()
            if next_x - self.spacing() > effective.right() and line_height > 0:
                x = effective.x()
                y += line_height + self.spacing()
                next_x = x + hint.width() + self.spacing()
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + bottom


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
        self._loaded_roots_count = 0
        self._loaded_entries_count = 0
        self._teracopy_path_pref = DEFAULT_TERACOPY_PATH
        self._teracopy_executable: str | None = None
        self._teracopy_process: QProcess | None = None
        self._teracopy_pending_batches: list[tuple[str, str, int]] = []
        self._teracopy_temp_files: list[str] = []
        self._teracopy_current_batch_size = 0
        self._teracopy_current_target = ""
        self._teracopy_current_output = ""
        self._teracopy_completion_title = "Move Completed"
        self._teracopy_total_items = 0
        self._teracopy_succeeded_items = 0
        self._teracopy_failed_items = 0
        self._teracopy_failure_details: list[str] = []
        self._teracopy_finish_callback: Callable[[int, int, list[str]], None] | None = None
        self._folder_icon_cache: dict[str, QIcon] = {}
        self._folder_icon_preview_cache: dict[str, QPixmap] = {}
        self._right_hovered_icon_row: int | None = None
        self._right_icon_hover_popup = QLabel(
            None,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self._right_icon_hover_popup.setAttribute(
            Qt.WidgetAttribute.WA_ShowWithoutActivating, True
        )
        self._right_icon_hover_popup.setFrameStyle(
            QFrame.Shape.Panel | QFrame.Shadow.Plain
        )
        self._right_icon_hover_popup.setLineWidth(1)
        self._right_icon_hover_popup.setStyleSheet(
            "background-color: #1c1c1c; padding: 4px;"
        )
        self._blank_icon = QIcon()
        blank_pix = QPixmap(16, 16)
        blank_pix.fill(Qt.GlobalColor.transparent)
        self._blank_icon.addPixmap(blank_pix)

        root = QWidget(self)
        root_layout = QVBoxLayout(root)

        top_controls = FlowLayout(spacing=4)
        top_controls.setContentsMargins(0, 0, 0, 0)
        self.add_root_btn = QPushButton("Add Root")
        self.remove_root_btn = QPushButton("Remove Root")
        self.refresh_btn = QPushButton("Refresh")
        self.reset_sort_btn = QPushButton("Reset")
        self.show_duplicates_btn = QPushButton("Dupes")
        self.show_duplicates_btn.setCheckable(True)
        self.delete_selected_btn = QPushButton("Del Selected")
        self.cleanup_btn = QPushButton("Cleanup")
        self.tags_btn = QPushButton("Find Tags")
        self.move_btn = QPushButton("Move Arch")
        self.assign_icon_btn = QPushButton("Set Icon")
        self.icon_settings_btn = QPushButton("Icon Src")
        self.move_backend_combo = QComboBox(self)
        self.move_backend_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.move_backend_combo.addItems(MOVE_BACKEND_OPTIONS.keys())
        self.locate_teracopy_btn = QPushButton("Locate TC")
        self.move_backend_label = QLabel("Move:")

        self.remove_root_btn.setToolTip("Remove Selected Root")
        self.reset_sort_btn.setToolTip("Reset Sort")
        self.show_duplicates_btn.setToolTip("Show Only Duplicates")
        self.delete_selected_btn.setToolTip("Delete Selected")
        self.cleanup_btn.setToolTip("Cleanup Names (Disk)")
        self.move_btn.setToolTip("Move ISO/Archives")
        self.assign_icon_btn.setToolTip("Assign Folder Icon...")
        self.icon_settings_btn.setToolTip("Icon Provider Settings...")
        self.locate_teracopy_btn.setToolTip("Locate TeraCopy")

        compact_controls = [
            self.add_root_btn,
            self.remove_root_btn,
            self.refresh_btn,
            self.reset_sort_btn,
            self.show_duplicates_btn,
            self.delete_selected_btn,
            self.cleanup_btn,
            self.tags_btn,
            self.move_btn,
            self.assign_icon_btn,
            self.icon_settings_btn,
            self.move_backend_label,
            self.move_backend_combo,
            self.locate_teracopy_btn,
        ]
        for control in compact_controls:
            control.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            if isinstance(control, QPushButton):
                control.setMinimumSize(control.sizeHint())

        def _build_top_group(
            widgets: list[QWidget], include_separator: bool = True
        ) -> QWidget:
            group = QWidget(root)
            group.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            row = QHBoxLayout(group)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(2)
            for widget in widgets:
                row.addWidget(widget)
            if include_separator:
                separator = QFrame(group)
                separator.setFrameShape(QFrame.Shape.VLine)
                separator.setFrameShadow(QFrame.Shadow.Sunken)
                separator.setSizePolicy(
                    QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding
                )
                row.addWidget(separator)
            group.setMinimumSize(group.sizeHint())
            return group

        top_groups = [
            _build_top_group(
                [self.add_root_btn, self.remove_root_btn, self.refresh_btn]
            ),
            _build_top_group([self.reset_sort_btn, self.show_duplicates_btn]),
            _build_top_group(
                [self.delete_selected_btn, self.cleanup_btn, self.tags_btn, self.move_btn]
            ),
            _build_top_group([self.assign_icon_btn, self.icon_settings_btn]),
            _build_top_group(
                [self.move_backend_label, self.move_backend_combo, self.locate_teracopy_btn],
                include_separator=False,
            ),
        ]
        for group in top_groups:
            top_controls.addWidget(group)
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
        self.left_table.setAcceptDrops(True)
        self.left_table.viewport().setAcceptDrops(True)
        left_layout.addWidget(self.left_table, 1)

        self.left_filter_section = QFrame(self.left_panel)
        self.left_filter_section.setFrameShape(QFrame.Shape.StyledPanel)
        self.left_filter_section.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        filter_layout = QVBoxLayout(self.left_filter_section)
        filter_layout.setContentsMargins(8, 6, 8, 6)
        filter_layout.setSpacing(4)
        filter_layout.addWidget(QLabel("Filter (Cleaned Name):", self.left_filter_section))

        self.left_filter_edit = QLineEdit(self.left_filter_section)
        self.left_filter_edit.setPlaceholderText(
            "Type anywhere to filter by cleaned name"
        )
        self.left_filter_edit.setClearButtonEnabled(True)
        filter_layout.addWidget(self.left_filter_edit)
        left_layout.addWidget(self.left_filter_section, 0)

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
        self.right_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers
        )
        self.right_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.right_table.setDragEnabled(True)
        self.right_table.setDragDropMode(QTableWidget.DragDropMode.DragOnly)
        self.right_table.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.right_table.setMouseTracking(True)
        self.right_table.viewport().setMouseTracking(True)
        right_layout.addWidget(self.right_table, 1)

        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([300, 1000])

        self.setCentralWidget(root)
        self._status_left_label = QLabel("", self)
        self._status_left_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addWidget(self._status_left_label, 1)
        self._status_selected_label = QLabel("| Selected: 0 games, 0 B", self)
        self._status_selected_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addPermanentWidget(self._status_selected_label, 0)

        self._wire_events()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
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
        self.assign_icon_btn.clicked.connect(self._on_assign_folder_icon_selected)
        self.icon_settings_btn.clicked.connect(self._on_icon_provider_settings)
        self.move_backend_combo.currentTextChanged.connect(self._on_move_backend_changed)
        self.locate_teracopy_btn.clicked.connect(self._on_locate_teracopy)
        self.left_sort_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_label_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_filter_edit.textChanged.connect(self._on_left_filter_changed)
        self.left_table.itemSelectionChanged.connect(self._on_left_selection_changed)
        self.left_table.viewport().installEventFilter(self)
        self.right_table.viewport().installEventFilter(self)
        self.right_table.horizontalHeader().sectionClicked.connect(
            self._on_right_header_clicked
        )
        self.right_table.itemDoubleClicked.connect(self._on_right_item_double_clicked)
        self.right_table.customContextMenuRequested.connect(self._on_right_context_menu)
        self.right_table.itemSelectionChanged.connect(self._on_right_selection_changed)

    def _load_prefs(self) -> None:
        sort_pref = self.state.get_ui_pref("left_sort", "source_label")
        label_pref = self.state.get_ui_pref("left_label_mode", "source")
        move_backend_pref = self.state.get_ui_pref("move_backend", "system")
        self._teracopy_path_pref = self.state.get_ui_pref(
            "teracopy_path", DEFAULT_TERACOPY_PATH
        )

        for text, value in LEFT_SORT_OPTIONS.items():
            if value == sort_pref:
                self.left_sort_combo.setCurrentText(text)
                break
        for text, value in LEFT_LABEL_OPTIONS.items():
            if value == label_pref:
                self.left_label_combo.setCurrentText(text)
                break
        for text, value in MOVE_BACKEND_OPTIONS.items():
            if value == move_backend_pref:
                self.move_backend_combo.setCurrentText(text)
                break
        self._teracopy_executable = resolve_teracopy_path(self._teracopy_path_pref)
        self._update_move_backend_ui()

    def _on_left_pref_changed(self) -> None:
        sort_key = LEFT_SORT_OPTIONS[self.left_sort_combo.currentText()]
        label_mode = LEFT_LABEL_OPTIONS[self.left_label_combo.currentText()]
        self.state.set_ui_pref("left_sort", sort_key)
        self.state.set_ui_pref("left_label_mode", label_mode)
        self._populate_left(self.root_infos)
        self._populate_right(self.inventory)

    def _current_move_backend(self) -> str:
        return MOVE_BACKEND_OPTIONS.get(self.move_backend_combo.currentText(), "system")

    def _update_move_backend_ui(self) -> None:
        use_teracopy = self._current_move_backend() == "teracopy"
        self.locate_teracopy_btn.setEnabled(use_teracopy)
        if use_teracopy:
            path = resolve_teracopy_path(self._teracopy_path_pref)
            self._teracopy_executable = path
            if path:
                self.locate_teracopy_btn.setToolTip(f"Locate TeraCopy\n{path}")
            else:
                self.locate_teracopy_btn.setToolTip(
                    "Locate TeraCopy\nTeraCopy not found. Click to auto-locate or choose manually."
                )
        else:
            self.locate_teracopy_btn.setToolTip("Locate TeraCopy")

    def _on_move_backend_changed(self, _text: str) -> None:
        backend = self._current_move_backend()
        self.state.set_ui_pref("move_backend", backend)
        self._update_move_backend_ui()

    def _on_locate_teracopy(self) -> None:
        resolved = resolve_teracopy_path(self._teracopy_path_pref)
        if resolved:
            self._teracopy_path_pref = resolved
            self._teracopy_executable = resolved
            self.state.set_ui_pref("teracopy_path", resolved)
            self._update_move_backend_ui()
            QMessageBox.information(self, "TeraCopy", f"Using:\n{resolved}")
            return

        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Locate TeraCopy.exe",
            r"C:\Program Files\TeraCopy",
            "Executables (*.exe);;All Files (*)",
        )
        if not selected:
            return
        selected = os.path.normpath(selected)
        if not os.path.isfile(selected):
            QMessageBox.warning(
                self, "Invalid TeraCopy Path", f"File does not exist:\n{selected}"
            )
            return
        self._teracopy_path_pref = selected
        self._teracopy_executable = selected
        self.state.set_ui_pref("teracopy_path", selected)
        self._update_move_backend_ui()
        QMessageBox.information(self, "TeraCopy", f"Using:\n{selected}")

    def _on_reset_right_sort(self) -> None:
        self._right_sort_column = _column_index_for_field("cleaned_name")
        self._right_sort_ascending = True
        self._update_right_headers()
        self._populate_right(self.inventory)

    def _on_left_selection_changed(self) -> None:
        self._populate_right(self.inventory)

    def _on_left_filter_changed(self, _text: str) -> None:
        self._populate_right(self.inventory)

    def _handle_global_filter_keypress(self, event: QKeyEvent) -> bool:
        active_window = QApplication.activeWindow()
        if active_window is not self:
            return False
        if event.modifiers() & (
            Qt.KeyboardModifier.ControlModifier
            | Qt.KeyboardModifier.AltModifier
            | Qt.KeyboardModifier.MetaModifier
        ):
            return False
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QComboBox)):
            return False
        key = event.key()
        current = self.left_filter_edit.text()
        if key == Qt.Key.Key_Backspace:
            if current:
                self.left_filter_edit.setText(current[:-1])
                return True
            return False
        if key == Qt.Key.Key_Escape:
            if current:
                self.left_filter_edit.clear()
                return True
            return False
        text = event.text()
        if text and text.isprintable():
            self.left_filter_edit.setText(f"{current}{text}")
            self.left_filter_edit.setCursorPosition(len(self.left_filter_edit.text()))
            return True
        return False

    def eventFilter(self, watched, event):  # type: ignore[override]
        if event.type() == QEvent.Type.KeyPress and self._handle_global_filter_keypress(
            event
        ):
            return True
        if watched is self.left_table.viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    idx = self.left_table.indexAt(event.pos())
                    selected_rows = {row.row() for row in self.left_table.selectionModel().selectedRows()}
                    if not idx.isValid():
                        if selected_rows:
                            self.left_table.clearSelection()
                        return False
                    if (
                        idx.row() in selected_rows
                        and len(selected_rows) == 1
                        and event.modifiers() == Qt.KeyboardModifier.NoModifier
                    ):
                        self.left_table.clearSelection()
                        return True
            if event.type() in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if event.source() is self.right_table:
                    idx = self.left_table.indexAt(event.pos())
                    if idx.isValid() and self._selected_right_entries():
                        event.acceptProposedAction()
                        return True
                event.ignore()
                return True
            if event.type() == QEvent.Type.Drop:
                if event.source() is not self.right_table:
                    event.ignore()
                    return True
                idx = self.left_table.indexAt(event.pos())
                if not idx.isValid():
                    event.ignore()
                    return True
                dest_item = self.left_table.item(idx.row(), 0)
                if dest_item is None:
                    event.ignore()
                    return True
                root_id = dest_item.data(Qt.ItemDataRole.UserRole)
                if root_id is None:
                    event.ignore()
                    return True
                self._move_selected_entries_to_root(int(root_id))
                event.acceptProposedAction()
                return True
        if watched is self.right_table.viewport():
            if event.type() == QEvent.Type.MouseMove:
                index = self.right_table.indexAt(event.pos())
                if index.isValid() and index.column() == 0:
                    cell_rect = self.right_table.visualRect(index)
                    icon_hit = cell_rect.adjusted(
                        0, 0, -(cell_rect.width() - 26), 0
                    )
                    if icon_hit.contains(event.pos()):
                        self._show_right_icon_hover_preview(
                            index.row(),
                            self.right_table.viewport().mapToGlobal(event.pos()),
                        )
                        return False
                self._hide_right_icon_hover_preview()
            elif event.type() in (
                QEvent.Type.Leave,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Wheel,
            ):
                self._hide_right_icon_hover_preview()
        return super().eventFilter(watched, event)

    def _on_toggle_show_duplicates(self, checked: bool) -> None:
        self._show_only_duplicates = checked
        self._populate_right(self.inventory)

    def _on_right_selection_changed(self) -> None:
        self._update_counts_status()

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
        assign_icon_action = QAction("Assign Folder Icon...", menu)
        assign_icon_action.triggered.connect(self._on_assign_folder_icon_selected)
        menu.addAction(assign_icon_action)
        rename_action = QAction("Edit Name...", menu)
        rename_action.triggered.connect(self._on_manual_rename_selected_entry)
        menu.addAction(rename_action)
        delete_action = QAction("Delete Selected", menu)
        delete_action.triggered.connect(self._on_delete_selected_entries)
        menu.addAction(delete_action)
        menu.exec(self.right_table.viewport().mapToGlobal(pos))

    def _selected_right_entries(self) -> list[InventoryItem]:
        model = self.right_table.selectionModel()
        if model is None:
            return []
        rows = sorted({idx.row() for idx in model.selectedRows()})
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

    def _move_selected_entries_to_root(self, destination_root_id: int) -> None:
        selected = self._selected_right_entries()
        if not selected:
            return
        destination = next(
            (info for info in self.root_infos if info.root_id == destination_root_id), None
        )
        if destination is None:
            QMessageBox.warning(self, "Invalid Destination", "Destination root not found.")
            return

        total_size = sum(item.size_bytes for item in selected)
        try:
            free_space = shutil.disk_usage(destination.root_path).free
        except OSError:
            free_space = destination.free_space_bytes
        if total_size > free_space:
            QMessageBox.warning(
                self,
                "Not Enough Space",
                "Drag and drop cancelled: not enough free space on destination root.\n\n"
                f"Selected size: {_format_bytes(total_size)}\n"
                f"Free on destination: {_format_bytes(free_space)}\n"
                f"Destination root: {destination.root_path}",
            )
            return

        conflicts: list[str] = []
        move_pairs: list[tuple[str, str]] = []
        for entry in selected:
            src = os.path.normpath(entry.full_path)
            dst = os.path.normpath(os.path.join(destination.root_path, entry.full_name))
            if os.path.normcase(src) == os.path.normcase(dst):
                continue
            if os.path.exists(dst):
                conflicts.append(f"{entry.full_name} -> {dst}")
                continue
            move_pairs.append((src, dst))
        if conflicts:
            details = "\n".join(conflicts[:8])
            QMessageBox.warning(
                self,
                "Destination Conflicts",
                "Drag and drop cancelled: destination already has item(s).\n\n"
                f"{details}",
            )
            return
        if not move_pairs:
            return

        answer = QMessageBox.question(
            self,
            "Confirm Move",
            "Move selected games to destination root?\n\n"
            f"Games selected: {len(move_pairs)}\n"
            f"Total size: {_format_bytes(total_size)}\n"
            f"Destination root: {destination.root_path}",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Ok:
            return

        if self._current_move_backend() == "teracopy":
            if self._teracopy_process is not None:
                QMessageBox.information(
                    self,
                    "Move In Progress",
                    "Wait for the current TeraCopy operation to finish first.",
                )
                return
            if self._start_teracopy_move_pairs(
                move_pairs, completion_title="Move Completed"
            ):
                return
            QMessageBox.warning(
                self,
                "TeraCopy Unavailable",
                "TeraCopy could not be located. Falling back to system move.",
            )

        moved = 0
        failed: list[str] = []
        for src, dst in move_pairs:
            try:
                shutil.move(src, dst)
                moved += 1
            except OSError as exc:
                failed.append(f"{src} -> {dst}: {exc}")
        self.refresh_all()
        if failed:
            details = "\n".join(failed[:8])
            QMessageBox.warning(
                self,
                "Move Completed with Errors",
                f"Moved: {moved}\nFailed: {len(failed)}\n\n{details}",
            )
            return
        QMessageBox.information(self, "Move Completed", f"Moved: {moved}")

    def _set_move_controls_busy(self, busy: bool) -> None:
        self.move_btn.setEnabled(not busy)
        self.move_backend_combo.setEnabled(not busy)
        self.locate_teracopy_btn.setEnabled(
            (not busy) and self._current_move_backend() == "teracopy"
        )
        self.right_table.setDragEnabled(not busy)

    def _resolve_teracopy_for_move(self, allow_manual_pick: bool) -> str | None:
        resolved = resolve_teracopy_path(self._teracopy_path_pref)
        if resolved:
            if resolved != self._teracopy_path_pref:
                self._teracopy_path_pref = resolved
                self.state.set_ui_pref("teracopy_path", resolved)
            self._teracopy_executable = resolved
            self._update_move_backend_ui()
            return resolved
        if not allow_manual_pick:
            return None
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Locate TeraCopy.exe",
            r"C:\Program Files\TeraCopy",
            "Executables (*.exe);;All Files (*)",
        )
        if not selected:
            return None
        selected = os.path.normpath(selected)
        if not os.path.isfile(selected):
            QMessageBox.warning(
                self, "Invalid TeraCopy Path", f"File does not exist:\n{selected}"
            )
            return None
        self._teracopy_path_pref = selected
        self._teracopy_executable = selected
        self.state.set_ui_pref("teracopy_path", selected)
        self._update_move_backend_ui()
        return selected

    def _start_teracopy_move_pairs(
        self,
        move_pairs: list[tuple[str, str]],
        completion_title: str,
        on_finish: Callable[[int, int, list[str]], None] | None = None,
    ) -> bool:
        if self._teracopy_process is not None:
            QMessageBox.information(
                self,
                "Move In Progress",
                "Wait for the current TeraCopy operation to finish first.",
            )
            return False

        teracopy_exe = self._resolve_teracopy_for_move(allow_manual_pick=True)
        if not teracopy_exe:
            return False

        grouped: dict[str, list[str]] = {}
        unsupported: list[tuple[str, str]] = []
        for src, dst in move_pairs:
            src_name = os.path.basename(src)
            dst_name = os.path.basename(dst)
            if src_name.casefold() != dst_name.casefold():
                unsupported.append((src, dst))
                continue
            target_dir = os.path.dirname(dst)
            grouped.setdefault(target_dir, []).append(src)

        self._teracopy_pending_batches = []
        self._teracopy_temp_files = []
        self._teracopy_total_items = 0
        self._teracopy_succeeded_items = 0
        self._teracopy_failed_items = len(unsupported)
        self._teracopy_current_batch_size = 0
        self._teracopy_current_target = ""
        self._teracopy_current_output = ""
        self._teracopy_completion_title = completion_title
        self._teracopy_finish_callback = on_finish
        self._teracopy_failure_details = [
            f"Fallback needed (renamed destination): {src} -> {dst}"
            for src, dst in unsupported
        ]

        for target_dir, sources in grouped.items():
            if not sources:
                continue
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as exc:
                self._teracopy_failed_items += len(sources)
                self._teracopy_failure_details.append(
                    f"Cannot create target folder {target_dir}: {exc}"
                )
                continue
            handle = tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", suffix=".txt", delete=False
            )
            with handle:
                for src in sources:
                    handle.write(f"{src}\n")
            list_path = os.path.normpath(handle.name)
            self._teracopy_temp_files.append(list_path)
            self._teracopy_pending_batches.append((list_path, target_dir, len(sources)))
            self._teracopy_total_items += len(sources)

        if not self._teracopy_pending_batches:
            self._cleanup_teracopy_temp_files()
            self.refresh_all()
            if self._teracopy_finish_callback is not None:
                callback = self._teracopy_finish_callback
                self._teracopy_finish_callback = None
                callback(0, self._teracopy_failed_items, self._teracopy_failure_details)
            elif self._teracopy_failed_items:
                details = "\n".join(self._teracopy_failure_details[:8])
                QMessageBox.warning(
                    self,
                    completion_title,
                    f"Moved: 0\nFailed: {self._teracopy_failed_items}\n\n{details}",
                )
            return True

        self._set_move_controls_busy(True)
        self._teracopy_executable = teracopy_exe
        self._launch_next_teracopy_batch()
        return True

    def _launch_next_teracopy_batch(self) -> None:
        if not self._teracopy_pending_batches:
            self._finish_teracopy_session()
            return
        list_path, target_dir, batch_size = self._teracopy_pending_batches.pop(0)
        self._teracopy_current_batch_size = batch_size
        self._teracopy_current_target = target_dir
        self._teracopy_current_output = ""

        proc = QProcess(self)
        self._teracopy_process = proc
        proc.readyReadStandardOutput.connect(self._on_teracopy_ready_read)
        proc.readyReadStandardError.connect(self._on_teracopy_ready_read)
        proc.errorOccurred.connect(self._on_teracopy_error)
        proc.finished.connect(self._on_teracopy_finished)
        proc.setProgram(self._teracopy_executable or "")
        proc.setArguments(["Move", f"*{list_path}", target_dir, "/Close"])
        proc.start()

    def _on_teracopy_ready_read(self) -> None:
        if self._teracopy_process is None:
            return
        out = bytes(self._teracopy_process.readAllStandardOutput()).decode(
            "utf-8", errors="ignore"
        )
        err = bytes(self._teracopy_process.readAllStandardError()).decode(
            "utf-8", errors="ignore"
        )
        chunk = (out + "\n" + err).strip()
        if chunk:
            self._teracopy_current_output = (
                f"{self._teracopy_current_output}\n{chunk}".strip()
            )

    def _on_teracopy_error(self, process_error: QProcess.ProcessError) -> None:
        self._teracopy_failure_details.append(
            f"TeraCopy process error {int(process_error)} for {self._teracopy_current_target}"
        )

    def _on_teracopy_finished(
        self, exit_code: int, exit_status: QProcess.ExitStatus
    ) -> None:
        if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            self._teracopy_succeeded_items += self._teracopy_current_batch_size
        else:
            self._teracopy_failed_items += self._teracopy_current_batch_size
            details = self._teracopy_current_output
            if details:
                details = details.splitlines()[-1].strip()
            if not details:
                details = f"exit_code={exit_code}"
            self._teracopy_failure_details.append(
                f"TeraCopy failed for {self._teracopy_current_target}: {details}"
            )

        if self._teracopy_process is not None:
            self._teracopy_process.deleteLater()
            self._teracopy_process = None
        self._launch_next_teracopy_batch()

    def _cleanup_teracopy_temp_files(self) -> None:
        for path in self._teracopy_temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._teracopy_temp_files.clear()

    def _finish_teracopy_session(self) -> None:
        self._cleanup_teracopy_temp_files()
        self._set_move_controls_busy(False)
        succeeded = self._teracopy_succeeded_items
        failed = self._teracopy_failed_items
        details = list(self._teracopy_failure_details)
        callback = self._teracopy_finish_callback
        self._teracopy_finish_callback = None
        self.refresh_all()
        if callback is not None:
            callback(succeeded, failed, details)
            return
        if failed:
            detail_text = "\n".join(details[:8])
            QMessageBox.warning(
                self,
                self._teracopy_completion_title,
                f"Moved: {succeeded}\nFailed: {failed}\n\n{detail_text}",
            )
            return
        QMessageBox.information(
            self, self._teracopy_completion_title, f"Moved: {succeeded}"
        )

    def _on_manual_rename_selected_entry(self) -> None:
        selected = self._selected_right_entries()
        if len(selected) != 1:
            QMessageBox.information(
                self,
                "Select One Entry",
                "Select exactly one row to use manual rename.",
            )
            return
        entry = selected[0]
        old_path = os.path.normpath(entry.full_path)
        parent_dir = os.path.dirname(old_path)
        old_name = os.path.basename(old_path)

        new_name, ok = QInputDialog.getText(
            self,
            "Manual Rename",
            "New name:",
            text=old_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "Invalid Name", "New name cannot be empty.")
            return
        if any(ch in new_name for ch in ("/", "\\")):
            QMessageBox.warning(
                self, "Invalid Name", "New name cannot include path separators."
            )
            return
        if new_name == old_name:
            return
        new_path = os.path.join(parent_dir, new_name)
        if os.path.exists(new_path):
            QMessageBox.warning(
                self,
                "Name Conflict",
                f"Destination already exists:\n{new_path}",
            )
            return
        confirm = QMessageBox.question(
            self,
            "Confirm Rename",
            "Rename this item on disk?\n\n"
            f"From: {old_name}\n"
            f"To:   {new_name}\n\n"
            f"Folder: {parent_dir}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            os.rename(old_path, new_path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Rename Failed",
                f"Could not rename:\n{old_path}\n\nto:\n{new_path}\n\n{exc}",
            )
            return
        self.refresh_all()

    def _on_icon_provider_settings(self) -> None:
        current = self.state.icon_search_settings()
        initial = IconProviderSettingsResult(
            steamgriddb_enabled=current.steamgriddb_enabled,
            steamgriddb_api_key=current.steamgriddb_api_key,
            steamgriddb_api_base=current.steamgriddb_api_base,
            iconfinder_enabled=current.iconfinder_enabled,
            iconfinder_api_key=current.iconfinder_api_key,
            iconfinder_api_base=current.iconfinder_api_base,
        )
        dialog = IconProviderSettingsDialog(
            initial=initial,
            test_callback=self._test_icon_provider_settings,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload()
        settings = IconSearchSettings(
            steamgriddb_enabled=payload.steamgriddb_enabled,
            steamgriddb_api_key=payload.steamgriddb_api_key,
            steamgriddb_api_base=payload.steamgriddb_api_base,
            iconfinder_enabled=payload.iconfinder_enabled,
            iconfinder_api_key=payload.iconfinder_api_key,
            iconfinder_api_base=payload.iconfinder_api_base,
        )
        try:
            self.state.save_icon_search_settings(settings)
        except RuntimeError as exc:
            QMessageBox.warning(self, "Icon Provider Settings", str(exc))
            return

    def _test_icon_provider_settings(self, payload: IconProviderSettingsResult) -> str:
        settings = IconSearchSettings(
            steamgriddb_enabled=payload.steamgriddb_enabled,
            steamgriddb_api_key=payload.steamgriddb_api_key,
            steamgriddb_api_base=payload.steamgriddb_api_base,
            iconfinder_enabled=payload.iconfinder_enabled,
            iconfinder_api_key=payload.iconfinder_api_key,
            iconfinder_api_base=payload.iconfinder_api_base,
        )
        return self.state.test_icon_search_settings(settings)

    def _icon_for_entry(self, entry: InventoryItem) -> QIcon:
        if entry.is_dir and entry.icon_status == "valid" and entry.folder_icon_path:
            icon_path = os.path.normpath(entry.folder_icon_path)
            cache_key = icon_cache_key(icon_path, 16)
            cached = self._folder_icon_cache.get(cache_key)
            if cached is not None:
                return cached
            icon = QIcon(icon_path)
            if icon.isNull():
                return self._blank_icon
            self._folder_icon_cache[cache_key] = icon
            return icon
        return self._blank_icon

    def _icon_preview_pixmap_for_entry(self, entry: InventoryItem) -> QPixmap | None:
        if not (entry.is_dir and entry.icon_status == "valid" and entry.folder_icon_path):
            return None
        icon_path = os.path.normpath(entry.folder_icon_path)
        cache_key = icon_cache_key(icon_path, 256)
        cached = self._folder_icon_preview_cache.get(cache_key)
        if cached is not None:
            return cached
        icon = QIcon(icon_path)
        if icon.isNull():
            return None
        pix = icon.pixmap(256, 256)
        if pix.isNull():
            return None
        self._folder_icon_preview_cache[cache_key] = pix
        return pix

    def _show_right_icon_hover_preview(self, row: int, global_pos: QPoint) -> None:
        if row < 0 or row >= len(self._visible_right_items):
            self._hide_right_icon_hover_preview()
            return
        entry = self._visible_right_items[row]
        pix = self._icon_preview_pixmap_for_entry(entry)
        if pix is None or pix.isNull():
            self._hide_right_icon_hover_preview()
            return
        if self._right_hovered_icon_row != row:
            self._right_icon_hover_popup.setPixmap(pix)
            self._right_icon_hover_popup.adjustSize()
            self._right_hovered_icon_row = row
        self._position_right_icon_hover_popup(global_pos)
        self._right_icon_hover_popup.show()

    def _position_right_icon_hover_popup(self, global_pos: QPoint) -> None:
        popup_size = self._right_icon_hover_popup.sizeHint()
        x = global_pos.x() + 20
        y = global_pos.y() + 20
        screen = QApplication.primaryScreen()
        if screen is not None:
            rect = screen.availableGeometry()
            x = min(max(rect.left(), x), max(rect.left(), rect.right() - popup_size.width()))
            y = min(max(rect.top(), y), max(rect.top(), rect.bottom() - popup_size.height()))
        self._right_icon_hover_popup.move(x, y)

    def _hide_right_icon_hover_preview(self) -> None:
        self._right_icon_hover_popup.hide()
        self._right_hovered_icon_row = None

    def _on_assign_folder_icon_selected(self) -> None:
        selected = [item for item in self._selected_right_entries() if item.is_dir]
        if not selected:
            QMessageBox.information(
                self,
                "No Folder Selected",
                "Select one or more folder rows to assign an icon.",
            )
            return

        skipped = 0
        applied = 0
        failed: list[str] = []
        for entry in selected:
            if entry.icon_status == "valid":
                skipped += 1
                continue

            try:
                candidates = self.state.search_icon_candidates(
                    entry.full_name, entry.cleaned_name
                )
            except Exception as exc:
                candidates = []
                failed.append(f"{entry.full_name}: search failed ({exc})")

            dialog = IconPickerDialog(
                folder_name=entry.full_name,
                candidates=candidates,
                preview_loader=self.state.candidate_preview,
                parent=self,
            )
            if dialog.exec() != dialog.DialogCode.Accepted:
                continue
            payload = dialog.result_payload()
            image_bytes: bytes
            if payload.local_image_path:
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

            result = self.state.apply_folder_icon(
                folder_path=entry.full_path,
                source_image=image_bytes,
                icon_name_hint=entry.cleaned_name or entry.full_name,
                info_tip=payload.info_tip,
                circular_ring=payload.circular_ring,
            )
            if result.status != "applied":
                failed.append(f"{entry.full_name}: {result.message}")
                continue
            applied += 1

        self.refresh_all()
        lines = [f"Applied: {applied}", f"Skipped existing: {skipped}"]
        if failed:
            lines.append(f"Failed: {len(failed)}")
            lines.append("")
            lines.extend(failed[:8])
        QMessageBox.information(self, "Assign Folder Icon", "\n".join(lines))

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

    def _status_base_text(self) -> str:
        active_roots = 1 if self._selected_root_id() is not None else len(self.root_infos)
        visible_games = len(self._visible_right_items)
        return (
            f"Roots active: {active_roots}/{self._loaded_roots_count} | "
            f"Games visible: {visible_games}/{self._loaded_entries_count}"
        )

    def _update_counts_status(self) -> None:
        selected = self._selected_right_entries()
        selected_size = sum(item.size_bytes for item in selected)
        self._status_left_label.setText(self._status_base_text())
        self._status_selected_label.setText(
            f"| Selected: {len(selected)} games, {_format_bytes(selected_size)}"
        )

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
        self.root_infos, self.inventory = self.state.refresh()
        self._folder_icon_cache.clear()
        self._folder_icon_preview_cache.clear()
        self._hide_right_icon_hover_preview()
        self._loaded_roots_count = len(self.root_infos)
        self._loaded_entries_count = len(self.inventory)
        self._populate_left(self.root_infos)
        self._populate_right(self.inventory)
        self._mark_refresh_needed(False)
        self._update_counts_status()

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
        selected_root_id = self._selected_root_id()
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
            if selected_root_id is not None and info.root_id == selected_root_id:
                self.left_table.selectRow(row)
        self.left_table.resizeRowsToContents()

    def _populate_right(self, items: list[InventoryItem]) -> None:
        root_info_by_id = {info.root_id: info for info in self.root_infos}
        selected_root_id = self._selected_root_id()
        visible_items = _filter_by_root_id(items, selected_root_id)
        visible_items = _filter_by_cleaned_name_query(
            visible_items, self.left_filter_edit.text()
        )
        visible_items = (
            _filter_only_duplicate_cleaned_names(visible_items)
            if self._show_only_duplicates
            else visible_items
        )
        sorted_items = self._sorted_inventory(visible_items, root_info_by_id)
        self._visible_right_items = sorted_items
        self.right_table.setRowCount(len(sorted_items))
        for row, entry in enumerate(sorted_items):
            name_item = QTableWidgetItem(entry.full_name)
            name_item.setIcon(self._icon_for_entry(entry))
            self.right_table.setItem(row, 0, name_item)
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
        self._update_counts_status()

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
        applied_items = dialog.applied_items()
        if self._current_move_backend() == "teracopy":
            self._execute_archive_moves_with_teracopy(applied_items)
            return

        report = self.state.execute_archive_move_plan(applied_items)
        self._show_archive_move_report(report)
        self.refresh_all()

    def _show_archive_move_report(self, report: OperationReport) -> None:
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

    def _execute_archive_moves_with_teracopy(
        self, plan_items: list[MovePlanItem]
    ) -> None:
        if self._teracopy_process is not None:
            QMessageBox.information(
                self,
                "Move In Progress",
                "Wait for the current TeraCopy operation to finish first.",
            )
            return
        if not self._resolve_teracopy_for_move(allow_manual_pick=True):
            QMessageBox.warning(
                self,
                "TeraCopy Unavailable",
                "TeraCopy could not be located. Falling back to system move.",
            )
            fallback_report = self.state.execute_archive_move_plan(plan_items)
            self._show_archive_move_report(fallback_report)
            self.refresh_all()
            return

        report = OperationReport(total=len(plan_items))
        teracopy_pairs: list[tuple[str, str]] = []

        for item in plan_items:
            action = item.selected_action
            if action == "skip":
                report.skipped += 1
                if item.status == "conflict":
                    report.conflicts += 1
                continue

            src_path = os.path.normpath(str(item.src_path))
            dst_path = os.path.normpath(str(item.dst_path))
            dst_folder = os.path.normpath(str(item.dst_folder))
            if action == "rename":
                if not item.manual_name:
                    report.failed += 1
                    report.details.append(
                        f"Missing manual name for {src_path}; action skipped."
                    )
                    continue
                dst_path = os.path.normpath(os.path.join(dst_folder, item.manual_name))

            if action in {"overwrite", "delete_destination"} and os.path.exists(dst_path):
                try:
                    self._delete_path(dst_path)
                except OSError as exc:
                    report.failed += 1
                    report.details.append(f"Failed removing destination {dst_path}: {exc}")
                    continue

            if os.path.exists(dst_path):
                report.conflicts += 1
                report.skipped += 1
                report.details.append(f"Conflict remains at destination: {dst_path}")
                continue
            if not os.path.exists(src_path):
                report.failed += 1
                report.details.append(f"Source does not exist: {src_path}")
                continue
            try:
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            except OSError as exc:
                report.failed += 1
                report.details.append(
                    f"Failed creating destination folder for {dst_path}: {exc}"
                )
                continue

            if os.path.basename(src_path).casefold() != os.path.basename(dst_path).casefold():
                try:
                    shutil.move(src_path, dst_path)
                    report.succeeded += 1
                except OSError as exc:
                    report.failed += 1
                    report.details.append(f"Failed move {src_path}: {exc}")
                continue
            teracopy_pairs.append((src_path, dst_path))

        def _on_finish(succeeded: int, failed: int, details: list[str]) -> None:
            report.succeeded += succeeded
            report.failed += failed
            report.details.extend(details)
            self._show_archive_move_report(report)

        if teracopy_pairs:
            if self._start_teracopy_move_pairs(
                teracopy_pairs,
                completion_title="Move Result",
                on_finish=_on_finish,
            ):
                return
            return

        self.refresh_all()
        self._show_archive_move_report(report)
