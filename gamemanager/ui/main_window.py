from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import json
import os
from pathlib import Path
import shutil
import sys
import threading

from PySide6.QtCore import QEvent, QObject, QPoint, QProcess, QRect, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QKeyEvent, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLayout,
    QLayoutItem,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QComboBox,
    QSlider,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gamemanager.app_state import AppState
from gamemanager.models import InventoryItem, OperationReport, RootDisplayInfo
from gamemanager.services.icon_cache import icon_cache_key
from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services.background_removal import normalize_background_removal_engine
from gamemanager.services.teracopy import DEFAULT_TERACOPY_PATH, resolve_teracopy_path
from gamemanager.ui.dialogs import (
    CleanupPreviewDialog,
    DeleteGroupDialog,
    IconRebuildPreviewDialog,
    PerformanceSettingsDialog,
    PerformanceSettingsResult,
    IconProviderSettingsDialog,
    IconProviderSettingsResult,
    MovePreviewDialog,
    TagReviewDialog,
)
from gamemanager.ui.main_window_infotip_ops import MainWindowInfoTipOpsMixin
from gamemanager.ui.main_window_inventory_ops import MainWindowInventoryOpsMixin
from gamemanager.ui.main_window_icon_ops import MainWindowIconOpsMixin
from gamemanager.ui.main_window_operation_ops import MainWindowOperationOpsMixin
from gamemanager.ui.main_window_prewarm_ops import MainWindowPrewarmOpsMixin
from gamemanager.ui.main_window_refresh_ops import MainWindowRefreshOpsMixin
from gamemanager.ui.main_window_transfer_ops import MainWindowTransferOpsMixin
from gamemanager.ui.main_window_actions_ops import MainWindowActionsOpsMixin
from gamemanager.ui.alpha_preview import composite_on_checkerboard


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

RIGHT_VIEW_OPTIONS = {
    "List": "list",
    "Icons": "icons",
}

ICON_VIEW_SIZES = [16, 24, 32, 48, 64, 128, 256]
ENABLE_ICON_PATH_REPAIR_ACTION = True

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


class RefreshWorker(QObject):
    progress = Signal(str, int, int)
    completed = Signal(object, object)
    canceled = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, state: AppState):
        super().__init__()
        self._state = state
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            root_infos, inventory = self._state.refresh(
                progress_cb=lambda stage, current, total: self.progress.emit(
                    str(stage), int(current), int(total)
                ),
                should_cancel=lambda: self._cancel_event.is_set(),
            )
            self.completed.emit(root_infos, inventory)
        except OperationCancelled as exc:
            self.canceled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class ReportWorker(QObject):
    progress = Signal(str, int, int)
    completed = Signal(object)
    canceled = Signal(str)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        run_fn: Callable[
            [Callable[[str, int, int], None], Callable[[], bool]],
            OperationReport,
        ],
    ):
        super().__init__()
        self._run_fn = run_fn
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            report = self._run_fn(
                lambda stage, current, total: self.progress.emit(
                    str(stage), int(current), int(total)
                ),
                lambda: self._cancel_event.is_set(),
            )
            self.completed.emit(report)
        except OperationCancelled as exc:
            self.canceled.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class InfoTipBackfillWorker(QObject):
    progress = Signal(str, int, int)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, state: AppState, items: list[tuple[str, str]]):
        super().__init__()
        self._state = state
        self._items = list(items)
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        self._cancel_event.set()

    @Slot()
    def run(self) -> None:
        updated = 0
        failed = 0
        tips_by_path: dict[str, str] = {}
        total = len(self._items)
        try:
            for idx, (folder_path, cleaned_name) in enumerate(self._items, start=1):
                if self._cancel_event.is_set():
                    break
                try:
                    changed, tip = self._state.ensure_folder_info_tip(
                        folder_path,
                        cleaned_name,
                    )
                except Exception:
                    failed += 1
                    changed, tip = False, None
                if changed:
                    updated += 1
                if tip:
                    tips_by_path[os.path.normpath(folder_path)] = tip
                if idx == total or idx % 10 == 0:
                    self.progress.emit("InfoTip backfill", idx, total)
            self.completed.emit(
                {
                    "updated": updated,
                    "failed": failed,
                    "tips_by_path": tips_by_path,
                    "attempted": total,
                }
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class MainWindow(
    MainWindowActionsOpsMixin,
    MainWindowTransferOpsMixin,
    MainWindowPrewarmOpsMixin,
    MainWindowRefreshOpsMixin,
    MainWindowInventoryOpsMixin,
    MainWindowOperationOpsMixin,
    MainWindowInfoTipOpsMixin,
    MainWindowIconOpsMixin,
    QMainWindow,
):
    def __init__(self, state: AppState):
        super().__init__()
        self.state = state
        self.setWindowTitle("Game Backup Manager")
        self._left_sort_options = LEFT_SORT_OPTIONS
        self._left_label_options = LEFT_LABEL_OPTIONS
        self._right_columns = RIGHT_COLUMNS
        self._icon_view_sizes = ICON_VIEW_SIZES
        self._default_right_sort_chain = DEFAULT_RIGHT_SORT_CHAIN
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
        self._right_view_mode = "list"
        self._right_icon_size_index = ICON_VIEW_SIZES.index(64)
        self._teracopy_path_pref = DEFAULT_TERACOPY_PATH
        self._teracopy_executable: str | None = None
        self._teracopy_processes: dict[int, QProcess] = {}
        self._teracopy_job_meta: dict[int, tuple[int, str]] = {}
        self._teracopy_job_output: dict[int, str] = {}
        self._teracopy_temp_files: list[str] = []
        self._teracopy_completion_title = "Move Completed"
        self._teracopy_total_items = 0
        self._teracopy_succeeded_items = 0
        self._teracopy_failed_items = 0
        self._teracopy_failure_details: list[str] = []
        self._teracopy_finish_callback: Callable[[int, int, list[str]], None] | None = None
        self._teracopy_session_active = False
        self._refresh_thread: QThread | None = None
        self._refresh_worker: RefreshWorker | None = None
        self._refresh_in_progress = False
        self._refresh_queued = False
        self._infotip_backfill_thread: QThread | None = None
        self._infotip_backfill_worker: InfoTipBackfillWorker | None = None
        self._infotip_backfill_in_progress = False
        self._operation_thread: QThread | None = None
        self._operation_worker: ReportWorker | None = None
        self._operation_in_progress = False
        self._operation_title = ""
        self._operation_complete_handler: Callable[[OperationReport], None] | None = None
        self._report_worker_cls = ReportWorker
        self._refresh_worker_cls = RefreshWorker
        self._infotip_backfill_worker_cls = InfoTipBackfillWorker
        self._move_preview_dialog_cls = MovePreviewDialog
        self._icon_rebuild_preview_dialog_cls = IconRebuildPreviewDialog
        self._delete_group_dialog_cls = DeleteGroupDialog
        self._interactive_operation_active = False
        self._interactive_cancel_requested = False
        self._ml_prewarm_started = False
        self._prewarm_in_progress = False
        self._prewarm_process: QProcess | None = None
        self._prewarm_stdout_buffer = ""
        self._prewarm_stderr_buffer = ""
        self._prewarm_scheduled = False
        self._first_show_done = False
        self._initial_refresh_done = False
        self._prewarm_delay_timer = QTimer(self)
        self._prewarm_delay_timer.setSingleShot(True)
        self._prewarm_delay_timer.timeout.connect(self._start_ml_prewarm)
        self._gpu_status_process: QProcess | None = None
        self._gpu_status_stdout_buffer = ""
        self._gpu_status_stderr_buffer = ""
        self._gpu_status_update_pending = False
        self._folder_icon_cache: dict[str, QIcon] = {}
        self._folder_icon_preview_cache: dict[str, QPixmap] = {}
        self._icon_placeholder_cache: dict[int, QIcon] = {}
        self._right_hovered_icon_row: int | None = None
        self._icon_converter_dialog: IconConverterDialog | None = None
        self._template_prep_dialog: TemplatePrepDialog | None = None
        self._template_transparency_dialog: TemplateTransparencyDialog | None = None
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
        self.cancel_op_btn = QPushButton("Cancel Op")
        self.reset_sort_btn = QPushButton("Reset")
        self.show_duplicates_btn = QPushButton("Dupes")
        self.show_duplicates_btn.setCheckable(True)
        self.delete_selected_btn = QPushButton("Del Selected")
        self.cleanup_btn = QPushButton("Cleanup")
        self.tags_btn = QPushButton("Find Tags")
        self.move_btn = QPushButton("Move Arch")
        self.assign_icon_btn = QPushButton("Set Icon")
        self.icon_convert_btn = QPushButton("Ico Conv")
        self.template_prep_btn = QPushButton("PrepTpl")
        self.template_alpha_btn = QPushButton("Tpl Alpha")
        self.rebuild_icons_btn = QPushButton("Rebuild Ico")
        self.repair_icon_paths_btn = QPushButton("Fix IcoPath")
        self.icon_settings_btn = QPushButton("Icon Src")
        self.perf_btn = QPushButton("Optns")
        self.move_backend_combo = QComboBox(self)
        self.move_backend_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContents
        )
        self.move_backend_combo.addItems(MOVE_BACKEND_OPTIONS.keys())
        self.locate_teracopy_btn = QPushButton("Locate TC")
        self.move_backend_label = QLabel("Move:")

        self.add_root_btn.setToolTip("Add Root\nShortcut: Ctrl+N")
        self.remove_root_btn.setToolTip("Remove Selected Root\nShortcut: Ctrl+Shift+N")
        self.refresh_btn.setToolTip("Refresh\nShortcut: Ctrl+R")
        self.reset_sort_btn.setToolTip("Reset Sort\nShortcut: Alt+R")
        self.cancel_op_btn.setToolTip("Cancel Running Operation\nShortcut: Ctrl+Shift+R")
        self.show_duplicates_btn.setToolTip("Show Only Duplicates\nShortcut: Alt+D")
        self.delete_selected_btn.setToolTip("Delete Selected\nShortcut: Ctrl+Delete")
        self.cleanup_btn.setToolTip("Cleanup Names (Disk)\nShortcut: Ctrl+Shift+C")
        self.tags_btn.setToolTip("Find Tags\nShortcut: Ctrl+Shift+G")
        self.move_btn.setToolTip("Move ISO/Archives\nShortcut: Ctrl+Shift+M")
        self.assign_icon_btn.setToolTip("Assign Folder Icon...\nShortcut: Ctrl+I")
        self.icon_convert_btn.setToolTip("Image to Icon Converter...\nShortcut: Ctrl+Shift+I")
        self.template_prep_btn.setToolTip("Template Batch Prep...\nShortcut: Ctrl+T")
        self.template_alpha_btn.setToolTip("Template Transparency...\nShortcut: Ctrl+Shift+T")
        self.rebuild_icons_btn.setToolTip(
            "Rebuild existing local folder icons (uses desktop.ini Rebuilt flag)\nShortcut: Ctrl+Shift+B"
        )
        self.repair_icon_paths_btn.setToolTip(
            "Repair absolute/external desktop.ini icon paths to local folder icons\n"
            "Shortcut: Alt+X"
        )
        self.icon_settings_btn.setToolTip("Icon Provider Settings...\nShortcut: Alt+S")
        self.perf_btn.setToolTip("Options/Settings/Performance...\nShortcut: Alt+P")
        self.locate_teracopy_btn.setToolTip("Locate TeraCopy\nShortcut: Alt+L")

        compact_controls = [
            self.add_root_btn,
            self.remove_root_btn,
            self.refresh_btn,
            self.cancel_op_btn,
            self.reset_sort_btn,
            self.show_duplicates_btn,
            self.delete_selected_btn,
            self.cleanup_btn,
            self.tags_btn,
            self.move_btn,
            self.assign_icon_btn,
            self.icon_convert_btn,
            self.template_prep_btn,
            self.template_alpha_btn,
            self.rebuild_icons_btn,
            self.icon_settings_btn,
            self.perf_btn,
            self.move_backend_label,
            self.move_backend_combo,
            self.locate_teracopy_btn,
        ]
        if ENABLE_ICON_PATH_REPAIR_ACTION:
            compact_controls.insert(compact_controls.index(self.icon_settings_btn), self.repair_icon_paths_btn)
        for control in compact_controls:
            if isinstance(control, QPushButton):
                control.setMinimumWidth(0)
                control.setMinimumHeight(0)
                control.setStyleSheet(
                    "QPushButton { min-width: 0px; padding: 1px 6px; }"
                )
                control.setSizePolicy(
                    QSizePolicy.Policy.Minimum,
                    QSizePolicy.Policy.Fixed,
                )
            else:
                control.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

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

        icon_group_widgets: list[QWidget] = [
            self.assign_icon_btn,
            self.icon_convert_btn,
            self.template_prep_btn,
            self.template_alpha_btn,
            self.rebuild_icons_btn,
        ]
        if ENABLE_ICON_PATH_REPAIR_ACTION:
            icon_group_widgets.append(self.repair_icon_paths_btn)
        icon_group_widgets.append(self.icon_settings_btn)
        icon_group_widgets.append(self.perf_btn)

        top_groups = [
            _build_top_group(
                [self.add_root_btn, self.remove_root_btn, self.refresh_btn, self.cancel_op_btn]
            ),
            _build_top_group([self.reset_sort_btn, self.show_duplicates_btn]),
            _build_top_group(
                [self.delete_selected_btn, self.cleanup_btn, self.tags_btn, self.move_btn]
            ),
            _build_top_group(icon_group_widgets),
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

        self.selected_info_section = QFrame(self.left_panel)
        self.selected_info_section.setFrameShape(QFrame.Shape.StyledPanel)
        self.selected_info_section.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        selected_info_layout = QVBoxLayout(self.selected_info_section)
        selected_info_layout.setContentsMargins(8, 6, 8, 6)
        selected_info_layout.setSpacing(3)
        selected_info_layout.addWidget(
            QLabel("Selected Game Description:", self.selected_info_section)
        )
        self.selected_info_label = QLabel(
            "Select a game to view its one-line description.",
            self.selected_info_section,
        )
        self.selected_info_label.setWordWrap(True)
        self.selected_info_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        selected_info_layout.addWidget(self.selected_info_label)
        left_layout.addWidget(self.selected_info_section, 0)

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
        right_controls = QHBoxLayout()
        right_controls.addWidget(QLabel("View:"))
        self.right_view_combo = QComboBox(right_panel)
        self.right_view_combo.addItems(RIGHT_VIEW_OPTIONS.keys())
        right_controls.addWidget(self.right_view_combo)
        right_controls.addWidget(QLabel("Icon size:"))
        self.right_icon_size_slider = QSlider(Qt.Orientation.Horizontal, right_panel)
        self.right_icon_size_slider.setMinimum(0)
        self.right_icon_size_slider.setMaximum(len(ICON_VIEW_SIZES) - 1)
        self.right_icon_size_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.right_icon_size_slider.setTickInterval(1)
        self.right_icon_size_slider.setSingleStep(1)
        self.right_icon_size_slider.setPageStep(1)
        self.right_icon_size_slider.setValue(self._right_icon_size_index)
        self.right_icon_size_label = QLabel("", right_panel)
        right_controls.addWidget(self.right_icon_size_slider, 1)
        right_controls.addWidget(self.right_icon_size_label)
        right_layout.addLayout(right_controls)

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
        self.right_icon_list = QListWidget(right_panel)
        self.right_icon_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.right_icon_list.setMovement(QListWidget.Movement.Static)
        self.right_icon_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.right_icon_list.setWrapping(True)
        self.right_icon_list.setWordWrap(True)
        self.right_icon_list.setUniformItemSizes(False)
        self.right_icon_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.right_icon_list.setDragEnabled(True)
        self.right_icon_list.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self.right_icon_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.right_icon_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.right_icon_list.hide()
        right_layout.addWidget(self.right_table, 1)
        right_layout.addWidget(self.right_icon_list, 1)

        self.splitter.addWidget(self.left_panel)
        self.splitter.addWidget(right_panel)
        self.splitter.setSizes([300, 1000])

        self.setCentralWidget(root)
        self.statusBar().setStyleSheet("QStatusBar::item { border: none; }")
        self._status_left_label = QLabel("", self)
        self._status_left_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addWidget(self._status_left_label, 1)
        self._status_operation_sep = QFrame(self)
        self._status_operation_sep.setFrameShape(QFrame.Shape.VLine)
        self._status_operation_sep.setFrameShadow(QFrame.Shadow.Plain)
        self._status_operation_sep.setLineWidth(1)
        self._status_operation_sep.setMidLineWidth(0)
        self._status_operation_sep.setStyleSheet("color: rgba(220,220,220,110);")
        self.statusBar().addPermanentWidget(self._status_operation_sep, 0)
        self._status_operation_label = QLabel("", self)
        self._status_operation_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._status_operation_label.setMinimumWidth(220)
        self.statusBar().addPermanentWidget(self._status_operation_label, 0)

        self._status_background_sep = QFrame(self)
        self._status_background_sep.setFrameShape(QFrame.Shape.VLine)
        self._status_background_sep.setFrameShadow(QFrame.Shadow.Plain)
        self._status_background_sep.setLineWidth(1)
        self._status_background_sep.setMidLineWidth(0)
        self._status_background_sep.setStyleSheet("color: rgba(220,220,220,110);")
        self.statusBar().addPermanentWidget(self._status_background_sep, 0)
        self._status_background_label = QLabel("", self)
        self._status_background_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._status_background_label.setMinimumWidth(220)
        self.statusBar().addPermanentWidget(self._status_background_label, 0)

        self._status_gpu_sep = QFrame(self)
        self._status_gpu_sep.setFrameShape(QFrame.Shape.VLine)
        self._status_gpu_sep.setFrameShadow(QFrame.Shadow.Plain)
        self._status_gpu_sep.setLineWidth(1)
        self._status_gpu_sep.setMidLineWidth(0)
        self._status_gpu_sep.setStyleSheet("color: rgba(220,220,220,110);")
        self.statusBar().addPermanentWidget(self._status_gpu_sep, 0)
        self._status_gpu_label = QLabel("GPU: Checking...", self)
        self._status_gpu_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addPermanentWidget(self._status_gpu_label, 0)

        self._status_vram_sep = QFrame(self)
        self._status_vram_sep.setFrameShape(QFrame.Shape.VLine)
        self._status_vram_sep.setFrameShadow(QFrame.Shadow.Plain)
        self._status_vram_sep.setLineWidth(1)
        self._status_vram_sep.setMidLineWidth(0)
        self._status_vram_sep.setStyleSheet("color: rgba(220,220,220,110);")
        self.statusBar().addPermanentWidget(self._status_vram_sep, 0)
        self._status_vram_label = QLabel("", self)
        self._status_vram_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addPermanentWidget(self._status_vram_label, 0)

        self._status_selected_sep = QFrame(self)
        self._status_selected_sep.setFrameShape(QFrame.Shape.VLine)
        self._status_selected_sep.setFrameShadow(QFrame.Shadow.Plain)
        self._status_selected_sep.setLineWidth(1)
        self._status_selected_sep.setMidLineWidth(0)
        self._status_selected_sep.setStyleSheet("color: rgba(220,220,220,110);")
        self.statusBar().addPermanentWidget(self._status_selected_sep, 0)
        self._status_selected_label = QLabel("Selected: 0 games, 0 B", self)
        self._status_selected_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addPermanentWidget(self._status_selected_label, 0)
        self._refresh_status_section_visibility()
        self._wire_events()
        self._setup_shortcuts()
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._load_prefs()
        self._request_gpu_status_update()
        self._update_cancel_button_state()
        self.refresh_all()
        QTimer.singleShot(0, self._apply_initial_splitter_sizes)

    def _wire_events(self) -> None:
        self.add_root_btn.clicked.connect(self._on_add_root)
        self.remove_root_btn.clicked.connect(self._on_remove_root)
        self.refresh_btn.clicked.connect(self.refresh_all)
        self.cancel_op_btn.clicked.connect(self._on_cancel_operation)
        self.reset_sort_btn.clicked.connect(self._on_reset_right_sort)
        self.show_duplicates_btn.toggled.connect(self._on_toggle_show_duplicates)
        self.delete_selected_btn.clicked.connect(self._on_delete_selected_entries)
        self.cleanup_btn.clicked.connect(self._on_cleanup)
        self.tags_btn.clicked.connect(self._on_find_tags)
        self.move_btn.clicked.connect(self._on_move_archives)
        self.assign_icon_btn.clicked.connect(self._on_assign_folder_icon_selected)
        self.icon_convert_btn.clicked.connect(self._on_open_icon_converter)
        self.template_prep_btn.clicked.connect(self._on_open_template_prep)
        self.template_alpha_btn.clicked.connect(self._on_open_template_transparency)
        self.rebuild_icons_btn.clicked.connect(self._on_rebuild_existing_icons)
        if ENABLE_ICON_PATH_REPAIR_ACTION:
            self.repair_icon_paths_btn.clicked.connect(self._on_repair_absolute_icon_paths)
        self.icon_settings_btn.clicked.connect(self._on_icon_provider_settings)
        self.perf_btn.clicked.connect(self._on_open_performance_settings)
        self.move_backend_combo.currentTextChanged.connect(self._on_move_backend_changed)
        self.locate_teracopy_btn.clicked.connect(self._on_locate_teracopy)
        self.left_sort_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_label_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_filter_edit.textChanged.connect(self._on_left_filter_changed)
        self.right_view_combo.currentTextChanged.connect(self._on_right_view_changed)
        self.right_icon_size_slider.valueChanged.connect(self._on_right_icon_size_changed)
        self.left_table.itemSelectionChanged.connect(self._on_left_selection_changed)
        self.left_table.viewport().installEventFilter(self)
        self.right_table.viewport().installEventFilter(self)
        self.right_table.horizontalHeader().sectionClicked.connect(
            self._on_right_header_clicked
        )
        self.right_table.itemDoubleClicked.connect(self._on_right_item_double_clicked)
        self.right_table.customContextMenuRequested.connect(self._on_right_context_menu)
        self.right_table.itemSelectionChanged.connect(self._on_right_selection_changed)
        self.right_icon_list.itemDoubleClicked.connect(self._on_right_icon_item_double_clicked)
        self.right_icon_list.customContextMenuRequested.connect(
            self._on_right_icon_context_menu
        )
        self.right_icon_list.itemSelectionChanged.connect(self._on_right_selection_changed)

    def _register_shortcut_action(
        self,
        name: str,
        sequence: str,
        callback: Callable[[], None],
    ) -> QAction:
        action = QAction(name, self)
        action.setShortcut(QKeySequence(sequence))
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(callback)
        self.addAction(action)
        return action

    def _setup_shortcuts(self) -> None:
        self._shortcut_actions: dict[str, QAction] = {}
        mappings: list[tuple[str, str, Callable[[], None]]] = [
            ("Add Root", "Ctrl+N", self._on_add_root),
            ("Remove Root", "Ctrl+Shift+N", self._on_remove_root),
            ("Refresh", "Ctrl+R", self.refresh_all),
            ("Cancel Operation", "Ctrl+Shift+R", self._on_cancel_operation),
            ("Reset Sort", "Alt+R", self._on_reset_right_sort),
            ("Toggle Duplicates", "Alt+D", self._toggle_duplicates_shortcut),
            ("Delete Selected", "Ctrl+Delete", self._on_delete_selected_entries),
            ("Cleanup Names", "Ctrl+Shift+C", self._on_cleanup),
            ("Find Tags", "Ctrl+Shift+G", self._on_find_tags),
            ("Move Archives", "Ctrl+Shift+M", self._on_move_archives),
            ("Assign Folder Icon", "Ctrl+I", self._on_assign_folder_icon_selected),
            ("Search on Google", "Alt+G", self._on_search_selected_on_google),
            ("Icon Converter", "Ctrl+Shift+I", self._on_open_icon_converter),
            ("Template Batch Prep", "Ctrl+T", self._on_open_template_prep),
            ("Template Transparency", "Ctrl+Shift+T", self._on_open_template_transparency),
            ("Rebuild Existing Icons", "Ctrl+Shift+B", self._on_rebuild_existing_icons),
            ("Icon Provider Settings", "Alt+S", self._on_icon_provider_settings),
            ("Performance Settings", "Alt+P", self._on_open_performance_settings),
            ("Locate TeraCopy", "Alt+L", self._on_locate_teracopy),
            ("Refresh InfoTip", "Alt+I", self._on_refresh_selected_infotips),
            ("Manual InfoTip Entry", "Alt+E", self._on_edit_selected_infotip),
            ("Open Folder/Archive", "Ctrl+O", self._on_open_selected_in_explorer),
            ("Edit Name", "F2", self._on_manual_rename_selected_entry),
            ("Focus Filter", "Ctrl+F", self._focus_cleaned_name_filter),
            ("Show Shortcuts", "F1", self._show_shortcuts_help),
        ]
        if ENABLE_ICON_PATH_REPAIR_ACTION:
            mappings.insert(
                16,
                ("Repair Icon Paths", "Alt+X", self._on_repair_absolute_icon_paths),
            )
        for name, sequence, callback in mappings:
            self._shortcut_actions[name] = self._register_shortcut_action(
                name,
                sequence,
                callback,
            )

    def _toggle_duplicates_shortcut(self) -> None:
        self.show_duplicates_btn.setChecked(not self.show_duplicates_btn.isChecked())

    def _focus_cleaned_name_filter(self) -> None:
        self.left_filter_edit.setFocus()
        self.left_filter_edit.selectAll()

    def _show_shortcuts_help(self) -> None:
        lines = [
            "Main Shortcuts",
            "",
            "Ctrl+N - Add Root",
            "Ctrl+Shift+N - Remove Selected Root",
            "Ctrl+R - Refresh",
            "Ctrl+Shift+R - Cancel Operation",
            "Alt+R - Reset Sort",
            "Alt+D - Toggle Duplicates",
            "Ctrl+Delete - Delete Selected",
            "Ctrl+O - Open Folder/Archive",
            "F2 - Edit Name",
            "Ctrl+I - Assign Folder Icon",
            "Alt+G - Search on Google",
            "Ctrl+Shift+I - Icon Converter",
            "Ctrl+Shift+B - Rebuild Existing Icons",
            "Alt+I - Refresh InfoTip",
            "Alt+E - Manual InfoTip Entry",
            "Ctrl+T - Template Batch Prep",
            "Ctrl+Shift+T - Template Transparency",
            "Ctrl+Shift+C - Cleanup Names",
            "Ctrl+Shift+M - Move Archives",
            "Ctrl+Shift+G - Find Tags",
            "Alt+S - Icon Provider Settings",
            "Alt+P - Performance Settings",
            "Alt+L - Locate TeraCopy",
            "Ctrl+F - Focus Cleaned-Name Filter",
            "F1 - Show Shortcuts",
        ]
        if ENABLE_ICON_PATH_REPAIR_ACTION:
            lines.insert(-4, "Alt+X - Repair Icon Paths")
        QMessageBox.information(self, "Shortcuts", "\n".join(lines))

    def _load_prefs(self) -> None:
        sort_pref = self.state.get_ui_pref("left_sort", "source_label")
        label_pref = self.state.get_ui_pref("left_label_mode", "source")
        right_view_pref = self.state.get_ui_pref("right_view_mode", "list")
        right_icon_size_pref = self.state.get_ui_pref("right_icon_size", "64")
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
        if right_view_pref in RIGHT_VIEW_OPTIONS.values():
            self._right_view_mode = right_view_pref
        try:
            pref_px = int(right_icon_size_pref)
            if pref_px in ICON_VIEW_SIZES:
                self._right_icon_size_index = ICON_VIEW_SIZES.index(pref_px)
        except ValueError:
            pass
        for text, value in RIGHT_VIEW_OPTIONS.items():
            if value == self._right_view_mode:
                self.right_view_combo.setCurrentText(text)
                break
        self.right_icon_size_slider.setValue(self._right_icon_size_index)
        self._apply_right_view_mode_ui()
        self._apply_right_icon_size_ui()
        self._teracopy_executable = resolve_teracopy_path(self._teracopy_path_pref)
        self._update_move_backend_ui()

    def _request_gpu_status_update(self) -> None:
        if self._gpu_status_process is not None:
            self._gpu_status_update_pending = True
            return
        bg_engine = normalize_background_removal_engine(
            self.state.get_ui_pref("icon_bg_removal_engine", "none")
        )
        self._set_status_section_text(self._status_gpu_label, self._status_gpu_sep, "GPU: Checking...")
        self._set_status_section_text(self._status_vram_label, self._status_vram_sep, "")
        process = QProcess(self)
        process.setProgram(sys.executable)
        process.setArguments(
            [
                "-m",
                "gamemanager.services.gpu_status_subprocess",
                "--worker",
                "--bg-engine",
                bg_engine,
            ]
        )
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        self._gpu_status_stdout_buffer = ""
        self._gpu_status_stderr_buffer = ""
        process.readyReadStandardOutput.connect(self._on_gpu_status_stdout_ready)
        process.readyReadStandardError.connect(self._on_gpu_status_stderr_ready)
        process.errorOccurred.connect(self._on_gpu_status_process_error)
        process.finished.connect(self._on_gpu_status_process_finished)
        self._gpu_status_process = process
        process.start()

    def _on_gpu_status_stdout_ready(self) -> None:
        if self._gpu_status_process is None:
            return
        text = bytes(self._gpu_status_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not text:
            return
        self._gpu_status_stdout_buffer += text

    def _on_gpu_status_stderr_ready(self) -> None:
        if self._gpu_status_process is None:
            return
        text = bytes(self._gpu_status_process.readAllStandardError()).decode("utf-8", errors="replace")
        if text:
            self._gpu_status_stderr_buffer += text

    def _on_gpu_status_process_finished(self, _exit_code: int, _status: QProcess.ExitStatus) -> None:
        process = self._gpu_status_process
        self._gpu_status_process = None
        payload_text = (self._gpu_status_stdout_buffer or "").strip()
        self._gpu_status_stdout_buffer = ""
        err_text = (self._gpu_status_stderr_buffer or "").strip()
        self._gpu_status_stderr_buffer = ""
        if process is not None:
            process.deleteLater()
        parsed: dict[str, object] | None = None
        if payload_text:
            for line in reversed(payload_text.splitlines()):
                token = line.strip()
                if not token:
                    continue
                try:
                    parsed_obj = json.loads(token)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed_obj, dict):
                    parsed = parsed_obj
                    break
        if parsed and parsed.get("type") == "gpu_status":
            summary = str(parsed.get("summary") or "GPU: Unknown")
            vram_summary = str(parsed.get("vram_summary") or "")
            tooltip = str(parsed.get("tooltip") or "")
            self._set_status_section_text(self._status_gpu_label, self._status_gpu_sep, summary)
            self._set_status_section_text(self._status_vram_label, self._status_vram_sep, vram_summary)
            self._status_gpu_label.setToolTip(tooltip)
            self._status_vram_label.setToolTip(tooltip)
        elif parsed and parsed.get("type") == "error":
            message = str(parsed.get("message") or "status probe failed")
            self._set_status_section_text(self._status_gpu_label, self._status_gpu_sep, "GPU: Probe failed")
            self._set_status_section_text(self._status_vram_label, self._status_vram_sep, "")
            self._status_gpu_label.setToolTip(message)
            self._status_vram_label.setToolTip("")
        else:
            self._set_status_section_text(self._status_gpu_label, self._status_gpu_sep, "GPU: Probe failed")
            self._set_status_section_text(self._status_vram_label, self._status_vram_sep, "")
            self._status_gpu_label.setToolTip(err_text or payload_text or "Unknown probe error")
            self._status_vram_label.setToolTip("")
        if self._gpu_status_update_pending:
            self._gpu_status_update_pending = False
            QTimer.singleShot(0, self._request_gpu_status_update)

    def _on_gpu_status_process_error(self, _error: QProcess.ProcessError) -> None:
        # finished handler will finalize label state; nothing extra needed here.
        return

    def _on_left_pref_changed(self) -> None:
        sort_key = LEFT_SORT_OPTIONS[self.left_sort_combo.currentText()]
        label_mode = LEFT_LABEL_OPTIONS[self.left_label_combo.currentText()]
        self.state.set_ui_pref("left_sort", sort_key)
        self.state.set_ui_pref("left_label_mode", label_mode)
        self._populate_left(self.root_infos)
        self._populate_right(self.inventory)

    def _on_right_view_changed(self, text: str) -> None:
        self._right_view_mode = RIGHT_VIEW_OPTIONS.get(text, "list")
        self.state.set_ui_pref("right_view_mode", self._right_view_mode)
        self._apply_right_view_mode_ui()
        self._populate_right(self.inventory)

    def _on_right_icon_size_changed(self, index: int) -> None:
        if index < 0 or index >= len(ICON_VIEW_SIZES):
            return
        selected_paths: list[str] = []
        anchor_path: str | None = None
        if self._right_view_mode == "icons":
            selected_paths = [item.full_path for item in self._selected_right_entries()]
            current_item = self.right_icon_list.currentItem()
            if current_item is not None:
                current_row = self.right_icon_list.row(current_item)
                if 0 <= current_row < len(self._visible_right_items):
                    anchor_path = self._visible_right_items[current_row].full_path
        self._right_icon_size_index = index
        self.state.set_ui_pref("right_icon_size", str(ICON_VIEW_SIZES[index]))
        self._apply_right_icon_size_ui()
        if self._right_view_mode == "icons":
            self._populate_right(self.inventory)
            self._restore_right_icon_selection(selected_paths, anchor_path)

    def _apply_right_view_mode_ui(self) -> None:
        icon_mode = self._right_view_mode == "icons"
        self.right_table.setVisible(not icon_mode)
        self.right_icon_list.setVisible(icon_mode)
        self.right_icon_size_slider.setEnabled(icon_mode)
        self.right_icon_size_label.setEnabled(icon_mode)

    def _apply_right_icon_size_ui(self) -> None:
        px = ICON_VIEW_SIZES[self._right_icon_size_index]
        self.right_icon_size_label.setText(f"{px}px")
        self.right_icon_list.setIconSize(QSize(px, px))
        grid_w = max(110, px + 52)
        grid_h = px + 52
        self.right_icon_list.setGridSize(QSize(grid_w, grid_h))

    def _current_move_backend(self) -> str:
        return MOVE_BACKEND_OPTIONS.get(self.move_backend_combo.currentText(), "system")

    def _update_move_backend_ui(self) -> None:
        use_teracopy = self._current_move_backend() == "teracopy"
        self.locate_teracopy_btn.setEnabled(use_teracopy)
        if use_teracopy:
            path = resolve_teracopy_path(self._teracopy_path_pref)
            self._teracopy_executable = path
            if path:
                self.locate_teracopy_btn.setToolTip(
                    f"Locate TeraCopy\nShortcut: Alt+L\n{path}"
                )
            else:
                self.locate_teracopy_btn.setToolTip(
                    "Locate TeraCopy\nShortcut: Alt+L\n"
                    "TeraCopy not found. Click to auto-locate or choose manually."
                )
        else:
            self.locate_teracopy_btn.setToolTip("Locate TeraCopy\nShortcut: Alt+L")

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
                if event.source() in (self.right_table, self.right_icon_list):
                    idx = self.left_table.indexAt(event.pos())
                    if idx.isValid() and self._selected_right_entries():
                        event.acceptProposedAction()
                        return True
                event.ignore()
                return True
            if event.type() == QEvent.Type.Drop:
                if event.source() not in (self.right_table, self.right_icon_list):
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
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    index = self.right_table.indexAt(event.pos())
                    if index.isValid() and index.column() == 0:
                        row = index.row()
                        if 0 <= row < len(self._visible_right_items):
                            cell_rect = self.right_table.visualRect(index)
                            icon_hit = cell_rect.adjusted(
                                0, 0, -(cell_rect.width() - 26), 0
                            )
                            if icon_hit.contains(event.pos()):
                                entry = self._visible_right_items[row]
                                if entry.is_dir:
                                    self._hide_right_icon_hover_preview()
                                    self.right_table.clearSelection()
                                    self.right_table.selectRow(row)
                                    self._on_assign_folder_icon_selected()
                                    return True
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

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._refresh_worker is not None:
            self._refresh_worker.request_cancel()
        if self._operation_worker is not None:
            self._operation_worker.request_cancel()
        if self._infotip_backfill_worker is not None:
            self._infotip_backfill_worker.request_cancel()
        if self._prewarm_delay_timer.isActive():
            self._prewarm_delay_timer.stop()
        if self._prewarm_process is not None:
            self._prewarm_process.kill()
            self._prewarm_process = None
        if self._gpu_status_process is not None:
            self._gpu_status_process.kill()
            self._gpu_status_process = None
        if self._teracopy_processes:
            self._cancel_teracopy_session()
        super().closeEvent(event)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if not self._first_show_done:
            self._first_show_done = True
            self._schedule_startup_prewarm_if_ready()

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

    def _on_open_performance_settings(self) -> None:
        def _to_int(raw: str, default: int) -> int:
            try:
                return int(raw.strip())
            except Exception:
                return default

        initial = PerformanceSettingsResult(
            scan_size_workers=_to_int(
                self.state.get_ui_pref("perf_scan_size_workers", "0"),
                0,
            ),
            progress_interval_ms=_to_int(
                self.state.get_ui_pref("perf_progress_interval_ms", "50"),
                50,
            ),
            dir_cache_enabled=self.state.get_ui_pref("perf_dir_cache_enabled", "1") == "1",
            dir_cache_max_entries=_to_int(
                self.state.get_ui_pref("perf_dir_cache_max_entries", "200000"),
                200000,
            ),
            startup_prewarm_mode=self.state.get_ui_pref(
                "perf_startup_prewarm_mode", "minimal"
            ),
            web_capture_download_mode=self._load_web_capture_download_mode_pref(),
            web_capture_download_dir=self._load_web_capture_download_dir_pref(),
            icon_rebuild_create_backups=self.state.get_ui_pref(
                "icon_rebuild_create_backups",
                "1",
            )
            == "1",
            icon_rebuild_mode=self.state.get_ui_pref(
                "icon_rebuild_mode",
                "guided",
            ),
        )
        dialog = PerformanceSettingsDialog(
            initial,
            cleanup_backups_callback=self._on_clean_backup_icons_from_options,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload()
        self.state.set_ui_pref("perf_scan_size_workers", str(payload.scan_size_workers))
        self.state.set_ui_pref("perf_progress_interval_ms", str(payload.progress_interval_ms))
        self.state.set_ui_pref("perf_dir_cache_enabled", "1" if payload.dir_cache_enabled else "0")
        self.state.set_ui_pref("perf_dir_cache_max_entries", str(payload.dir_cache_max_entries))
        self.state.set_ui_pref("perf_startup_prewarm_mode", payload.startup_prewarm_mode)
        self._save_web_capture_download_mode_pref(payload.web_capture_download_mode)
        self._save_web_capture_download_dir_pref(payload.web_capture_download_dir)
        self.state.set_ui_pref(
            "icon_rebuild_create_backups",
            "1" if payload.icon_rebuild_create_backups else "0",
        )
        mode = str(payload.icon_rebuild_mode or "guided").strip().casefold()
        self.state.set_ui_pref(
            "icon_rebuild_mode",
            mode if mode in {"guided", "automatic"} else "guided",
        )
        QMessageBox.information(
            self,
            "Performance Settings",
            "Settings saved. Scan/cache settings apply on next refresh/operation. "
            "Startup preload mode applies on next app startup.",
        )

    def _on_clean_backup_icons_from_options(self) -> None:
        decision = QMessageBox.question(
            self,
            "Clean Backup Icons",
            (
                "Delete all backup icon files matching *.gm_backup_*.ico "
                "under configured roots?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if decision != QMessageBox.StandardButton.Yes:
            return
        report = self.state.clean_backup_icons()
        lines = [
            f"Deleted: {report.succeeded}",
            f"Failed: {report.failed}",
            f"Skipped roots: {report.skipped}",
        ]
        if report.details:
            lines.append("")
            lines.extend(report.details[:12])
        QMessageBox.information(self, "Clean Backup Icons", "\n".join(lines))

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

    def _icon_placeholder(self, size_px: int) -> QIcon:
        cached = self._icon_placeholder_cache.get(size_px)
        if cached is not None:
            return cached
        pix = QPixmap(size_px, size_px)
        pix.fill(Qt.GlobalColor.transparent)
        icon = QIcon(pix)
        self._icon_placeholder_cache[size_px] = icon
        return icon

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
        composed = composite_on_checkerboard(
            pix,
            width=256,
            height=256,
            keep_aspect=True,
        )
        self._folder_icon_preview_cache[cache_key] = composed
        return composed

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

    def _find_inventory_item_by_path(self, full_path: str) -> InventoryItem | None:
        norm = os.path.normcase(os.path.normpath(full_path))
        for item in self.inventory:
            if os.path.normcase(os.path.normpath(item.full_path)) == norm:
                return item
        return None

    def _status_base_text(self) -> str:
        active_roots = 1 if self._selected_root_id() is not None else len(self.root_infos)
        visible_games = len(self._visible_right_items)
        return f"Roots active: {active_roots}/{self._loaded_roots_count}    Games visible: {visible_games}/{self._loaded_entries_count}"

    def _set_status_section_text(self, label: QLabel, separator: QFrame, text: str) -> None:
        value = str(text or "").strip()
        label.setText(value)
        label.setVisible(bool(value))
        separator.setVisible(False)
        self._refresh_status_section_visibility()

    def _refresh_status_section_visibility(self) -> None:
        sections = [
            (self._status_operation_label, self._status_operation_sep),
            (self._status_background_label, self._status_background_sep),
            (self._status_gpu_label, self._status_gpu_sep),
            (self._status_vram_label, self._status_vram_sep),
            (self._status_selected_label, self._status_selected_sep),
        ]
        has_left = self._status_left_label.isVisible()
        seen_visible = False
        for label, separator in sections:
            visible = label.isVisible() and bool(label.text().strip())
            label.setVisible(visible)
            separator.setVisible(visible and (has_left or seen_visible))
            if visible:
                seen_visible = True

    def _update_counts_status(self) -> None:
        selected = self._selected_right_entries()
        selected_size = sum(item.size_bytes for item in selected)
        self._status_left_label.setText(self._status_base_text())
        self._set_status_section_text(
            self._status_selected_label,
            self._status_selected_sep,
            f"Selected: {len(selected)} games, {_format_bytes(selected_size)}",
        )
        self._update_selected_info_box()

    def _set_operation_progress(self, stage: str, current: int, total: int) -> None:
        current_i = max(0, int(current))
        total_i = max(0, int(total))
        if total_i > 0:
            current_i = min(current_i, total_i)
            self._set_status_section_text(
                self._status_operation_label,
                self._status_operation_sep,
                f"{stage}: {current_i}/{total_i}",
            )
            return
        self._set_status_section_text(
            self._status_operation_label,
            self._status_operation_sep,
            stage,
        )

    def _set_background_progress(self, stage: str, current: int, total: int) -> None:
        current_i = max(0, int(current))
        total_i = max(0, int(total))
        if total_i > 0:
            current_i = min(current_i, total_i)
            pct = int(round((current_i / total_i) * 100.0))
            self._set_status_section_text(
                self._status_background_label,
                self._status_background_sep,
                f"{stage}: {current_i}/{total_i} ({pct}%)",
            )
            return
        self._set_status_section_text(
            self._status_background_label,
            self._status_background_sep,
            stage,
        )

    def _clear_background_progress(self) -> None:
        self._set_status_section_text(self._status_background_label, self._status_background_sep, "")

    def _clear_operation_progress(self) -> None:
        self._set_status_section_text(self._status_operation_label, self._status_operation_sep, "")

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
        conflict_count = len([x for x in plan if x.status == "conflict"])

        def _run(progress_cb, should_cancel):
            return self.state.execute_cleanup_plan_with_progress(
                safe_items,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def _done(report: OperationReport) -> None:
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

        self._start_report_operation("Cleanup names", _run, _done)

