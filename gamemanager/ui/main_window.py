from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from urllib.parse import quote_plus
import webbrowser

from PySide6.QtCore import QEvent, QObject, QPoint, QProcess, QRect, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QIcon, QKeyEvent, QKeySequence, QPixmap
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
    QMenu,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gamemanager.app_state import AppState
from gamemanager.models import InventoryItem, MovePlanItem, OperationReport, RootDisplayInfo
from gamemanager.services.cancellation import OperationCancelled
from gamemanager.services.icon_cache import icon_cache_key
from gamemanager.services.icon_pipeline import (
    border_shader_to_dict,
    normalize_border_shader_config,
    normalize_icon_style,
)
from gamemanager.services.icon_sources import IconSearchSettings
from gamemanager.services.background_removal import normalize_background_removal_engine
from gamemanager.services.normalization import cleaned_name_from_full
from gamemanager.services.sorting import natural_key
from gamemanager.services.storage import mountpoint_sort_key
from gamemanager.services.teracopy import DEFAULT_TERACOPY_PATH, resolve_teracopy_path
from gamemanager.ui.dialogs import (
    CleanupPreviewDialog,
    DeleteGroupDialog,
    IconConverterDialog,
    IconPickerDialog,
    TemplatePrepDialog,
    PerformanceSettingsDialog,
    PerformanceSettingsResult,
    IconProviderSettingsDialog,
    IconProviderSettingsResult,
    MovePreviewDialog,
    TagReviewDialog,
    TemplateTransparencyDialog,
)
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
        self.repair_icon_paths_btn = QPushButton("Fix IcoPath")
        self.icon_settings_btn = QPushButton("Icon Src")
        self.perf_btn = QPushButton("Perf")
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
        self.repair_icon_paths_btn.setToolTip(
            "Repair absolute/external desktop.ini icon paths to local folder icons\n"
            "Shortcut: Alt+X"
        )
        self.icon_settings_btn.setToolTip("Icon Provider Settings...\nShortcut: Alt+S")
        self.perf_btn.setToolTip("Performance Settings...\nShortcut: Alt+P")
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
        self._status_left_label = QLabel("", self)
        self._status_left_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addWidget(self._status_left_label, 1)
        self._status_operation_label = QLabel("", self)
        self._status_operation_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._status_operation_label.setMinimumWidth(260)
        self.statusBar().addPermanentWidget(self._status_operation_label, 0)
        self._status_background_label = QLabel("", self)
        self._status_background_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self._status_background_label.setMinimumWidth(240)
        self.statusBar().addPermanentWidget(self._status_background_label, 0)
        self._status_gpu_label = QLabel("| GPU: Checking...", self)
        self._status_gpu_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addPermanentWidget(self._status_gpu_label, 0)
        self._status_selected_label = QLabel("| Selected: 0 games, 0 B", self)
        self._status_selected_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.statusBar().addPermanentWidget(self._status_selected_label, 0)
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

    def _start_ml_prewarm(self) -> None:
        self._prewarm_scheduled = False
        if self._ml_prewarm_started or self._prewarm_in_progress:
            return
        mode = self._startup_prewarm_mode()
        if mode == "off":
            self._ml_prewarm_started = True
            return
        resources = self._prewarm_resource_ids_for_mode(mode)
        if not resources:
            self._ml_prewarm_started = True
            return
        self._ml_prewarm_started = True
        self._prewarm_in_progress = True
        self._set_background_progress("Preload models", 0, len(resources))
        process = QProcess(self)
        process.setProgram(sys.executable)
        process.setArguments(
            [
                "-m",
                "gamemanager.services.prewarm_subprocess",
                "--worker",
                "--resources-json",
                json.dumps(resources, ensure_ascii=False),
            ]
        )
        process.setWorkingDirectory(str(Path(__file__).resolve().parents[2]))
        self._prewarm_stdout_buffer = ""
        self._prewarm_stderr_buffer = ""
        process.readyReadStandardOutput.connect(self._on_prewarm_stdout_ready)
        process.readyReadStandardError.connect(self._on_prewarm_stderr_ready)
        process.errorOccurred.connect(self._on_prewarm_process_error)
        process.finished.connect(self._on_prewarm_process_finished)
        self._prewarm_process = process
        process.start()

    def _startup_prewarm_mode(self) -> str:
        value = self.state.get_ui_pref("perf_startup_prewarm_mode", "minimal").strip().casefold()
        if value in {"off", "minimal", "full"}:
            return value
        return "minimal"

    def _prewarm_resource_ids_for_mode(self, mode: str) -> list[str]:
        mode_key = str(mode or "minimal").strip().casefold()
        if mode_key == "off":
            return []
        if mode_key == "full":
            return ["torch_runtime", "background_stack", "text_stack"]

        resources = ["torch_runtime"]
        bg_engine = normalize_background_removal_engine(
            self.state.get_ui_pref("icon_bg_removal_engine", "none")
        )
        if bg_engine == "rembg":
            resources.append("background_rembg")
        elif bg_engine == "bria_rmbg":
            resources.append("background_bria")

        text_method = self.state.get_ui_pref("icon_text_extraction_method", "none").strip().casefold()
        if text_method == "paddleocr":
            resources.append("text_paddle")
        elif text_method == "opencv_db":
            resources.append("text_opencv")

        seen: set[str] = set()
        ordered: list[str] = []
        for item in resources:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    def _schedule_startup_prewarm_if_ready(self) -> None:
        if self._ml_prewarm_started or self._prewarm_in_progress or self._prewarm_scheduled:
            return
        if not self._first_show_done or not self._initial_refresh_done:
            return
        if self._startup_prewarm_mode() == "off":
            self._ml_prewarm_started = True
            return
        self._prewarm_scheduled = True
        self._set_background_progress("Preload scheduled", 0, 1)
        self._prewarm_delay_timer.start(1500)

    def _on_prewarm_stdout_ready(self) -> None:
        if self._prewarm_process is None:
            return
        text = bytes(self._prewarm_process.readAllStandardOutput()).decode("utf-8", errors="replace")
        if not text:
            return
        self._prewarm_stdout_buffer += text
        lines = self._prewarm_stdout_buffer.splitlines()
        if self._prewarm_stdout_buffer and not self._prewarm_stdout_buffer.endswith(("\n", "\r")):
            self._prewarm_stdout_buffer = lines.pop() if lines else self._prewarm_stdout_buffer
        else:
            self._prewarm_stdout_buffer = ""
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = str(payload.get("type") or "")
            if kind == "progress":
                self._on_prewarm_progress(
                    str(payload.get("stage") or "Preload"),
                    int(payload.get("current") or 0),
                    int(payload.get("total") or 0),
                )
            elif kind == "done":
                self._on_prewarm_completed(str(payload.get("message") or "Preload complete"))
            elif kind == "error":
                self._on_prewarm_failed(str(payload.get("message") or "Preload failed"))
            elif kind == "warning":
                self._set_background_progress(str(payload.get("message") or "Preload warning"), 1, 1)

    def _on_prewarm_stderr_ready(self) -> None:
        if self._prewarm_process is None:
            return
        text = bytes(self._prewarm_process.readAllStandardError()).decode("utf-8", errors="replace")
        if text:
            self._prewarm_stderr_buffer += text

    def _on_prewarm_process_error(self, error: QProcess.ProcessError) -> None:
        if self._prewarm_process is None:
            return
        self._on_prewarm_failed(f"Preload process error: {int(error)}")
        self._on_prewarm_finished()

    def _on_prewarm_process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        if exit_code != 0 and self._prewarm_stderr_buffer.strip():
            self._on_prewarm_failed(self._prewarm_stderr_buffer.strip().splitlines()[-1])
        self._on_prewarm_finished()

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

    def _save_icon_style_pref(self, value: str) -> None:
        self.state.set_ui_pref("icon_style", value.strip() or "none")

    def _save_bg_engine_pref(self, value: str) -> None:
        normalized = normalize_background_removal_engine(value)
        self.state.set_ui_pref("icon_bg_removal_engine", normalized)
        self._request_gpu_status_update()

    def _request_gpu_status_update(self) -> None:
        if self._gpu_status_process is not None:
            self._gpu_status_update_pending = True
            return
        bg_engine = normalize_background_removal_engine(
            self.state.get_ui_pref("icon_bg_removal_engine", "none")
        )
        self._status_gpu_label.setText("| GPU: Checking...")
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
            summary = str(parsed.get("summary") or "| GPU: Unknown")
            tooltip = str(parsed.get("tooltip") or "")
            self._status_gpu_label.setText(summary)
            self._status_gpu_label.setToolTip(tooltip)
        elif parsed and parsed.get("type") == "error":
            message = str(parsed.get("message") or "status probe failed")
            self._status_gpu_label.setText("| GPU: Probe failed")
            self._status_gpu_label.setToolTip(message)
        else:
            self._status_gpu_label.setText("| GPU: Probe failed")
            self._status_gpu_label.setToolTip(err_text or payload_text or "Unknown probe error")
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

    def _on_right_item_double_clicked(self, table_item: QTableWidgetItem) -> None:
        row = table_item.row()
        if row < 0 or row >= len(self._visible_right_items):
            return
        entry = self._visible_right_items[row]
        self._open_in_explorer(entry.full_path)

    def _on_right_icon_item_double_clicked(self, list_item: QListWidgetItem) -> None:
        row = self.right_icon_list.row(list_item)
        if row < 0 or row >= len(self._visible_right_items):
            return
        self.right_icon_list.clearSelection()
        list_item.setSelected(True)
        self._on_assign_folder_icon_selected()

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
        open_action = QAction("Open Folder/Archive\tCtrl+O", menu)
        open_action.triggered.connect(self._on_open_selected_in_explorer)
        menu.addAction(open_action)
        menu.addSeparator()
        assign_icon_action = QAction("Assign Folder Icon...\tCtrl+I", menu)
        assign_icon_action.triggered.connect(self._on_assign_folder_icon_selected)
        menu.addAction(assign_icon_action)
        search_google_action = QAction("Search on Google\tAlt+G", menu)
        search_google_action.triggered.connect(self._on_search_selected_on_google)
        menu.addAction(search_google_action)
        refresh_tip_action = QAction("Refresh InfoTip\tAlt+I", menu)
        refresh_tip_action.triggered.connect(self._on_refresh_selected_infotips)
        menu.addAction(refresh_tip_action)
        edit_tip_action = QAction("Manual InfoTip Entry...\tAlt+E", menu)
        edit_tip_action.triggered.connect(self._on_edit_selected_infotip)
        menu.addAction(edit_tip_action)
        rename_action = QAction("Edit Name...\tF2", menu)
        rename_action.triggered.connect(self._on_manual_rename_selected_entry)
        menu.addAction(rename_action)
        delete_action = QAction("Delete Selected\tCtrl+Delete", menu)
        delete_action.triggered.connect(self._on_delete_selected_entries)
        menu.addAction(delete_action)
        menu.exec(self.right_table.viewport().mapToGlobal(pos))

    def _on_right_icon_context_menu(self, pos) -> None:
        index = self.right_icon_list.indexAt(pos)
        if index.isValid():
            item = self.right_icon_list.item(index.row())
            if item is not None and not item.isSelected():
                self.right_icon_list.clearSelection()
                item.setSelected(True)

        menu = QMenu(self.right_icon_list)
        open_action = QAction("Open Folder/Archive\tCtrl+O", menu)
        open_action.triggered.connect(self._on_open_selected_in_explorer)
        menu.addAction(open_action)
        menu.addSeparator()
        assign_icon_action = QAction("Assign Folder Icon...\tCtrl+I", menu)
        assign_icon_action.triggered.connect(self._on_assign_folder_icon_selected)
        menu.addAction(assign_icon_action)
        search_google_action = QAction("Search on Google\tAlt+G", menu)
        search_google_action.triggered.connect(self._on_search_selected_on_google)
        menu.addAction(search_google_action)
        refresh_tip_action = QAction("Refresh InfoTip\tAlt+I", menu)
        refresh_tip_action.triggered.connect(self._on_refresh_selected_infotips)
        menu.addAction(refresh_tip_action)
        edit_tip_action = QAction("Manual InfoTip Entry...\tAlt+E", menu)
        edit_tip_action.triggered.connect(self._on_edit_selected_infotip)
        menu.addAction(edit_tip_action)
        rename_action = QAction("Edit Name...\tF2", menu)
        rename_action.triggered.connect(self._on_manual_rename_selected_entry)
        menu.addAction(rename_action)
        delete_action = QAction("Delete Selected\tCtrl+Delete", menu)
        delete_action.triggered.connect(self._on_delete_selected_entries)
        menu.addAction(delete_action)
        menu.exec(self.right_icon_list.viewport().mapToGlobal(pos))

    def _selected_right_entries(self) -> list[InventoryItem]:
        rows: list[int] = []
        if self._right_view_mode == "icons":
            rows = sorted(
                {
                    self.right_icon_list.row(item)
                    for item in self.right_icon_list.selectedItems()
                }
            )
        else:
            model = self.right_table.selectionModel()
            if model is None:
                return []
            rows = sorted({idx.row() for idx in model.selectedRows()})
        return [
            self._visible_right_items[row]
            for row in rows
            if 0 <= row < len(self._visible_right_items)
        ]

    def _on_open_selected_in_explorer(self) -> None:
        selected = self._selected_right_entries()
        if not selected:
            QMessageBox.information(
                self,
                "Open Folder/Archive",
                "Select at least one game entry in the right pane.",
            )
            return
        # Open only the first selected entry to avoid unintentionally spawning
        # many Explorer windows.
        self._open_in_explorer(selected[0].full_path)

    def _on_search_selected_on_google(self) -> None:
        selected = self._selected_right_entries()
        if not selected:
            QMessageBox.information(
                self,
                "Search on Google",
                "Select at least one game entry in the right pane.",
            )
            return
        entry = selected[0]
        query = f"{(entry.cleaned_name.strip() or entry.full_name.strip() or 'game')} game"
        url = f"https://www.google.com/search?q={quote_plus(query)}"
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Search on Google",
                f"Could not open browser for query:\n{query}\n\n{exc}",
            )

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
            if self._teracopy_processes:
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

        def _run(progress_cb, should_cancel):
            report = OperationReport(total=len(move_pairs))
            total = len(move_pairs)
            if progress_cb is not None:
                progress_cb("Move selected games", 0, total)
            for idx, (src, dst) in enumerate(move_pairs, start=1):
                if should_cancel():
                    raise OperationCancelled("Move selected games canceled")
                try:
                    shutil.move(src, dst)
                    report.succeeded += 1
                except OSError as exc:
                    report.failed += 1
                    report.details.append(f"{src} -> {dst}: {exc}")
                if progress_cb is not None:
                    progress_cb("Move selected games", idx, total)
            return report

        def _done(report: OperationReport) -> None:
            self.refresh_all()
            if report.failed:
                details = "\n".join(report.details[:8])
                QMessageBox.warning(
                    self,
                    "Move Completed with Errors",
                    f"Moved: {report.succeeded}\nFailed: {report.failed}\n\n{details}",
                )
                return
            QMessageBox.information(self, "Move Completed", f"Moved: {report.succeeded}")

        self._start_report_operation("Move selected games", _run, _done)

    def _set_move_controls_busy(self, busy: bool) -> None:
        self.move_btn.setEnabled(not busy)
        self.move_backend_combo.setEnabled(not busy)
        self.locate_teracopy_btn.setEnabled(
            (not busy) and self._current_move_backend() == "teracopy"
        )
        self.right_table.setDragEnabled(not busy)
        self.right_icon_list.setDragEnabled(not busy)
        self._update_cancel_button_state()

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
        if self._teracopy_processes:
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

        batches: list[tuple[str, str, int]] = []
        self._teracopy_temp_files = []
        self._teracopy_total_items = 0
        self._teracopy_succeeded_items = 0
        self._teracopy_failed_items = len(unsupported)
        self._teracopy_completion_title = completion_title
        self._teracopy_finish_callback = on_finish
        self._teracopy_session_active = True
        self._teracopy_failure_details = [
            f"Fallback needed (renamed destination): {src} -> {dst}"
            for src, dst in unsupported
        ]
        self._teracopy_job_meta.clear()
        self._teracopy_job_output.clear()

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
            batches.append((list_path, target_dir, len(sources)))
            self._teracopy_total_items += len(sources)

        if not batches:
            self._cleanup_teracopy_temp_files()
            self._set_operation_progress("TeraCopy move", 0, 0)
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
        self._set_operation_progress("TeraCopy move", 0, self._teracopy_total_items)
        for list_path, target_dir, batch_size in batches:
            self._start_teracopy_batch(list_path, target_dir, batch_size)
        return True

    def _start_teracopy_batch(self, list_path: str, target_dir: str, batch_size: int) -> None:
        proc = QProcess(self)
        pid = id(proc)
        self._teracopy_processes[pid] = proc
        self._teracopy_job_meta[pid] = (batch_size, target_dir)
        self._teracopy_job_output[pid] = ""
        proc.readyReadStandardOutput.connect(lambda p=proc: self._on_teracopy_ready_read(p))
        proc.readyReadStandardError.connect(lambda p=proc: self._on_teracopy_ready_read(p))
        proc.errorOccurred.connect(lambda err, p=proc: self._on_teracopy_error(p, err))
        proc.finished.connect(
            lambda code, status, p=proc: self._on_teracopy_finished(p, code, status)
        )
        proc.setProgram(self._teracopy_executable or "")
        proc.setArguments(["Move", f"*{list_path}", target_dir, "/Close"])
        proc.start()
        self._update_cancel_button_state()

    def _on_teracopy_ready_read(self, proc: QProcess) -> None:
        pid = id(proc)
        if pid not in self._teracopy_processes:
            return
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="ignore")
        err = bytes(proc.readAllStandardError()).decode("utf-8", errors="ignore")
        chunk = (out + "\n" + err).strip()
        if chunk:
            previous = self._teracopy_job_output.get(pid, "")
            self._teracopy_job_output[pid] = f"{previous}\n{chunk}".strip()

    def _on_teracopy_error(self, proc: QProcess, process_error: QProcess.ProcessError) -> None:
        pid = id(proc)
        _batch_size, target = self._teracopy_job_meta.get(pid, (0, "?"))
        self._teracopy_failure_details.append(
            f"TeraCopy process error {int(process_error)} for {target}"
        )

    def _on_teracopy_finished(
        self, proc: QProcess, exit_code: int, exit_status: QProcess.ExitStatus
    ) -> None:
        pid = id(proc)
        batch_size, target = self._teracopy_job_meta.get(pid, (0, "?"))
        if exit_status == QProcess.ExitStatus.NormalExit and exit_code == 0:
            self._teracopy_succeeded_items += batch_size
        else:
            self._teracopy_failed_items += batch_size
            details = self._teracopy_job_output.get(pid, "")
            if details:
                details = details.splitlines()[-1].strip()
            if not details:
                details = f"exit_code={exit_code}"
            self._teracopy_failure_details.append(
                f"TeraCopy failed for {target}: {details}"
            )
        self._teracopy_job_output.pop(pid, None)
        self._teracopy_job_meta.pop(pid, None)
        self._teracopy_processes.pop(pid, None)
        proc.deleteLater()
        self._update_cancel_button_state()

        done = self._teracopy_succeeded_items + self._teracopy_failed_items
        self._set_operation_progress(
            "TeraCopy move",
            done,
            max(1, self._teracopy_total_items),
        )
        if self._teracopy_session_active and not self._teracopy_processes:
            self._finish_teracopy_session()

    def _cleanup_teracopy_temp_files(self) -> None:
        for path in self._teracopy_temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._teracopy_temp_files.clear()

    def _finish_teracopy_session(self) -> None:
        if not self._teracopy_session_active:
            return
        self._teracopy_session_active = False
        self._cleanup_teracopy_temp_files()
        self._set_move_controls_busy(False)
        succeeded = self._teracopy_succeeded_items
        failed = self._teracopy_failed_items
        details = list(self._teracopy_failure_details)
        callback = self._teracopy_finish_callback
        self._teracopy_finish_callback = None
        done = succeeded + failed
        self._set_operation_progress(
            "TeraCopy move",
            done,
            max(1, self._teracopy_total_items),
        )
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

    def _cancel_teracopy_session(self) -> None:
        if not self._teracopy_session_active:
            return
        processes = list(self._teracopy_processes.values())
        for proc in processes:
            try:
                proc.kill()
            except Exception:
                continue
        # Count any still-running jobs as failed; finished handlers will remove maps.
        remaining_items = sum(batch for batch, _target in self._teracopy_job_meta.values())
        if remaining_items > 0:
            self._teracopy_failed_items += remaining_items
            self._teracopy_failure_details.append("Canceled by user.")
        self._teracopy_job_meta.clear()
        self._teracopy_job_output.clear()
        self._teracopy_processes.clear()
        self._update_cancel_button_state()
        self._finish_teracopy_session()

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
                QMessageBox.information(self, "InfoTip Refresh", "\n".join(lines))
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
            for col in range(len(RIGHT_COLUMNS)):
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
        if entry.is_dir and entry.icon_status == "valid":
            renamed_cleaned = cleaned_name_from_full(
                full_name=new_name,
                is_file=False,
                approved_tags=self.state.approved_tags(),
            )
            started = self._run_infotip_refresh_operation(
                [(new_path, renamed_cleaned)],
                title="Refresh renamed InfoTip",
                show_summary=False,
            )
            if not started:
                self.refresh_all()
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
        )
        dialog = PerformanceSettingsDialog(initial, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        payload = dialog.result_payload()
        self.state.set_ui_pref("perf_scan_size_workers", str(payload.scan_size_workers))
        self.state.set_ui_pref("perf_progress_interval_ms", str(payload.progress_interval_ms))
        self.state.set_ui_pref("perf_dir_cache_enabled", "1" if payload.dir_cache_enabled else "0")
        self.state.set_ui_pref("perf_dir_cache_max_entries", str(payload.dir_cache_max_entries))
        self.state.set_ui_pref("perf_startup_prewarm_mode", payload.startup_prewarm_mode)
        QMessageBox.information(
            self,
            "Performance Settings",
            "Settings saved. Scan/cache settings apply on next refresh/operation. "
            "Startup preload mode applies on next app startup.",
        )

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

    def _apply_icon_result_to_entry(
        self,
        entry: InventoryItem,
        ico_path: str | None,
        desktop_ini_path: str | None,
        info_tip: str | None = None,
    ) -> None:
        target = self._find_inventory_item_by_path(entry.full_path) or entry
        target.icon_status = "valid"
        target.folder_icon_path = os.path.normpath(ico_path) if ico_path else target.folder_icon_path
        target.desktop_ini_path = os.path.normpath(desktop_ini_path) if desktop_ini_path else target.desktop_ini_path
        if info_tip is not None and info_tip.strip():
            target.info_tip = info_tip.strip()
        try:
            stat = Path(target.full_path).stat()
            target.modified_at = datetime.fromtimestamp(stat.st_mtime)
            # Preserve current aggregate size value and pin it to new dir mtime in cache.
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
        # Keep single-item flow quiet and also avoid a no-op popup on full cancel.
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
        deleted_paths: set[str] = set()
        failed: list[str] = []
        sorted_paths = sorted(final_delete_paths)
        self._begin_interactive_operation("Delete selected", len(sorted_paths))
        canceled_by_user = False
        try:
            for idx, full_path in enumerate(sorted_paths, start=1):
                if self._step_interactive_operation("Delete selected", idx - 1, len(sorted_paths)):
                    canceled_by_user = True
                    break
                try:
                    self._delete_path(full_path)
                    deleted += 1
                    deleted_paths.add(full_path)
                except OSError as exc:
                    elevated_ok, elevated_err = self._delete_path_with_elevation(full_path)
                    if elevated_ok:
                        deleted += 1
                        deleted_paths.add(full_path)
                        continue
                    details = f"{full_path}: {exc}"
                    if elevated_err:
                        details += f" | elevated retry failed: {elevated_err}"
                    failed.append(details)
        finally:
            self._end_interactive_operation()

        if deleted > 0:
            deleted_norm = {
                os.path.normcase(os.path.normpath(path))
                for path in deleted_paths
            }
            self.inventory = [
                item
                for item in self.inventory
                if os.path.normcase(os.path.normpath(item.full_path)) not in deleted_norm
            ]
            self._loaded_entries_count = len(self.inventory)
            self._populate_right(self.inventory)
            self._mark_refresh_needed(True)
        if failed:
            details = "\n".join(failed[:8])
            QMessageBox.warning(
                self,
                "Deletion Completed with Errors",
                f"Deleted: {deleted}\nFailed: {len(failed)}\n\n{details}",
            )
            return
        if canceled_by_user:
            QMessageBox.information(
                self,
                "Deletion Canceled",
                f"Deleted before cancel: {deleted}",
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

    def _entry_tooltip_text(self, entry: InventoryItem, row_source: str) -> str:
        lines = [
            f"Name: {entry.full_name}",
            f"Cleaned: {entry.cleaned_name}",
            f"Path: {entry.full_path}",
            f"Source: {row_source}",
        ]
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

    def _update_counts_status(self) -> None:
        selected = self._selected_right_entries()
        selected_size = sum(item.size_bytes for item in selected)
        self._status_left_label.setText(self._status_base_text())
        self._status_selected_label.setText(
            f"| Selected: {len(selected)} games, {_format_bytes(selected_size)}"
        )
        self._update_selected_info_box()

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
        worker = RefreshWorker(self.state)
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

    def _set_operation_progress(self, stage: str, current: int, total: int) -> None:
        current_i = max(0, int(current))
        total_i = max(0, int(total))
        if total_i > 0:
            current_i = min(current_i, total_i)
            self._status_operation_label.setText(f"| {stage}: {current_i}/{total_i}")
            return
        self._status_operation_label.setText(f"| {stage}")

    def _set_background_progress(self, stage: str, current: int, total: int) -> None:
        current_i = max(0, int(current))
        total_i = max(0, int(total))
        if total_i > 0:
            current_i = min(current_i, total_i)
            pct = int(round((current_i / total_i) * 100.0))
            self._status_background_label.setText(
                f"| {stage}: {current_i}/{total_i} ({pct}%)"
            )
            return
        self._status_background_label.setText(f"| {stage}")

    def _clear_background_progress(self) -> None:
        self._status_background_label.setText("")

    def _clear_operation_progress(self) -> None:
        self._status_operation_label.setText("")

    def _on_refresh_progress(self, stage: str, current: int, total: int) -> None:
        self._set_operation_progress(stage, current, total)

    def _on_prewarm_progress(self, stage: str, current: int, total: int) -> None:
        self._set_background_progress(stage, current, total)

    def _on_prewarm_completed(self, message: str) -> None:
        msg = message.strip() or "Preload complete"
        self._set_background_progress(msg, 1, 1)

    def _on_prewarm_failed(self, message: str) -> None:
        err = message.strip() or "Preload failed"
        self._set_background_progress(f"{err}", 1, 1)

    def _on_prewarm_finished(self) -> None:
        if self._prewarm_process is not None:
            self._prewarm_process.deleteLater()
        self._prewarm_process = None
        self._prewarm_in_progress = False
        self._request_gpu_status_update()
        QTimer.singleShot(2000, self._clear_background_progress)

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
        worker = InfoTipBackfillWorker(self.state, candidates)
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

    def _set_background_operation_busy(self, busy: bool) -> None:
        self.cleanup_btn.setEnabled(not busy)
        self.move_btn.setEnabled(not busy)
        self.add_root_btn.setEnabled(not busy)
        self.remove_root_btn.setEnabled(not busy)
        self.tags_btn.setEnabled(not busy)
        self.refresh_btn.setEnabled((not busy) and (not self._refresh_in_progress))
        self._update_cancel_button_state()

    def _start_report_operation(
        self,
        title: str,
        run_fn: Callable[
            [Callable[[str, int, int], None], Callable[[], bool]],
            OperationReport,
        ],
        on_complete: Callable[[OperationReport], None],
    ) -> bool:
        if self._operation_in_progress:
            QMessageBox.information(
                self,
                "Operation In Progress",
                "Wait for the current operation to finish first.",
            )
            return False
        if self._refresh_in_progress:
            QMessageBox.information(
                self,
                "Refresh In Progress",
                "Wait for the current refresh to finish first.",
            )
            return False
        if self._teracopy_processes:
            QMessageBox.information(
                self,
                "Move In Progress",
                "Wait for the current TeraCopy operation to finish first.",
            )
            return False

        self._operation_in_progress = True
        self._operation_title = title.strip() or "Operation"
        self._operation_complete_handler = on_complete
        self._set_background_operation_busy(True)
        self._set_operation_progress(self._operation_title, 0, 1)

        thread = QThread(self)
        worker = ReportWorker(run_fn)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_operation_progress)
        worker.completed.connect(self._on_operation_completed)
        worker.canceled.connect(self._on_operation_canceled)
        worker.failed.connect(self._on_operation_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_operation_finished)
        self._operation_thread = thread
        self._operation_worker = worker
        thread.start()
        return True

    def _on_operation_progress(self, stage: str, current: int, total: int) -> None:
        label = stage.strip() or self._operation_title or "Operation"
        self._set_operation_progress(label, current, total)

    def _on_operation_completed(self, report_obj: object) -> None:
        report = report_obj if isinstance(report_obj, OperationReport) else OperationReport()
        handler = self._operation_complete_handler
        if handler is not None:
            handler(report)
        self._set_operation_progress(self._operation_title or "Operation complete", 1, 1)

    def _on_operation_failed(self, message: str) -> None:
        err = message.strip() or "Unknown operation error."
        QMessageBox.warning(self, "Operation Failed", err)
        self._set_operation_progress(
            f"{self._operation_title or 'Operation'} failed", 1, 1
        )

    def _on_operation_canceled(self, message: str) -> None:
        msg = message.strip() or f"{self._operation_title or 'Operation'} canceled"
        self._set_operation_progress(msg, 1, 1)

    def _on_operation_finished(self) -> None:
        self._operation_thread = None
        self._operation_worker = None
        self._operation_complete_handler = None
        self._operation_in_progress = False
        self._operation_title = ""
        self._set_background_operation_busy(False)
        if (
            not self._refresh_in_progress
            and not self._interactive_operation_active
            and not self._teracopy_processes
        ):
            self._clear_operation_progress()

    def _update_cancel_button_state(self) -> None:
        can_cancel = (
            self._refresh_in_progress
            or self._operation_in_progress
            or self._interactive_operation_active
            or bool(self._teracopy_processes)
        )
        self.cancel_op_btn.setEnabled(can_cancel)

    def _on_cancel_operation(self) -> None:
        if self._refresh_in_progress and self._refresh_worker is not None:
            self._refresh_worker.request_cancel()
            self._set_operation_progress("Canceling refresh", 0, 1)
            self._update_cancel_button_state()
            return
        if self._operation_in_progress and self._operation_worker is not None:
            self._operation_worker.request_cancel()
            self._set_operation_progress("Canceling operation", 0, 1)
            self._update_cancel_button_state()
            return
        if self._interactive_operation_active:
            self._interactive_cancel_requested = True
            self._set_operation_progress("Cancel requested", 0, 1)
            self._update_cancel_button_state()
            return
        if self._teracopy_processes:
            self._cancel_teracopy_session()
            self._update_cancel_button_state()

    def _begin_interactive_operation(self, title: str, total: int) -> None:
        self._interactive_operation_active = True
        self._interactive_cancel_requested = False
        self._set_operation_progress(title, 0, max(1, total))
        self._update_cancel_button_state()

    def _step_interactive_operation(self, title: str, current: int, total: int) -> bool:
        self._set_operation_progress(title, current, max(1, total))
        QApplication.processEvents()
        return self._interactive_cancel_requested

    def _end_interactive_operation(self) -> None:
        self._interactive_operation_active = False
        self._interactive_cancel_requested = False
        self._update_cancel_button_state()
        if not self._refresh_in_progress and not self._operation_in_progress and not self._teracopy_processes:
            self._clear_operation_progress()

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
            self.refresh_btn.setToolTip("Manual refresh required.\nShortcut: Ctrl+R")
            return
        self.refresh_btn.setStyleSheet("")
        self.refresh_btn.setToolTip("Refresh\nShortcut: Ctrl+R")

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
        self.left_table.setUpdatesEnabled(False)
        try:
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
        finally:
            self.left_table.setUpdatesEnabled(True)

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
        icon_px = ICON_VIEW_SIZES[self._right_icon_size_index]
        self._visible_right_items = sorted_items
        self.right_table.setUpdatesEnabled(False)
        self.right_icon_list.setUpdatesEnabled(False)
        try:
            self.right_table.setRowCount(len(sorted_items))
            self.right_icon_list.clear()
            for row, entry in enumerate(sorted_items):
                row_icon = self._icon_for_entry(entry)
                row_source = self._source_for_item(entry, root_info_by_id)
                name_item = QTableWidgetItem(entry.full_name)
                name_item.setIcon(row_icon)
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
                    row, 5, QTableWidgetItem(row_source)
                )
                tooltip = self._entry_tooltip_text(entry, row_source)
                for col in range(len(RIGHT_COLUMNS)):
                    cell = self.right_table.item(row, col)
                    if cell is not None:
                        cell.setToolTip(tooltip)

                tile_text = entry.cleaned_name.strip() or entry.full_name
                tile_icon = row_icon
                if tile_icon.cacheKey() == self._blank_icon.cacheKey():
                    tile_icon = self._icon_placeholder(icon_px)
                tile = QListWidgetItem(tile_icon, tile_text)
                tile.setToolTip(tooltip)
                tile.setData(Qt.ItemDataRole.UserRole, row)
                tile.setTextAlignment(
                    int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
                )
                self.right_icon_list.addItem(tile)

            self.right_table.resizeColumnsToContents()
        finally:
            self.right_table.setUpdatesEnabled(True)
            self.right_icon_list.setUpdatesEnabled(True)
        self._apply_right_icon_size_ui()
        self._update_counts_status()

    def _restore_right_icon_selection(
        self,
        selected_paths: list[str],
        anchor_path: str | None = None,
    ) -> None:
        if self._right_view_mode != "icons" or not selected_paths:
            return
        path_to_rows: dict[str, list[int]] = {}
        for row, entry in enumerate(self._visible_right_items):
            path_to_rows.setdefault(entry.full_path, []).append(row)
        rows_to_select: list[int] = []
        for path in selected_paths:
            rows_to_select.extend(path_to_rows.get(path, []))
        if not rows_to_select:
            return
        rows_to_select = sorted(set(rows_to_select))
        blocked = self.right_icon_list.blockSignals(True)
        try:
            self.right_icon_list.clearSelection()
            for row in rows_to_select:
                item = self.right_icon_list.item(row)
                if item is not None:
                    item.setSelected(True)
            anchor_row: int | None = None
            if anchor_path:
                anchor_rows = path_to_rows.get(anchor_path, [])
                if anchor_rows:
                    anchor_row = anchor_rows[0]
            if anchor_row is None:
                anchor_row = rows_to_select[0]
            anchor_item = self.right_icon_list.item(anchor_row)
            if anchor_item is not None:
                self.right_icon_list.setCurrentItem(anchor_item)
                self.right_icon_list.scrollToItem(anchor_item)
        finally:
            self.right_icon_list.blockSignals(blocked)
        self._update_counts_status()

    def _prune_icon_caches(self) -> None:
        valid_icon_keys: set[str] = set()
        valid_preview_keys: set[str] = set()
        for entry in self.inventory:
            if not (entry.is_dir and entry.icon_status == "valid" and entry.folder_icon_path):
                continue
            icon_path = os.path.normpath(entry.folder_icon_path)
            valid_icon_keys.add(icon_cache_key(icon_path, 16))
            valid_preview_keys.add(icon_cache_key(icon_path, 256))
        self._folder_icon_cache = {
            key: icon for key, icon in self._folder_icon_cache.items() if key in valid_icon_keys
        }
        self._folder_icon_preview_cache = {
            key: pix
            for key, pix in self._folder_icon_preview_cache.items()
            if key in valid_preview_keys
        }

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

        def _run(progress_cb, should_cancel):
            return self.state.execute_archive_move_plan_with_progress(
                applied_items,
                progress_cb=progress_cb,
                should_cancel=should_cancel,
            )

        def _done(report: OperationReport) -> None:
            self._show_archive_move_report(report)
            self.refresh_all()

        self._start_report_operation("Move archives", _run, _done)

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
        if self._teracopy_processes:
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
            def _run(progress_cb, should_cancel):
                return self.state.execute_archive_move_plan_with_progress(
                    plan_items,
                    progress_cb=progress_cb,
                    should_cancel=should_cancel,
                )

            def _done(report: OperationReport) -> None:
                self._show_archive_move_report(report)
                self.refresh_all()

            self._start_report_operation("Move archives", _run, _done)
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
