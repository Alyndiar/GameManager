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
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeyEvent, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHeaderView,
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
    QMenu,
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
from gamemanager.services.storefronts.priority import (
    STORE_BADGE_COLORS,
    STORE_SHORT_LABELS,
    normalize_store_name,
    sort_stores,
)
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
    StoreAccountsDialog,
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
    ("stores", "Stores"),
]

RIGHT_VIEW_OPTIONS = {
    "List": "list",
    "Icons": "icons",
}

RIGHT_SORT_DIRECTION_OPTIONS = {
    "Ascending": True,
    "Descending": False,
}
RIGHT_STORE_FILTER_ALL = ""
RIGHT_STORE_FILTER_ANY = "__any__"
RIGHT_STORE_FILTER_NONE = "__none__"

ICON_VIEW_SIZES = [16, 24, 32, 48, 64, 128, 256]
ENABLE_ICON_PATH_REPAIR_ACTION = True

DEFAULT_RIGHT_SORT_CHAIN: list[tuple[str, bool]] = [
    ("cleaned_name", True),
    ("modified_at", False),
    ("full_name", True),
    ("created_at", False),
    ("size_bytes", False),
    ("source", True),
    ("stores", True),
]

RIGHT_COLUMN_LAYOUT_ORDER_PREF = "right_columns_order_v1"
RIGHT_COLUMN_LAYOUT_HIDDEN_PREF = "right_columns_hidden_v1"
RIGHT_COLUMN_LAYOUT_WIDTHS_PREF = "right_columns_widths_v1"
STORE_BADGE_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets" / "store_badges"
STORE_BADGE_ICON_FILES: dict[str, str] = {
    "Steam": "steam.png",
    "EGS": "egs.png",
    "GOG": "gog.png",
    "Itch.io": "itchio.png",
    "Humble": "humble.png",
    "Ubisoft": "ubisoft.png",
    "Battle.net": "battlenet.png",
    "Amazon Games": "amazon_games.png",
}


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
        self._right_column_order_fields: list[str] = [field for field, _ in RIGHT_COLUMNS]
        self._right_hidden_columns: set[int] = set()
        self._right_column_widths: dict[int, int] = {}
        self._right_column_layout_loaded = False
        self._right_columns_autosized_once = False
        self._initial_split_applied = False
        self._show_only_duplicates = False
        self._visible_right_items: list[InventoryItem] = []
        self._loaded_roots_count = 0
        self._loaded_entries_count = 0
        self._right_view_mode = "list"
        self._right_icon_size_index = ICON_VIEW_SIZES.index(64)
        self._right_store_filter = ""
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
        self._store_accounts_dialog_cls = StoreAccountsDialog
        self._interactive_operation_active = False
        self._interactive_cancel_requested = False
        self._sgdb_picker_flow_state: dict[str, object] | None = None
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
        self._store_badge_pixmap_cache: dict[tuple[str, int, int], QPixmap] = {}
        self._store_logo_pixmap_cache: dict[tuple[str, int], QPixmap] = {}
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
        self.store_accounts_btn = QPushButton("Stores")
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
        self.store_accounts_btn.setToolTip("Storefront Accounts...\nShortcut: Alt+K")
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
            self.store_accounts_btn,
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
        icon_group_widgets.append(self.store_accounts_btn)

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
        right_controls.addWidget(QLabel("Sort:"))
        self.right_sort_combo = QComboBox(right_panel)
        for idx, (_field, label) in enumerate(self._right_columns):
            self.right_sort_combo.addItem(label, idx)
        right_controls.addWidget(self.right_sort_combo)
        right_controls.addWidget(QLabel("Order:"))
        self.right_sort_order_combo = QComboBox(right_panel)
        for label, value in RIGHT_SORT_DIRECTION_OPTIONS.items():
            self.right_sort_order_combo.addItem(label, value)
        right_controls.addWidget(self.right_sort_order_combo)
        right_controls.addWidget(QLabel("Store:"))
        self.right_store_filter_combo = QComboBox(right_panel)
        right_controls.addWidget(self.right_store_filter_combo)
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
        right_header = self.right_table.horizontalHeader()
        right_header.setStretchLastSection(False)
        right_header.setSectionsClickable(True)
        right_header.setSectionsMovable(True)
        right_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        right_header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
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
        self.store_accounts_btn.clicked.connect(self._on_store_accounts)
        self.move_backend_combo.currentTextChanged.connect(self._on_move_backend_changed)
        self.locate_teracopy_btn.clicked.connect(self._on_locate_teracopy)
        self.left_sort_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_label_combo.currentTextChanged.connect(self._on_left_pref_changed)
        self.left_filter_edit.textChanged.connect(self._on_left_filter_changed)
        self.right_view_combo.currentTextChanged.connect(self._on_right_view_changed)
        self.right_sort_combo.currentIndexChanged.connect(self._on_right_sort_controls_changed)
        self.right_sort_order_combo.currentIndexChanged.connect(self._on_right_sort_controls_changed)
        self.right_store_filter_combo.currentIndexChanged.connect(self._on_right_store_filter_changed)
        self.right_icon_size_slider.valueChanged.connect(self._on_right_icon_size_changed)
        self.left_table.itemSelectionChanged.connect(self._on_left_selection_changed)
        self.left_table.viewport().installEventFilter(self)
        self.right_table.viewport().installEventFilter(self)
        self.right_icon_list.viewport().installEventFilter(self)
        right_header = self.right_table.horizontalHeader()
        right_header.sectionClicked.connect(self._on_right_header_clicked)
        right_header.sectionMoved.connect(self._on_right_header_section_moved)
        right_header.sectionResized.connect(self._on_right_header_section_resized)
        right_header.sectionHandleDoubleClicked.connect(
            self._on_right_header_handle_double_clicked
        )
        right_header.customContextMenuRequested.connect(
            self._on_right_header_context_menu
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
            ("Backfill Missing Icon Sources", "Ctrl+Shift+U", self._on_backfill_missing_icon_sources),
            ("Upload Missing Icons to SteamGridDB", "Ctrl+Shift+Y", self._on_upload_missing_icons_to_steamgriddb_selected),
            ("Icon Provider Settings", "Alt+S", self._on_icon_provider_settings),
            ("Performance Settings", "Alt+P", self._on_open_performance_settings),
            ("Storefront Accounts", "Alt+K", self._on_store_accounts),
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
            "Ctrl+Shift+U - Backfill Missing Icon Sources",
            "Ctrl+Shift+Y - Upload Missing Icons to SteamGridDB",
            "Alt+I - Refresh InfoTip",
            "Alt+E - Manual InfoTip Entry",
            "Ctrl+T - Template Batch Prep",
            "Ctrl+Shift+T - Template Transparency",
            "Ctrl+Shift+C - Cleanup Names",
            "Ctrl+Shift+M - Move Archives",
            "Ctrl+Shift+G - Find Tags",
            "Alt+S - Icon Provider Settings",
            "Alt+P - Performance Settings",
            "Alt+K - Storefront Accounts",
            "Alt+L - Locate TeraCopy",
            "Ctrl+F - Focus Cleaned-Name Filter",
            "F1 - Show Shortcuts",
        ]
        if ENABLE_ICON_PATH_REPAIR_ACTION:
            lines.insert(-4, "Alt+X - Repair Icon Paths")
        QMessageBox.information(self, "Shortcuts", "\n".join(lines))

    def _success_popups_enabled(self) -> bool:
        raw = str(self.state.get_ui_pref("show_success_popups", "1") or "").strip().casefold()
        return raw not in {"0", "false", "no", "off"}

    def _set_success_popups_enabled(self, enabled: bool) -> None:
        self.state.set_ui_pref("show_success_popups", "1" if enabled else "0")

    def _show_success_popup(self, title: str, message: str) -> None:
        if not self._success_popups_enabled():
            return
        box = QMessageBox(self)
        box.setWindowTitle(str(title or "").strip() or "Operation Complete")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(str(message or "").strip())
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        dont_show = QCheckBox("Don't show success confirmations again", box)
        box.setCheckBox(dont_show)
        box.exec()
        if dont_show.isChecked():
            self._set_success_popups_enabled(False)

    @staticmethod
    def _normalize_right_store_filter_value(value: str) -> str:
        token = str(value or "").strip()
        if token in {RIGHT_STORE_FILTER_ALL, RIGHT_STORE_FILTER_ANY, RIGHT_STORE_FILTER_NONE}:
            return token
        return normalize_store_name(token)

    def _load_prefs(self) -> None:
        sort_pref = self.state.get_ui_pref("left_sort", "source_label")
        label_pref = self.state.get_ui_pref("left_label_mode", "source")
        right_view_pref = self.state.get_ui_pref("right_view_mode", "list")
        right_icon_size_pref = self.state.get_ui_pref("right_icon_size", "64")
        right_store_filter_pref = self.state.get_ui_pref("right_store_filter", "")
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
        self._right_store_filter = self._normalize_right_store_filter_value(
            right_store_filter_pref
        )
        for text, value in RIGHT_VIEW_OPTIONS.items():
            if value == self._right_view_mode:
                self.right_view_combo.setCurrentText(text)
                break
        self._populate_right_store_filter_options()
        self.right_icon_size_slider.setValue(self._right_icon_size_index)
        self._load_right_column_layout_prefs()
        self._apply_right_column_layout()
        self._apply_right_view_mode_ui()
        self._apply_right_icon_size_ui()
        self._sync_right_sort_controls()
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

    def _sync_right_sort_controls(self) -> None:
        if not hasattr(self, "right_sort_combo") or not hasattr(self, "right_sort_order_combo"):
            return
        blocked_sort = self.right_sort_combo.blockSignals(True)
        blocked_order = self.right_sort_order_combo.blockSignals(True)
        try:
            sort_idx = self.right_sort_combo.findData(self._right_sort_column)
            if sort_idx >= 0:
                self.right_sort_combo.setCurrentIndex(sort_idx)
            order_idx = self.right_sort_order_combo.findData(bool(self._right_sort_ascending))
            if order_idx >= 0:
                self.right_sort_order_combo.setCurrentIndex(order_idx)
        finally:
            self.right_sort_combo.blockSignals(blocked_sort)
            self.right_sort_order_combo.blockSignals(blocked_order)

    def _on_right_sort_controls_changed(self, _index: int) -> None:
        sort_data = self.right_sort_combo.currentData()
        order_data = self.right_sort_order_combo.currentData()
        try:
            sort_column = int(sort_data)
        except Exception:
            return
        ascending = bool(order_data)
        if sort_column < 0 or sort_column >= len(self._right_columns):
            return
        if sort_column == self._right_sort_column and ascending == self._right_sort_ascending:
            return
        self._right_sort_column = sort_column
        self._right_sort_ascending = ascending
        self._update_right_headers()
        self._populate_right(self.inventory)

    def _populate_right_store_filter_options(self) -> None:
        blocked = self.right_store_filter_combo.blockSignals(True)
        try:
            self.right_store_filter_combo.clear()
            self.right_store_filter_combo.addItem("All", RIGHT_STORE_FILTER_ALL)
            self.right_store_filter_combo.addItem("Any", RIGHT_STORE_FILTER_ANY)
            self.right_store_filter_combo.addItem("None", RIGHT_STORE_FILTER_NONE)
            seen: set[str] = set()
            for store_name in self.state.available_store_names():
                canonical = normalize_store_name(store_name)
                if not canonical:
                    continue
                key = canonical.casefold()
                if key in seen:
                    continue
                seen.add(key)
                self.right_store_filter_combo.addItem(canonical, canonical)
            selected = self._normalize_right_store_filter_value(self._right_store_filter)
            idx = self.right_store_filter_combo.findData(selected)
            if idx < 0:
                idx = 0
            self.right_store_filter_combo.setCurrentIndex(idx)
            self._right_store_filter = self._normalize_right_store_filter_value(
                str(self.right_store_filter_combo.currentData() or "")
            )
        finally:
            self.right_store_filter_combo.blockSignals(blocked)

    def _on_right_store_filter_changed(self, _index: int) -> None:
        selected = self._normalize_right_store_filter_value(
            str(self.right_store_filter_combo.currentData() or "")
        )
        if selected == self._right_store_filter:
            return
        self._right_store_filter = selected
        self.state.set_ui_pref("right_store_filter", self._right_store_filter)
        self._populate_right(self.inventory)

    def _apply_right_view_mode_ui(self) -> None:
        icon_mode = self._right_view_mode == "icons"
        self.right_table.setVisible(not icon_mode)
        self.right_icon_list.setVisible(icon_mode)
        self.right_icon_size_slider.setEnabled(icon_mode)
        self.right_icon_size_label.setEnabled(icon_mode)

    def _apply_right_icon_size_ui(self) -> None:
        px = ICON_VIEW_SIZES[self._right_icon_size_index]
        self.right_icon_size_label.setText(f"{px}px")
        icon_w = px if px >= 48 else px + 16
        self.right_icon_list.setIconSize(QSize(icon_w, px))
        grid_w = max(110, icon_w + 52)
        grid_h = px + 52
        self.right_icon_list.setGridSize(QSize(grid_w, grid_h))

    def _right_column_index_by_field(self, field_name: str) -> int:
        for idx, (field, _label) in enumerate(self._right_columns):
            if field == field_name:
                return idx
        return -1

    def _right_column_field_by_index(self, index: int) -> str:
        if 0 <= index < len(self._right_columns):
            return self._right_columns[index][0]
        return ""

    def _load_right_column_layout_prefs(self) -> None:
        default_order = [field for field, _label in self._right_columns]
        order_raw = self.state.get_ui_pref(
            RIGHT_COLUMN_LAYOUT_ORDER_PREF,
            "",
        ).strip()
        hidden_raw = self.state.get_ui_pref(
            RIGHT_COLUMN_LAYOUT_HIDDEN_PREF,
            "",
        ).strip()
        widths_raw = self.state.get_ui_pref(
            RIGHT_COLUMN_LAYOUT_WIDTHS_PREF,
            "",
        ).strip()

        order_fields: list[str] = []
        if order_raw:
            try:
                loaded = json.loads(order_raw)
            except json.JSONDecodeError:
                loaded = []
            if isinstance(loaded, list):
                seen: set[str] = set()
                for token in loaded:
                    field = str(token or "").strip()
                    if field in default_order and field not in seen:
                        seen.add(field)
                        order_fields.append(field)
        for field in default_order:
            if field not in order_fields:
                order_fields.append(field)
        self._right_column_order_fields = order_fields

        hidden_indexes: set[int] = set()
        if hidden_raw:
            try:
                loaded_hidden = json.loads(hidden_raw)
            except json.JSONDecodeError:
                loaded_hidden = []
            if isinstance(loaded_hidden, list):
                for token in loaded_hidden:
                    idx = self._right_column_index_by_field(str(token or "").strip())
                    if idx > 0:
                        hidden_indexes.add(idx)
        self._right_hidden_columns = hidden_indexes

        widths: dict[int, int] = {}
        if widths_raw:
            try:
                loaded_widths = json.loads(widths_raw)
            except json.JSONDecodeError:
                loaded_widths = {}
            if isinstance(loaded_widths, dict):
                for key, value in loaded_widths.items():
                    idx = self._right_column_index_by_field(str(key or "").strip())
                    if idx < 0:
                        continue
                    try:
                        width = int(value)
                    except Exception:
                        continue
                    if width >= 24:
                        widths[idx] = width
        self._right_column_widths = widths
        self._right_column_layout_loaded = True

    def _visible_right_columns_count(self) -> int:
        visible = 0
        for idx in range(len(self._right_columns)):
            if not self.right_table.isColumnHidden(idx):
                visible += 1
        return visible

    def _save_right_column_layout_prefs(self) -> None:
        if not self._right_column_layout_loaded:
            return
        header = self.right_table.horizontalHeader()
        order_fields: list[str] = []
        for visual_idx in range(header.count()):
            logical = header.logicalIndex(visual_idx)
            field = self._right_column_field_by_index(logical)
            if field:
                order_fields.append(field)
        hidden_fields = [
            self._right_column_field_by_index(idx)
            for idx in range(len(self._right_columns))
            if self.right_table.isColumnHidden(idx) and idx != 0
        ]
        widths = {
            self._right_column_field_by_index(idx): int(self.right_table.columnWidth(idx))
            for idx in range(len(self._right_columns))
            if self.right_table.columnWidth(idx) >= 24
        }
        self.state.set_ui_pref(
            RIGHT_COLUMN_LAYOUT_ORDER_PREF,
            json.dumps(order_fields, ensure_ascii=False),
        )
        self.state.set_ui_pref(
            RIGHT_COLUMN_LAYOUT_HIDDEN_PREF,
            json.dumps([field for field in hidden_fields if field], ensure_ascii=False),
        )
        self.state.set_ui_pref(
            RIGHT_COLUMN_LAYOUT_WIDTHS_PREF,
            json.dumps(widths, ensure_ascii=False),
        )

    def _apply_right_column_layout(self) -> None:
        header = self.right_table.horizontalHeader()
        blocked = header.blockSignals(True)
        try:
            ordered_logicals = [
                self._right_column_index_by_field(field)
                for field in self._right_column_order_fields
            ]
            ordered_logicals = [idx for idx in ordered_logicals if idx >= 0]
            for visual_idx, logical in enumerate(ordered_logicals):
                current_visual = header.visualIndex(logical)
                if current_visual >= 0 and current_visual != visual_idx:
                    header.moveSection(current_visual, visual_idx)
            for idx in range(len(self._right_columns)):
                hide = idx in self._right_hidden_columns and idx != 0
                self.right_table.setColumnHidden(idx, hide)
            for idx, width in self._right_column_widths.items():
                if 0 <= idx < len(self._right_columns):
                    self.right_table.setColumnWidth(idx, max(24, int(width)))
        finally:
            header.blockSignals(blocked)
        self._save_right_column_layout_prefs()

    def _toggle_right_column_visibility(self, logical_index: int, visible: bool) -> bool:
        if logical_index <= 0:
            self.right_table.setColumnHidden(0, False)
            return False
        if not visible and self._visible_right_columns_count() <= 1:
            return False
        self.right_table.setColumnHidden(logical_index, not visible)
        self._save_right_column_layout_prefs()
        return True

    def _on_right_header_context_menu(self, pos: QPoint) -> None:
        header = self.right_table.horizontalHeader()
        menu = QMenu(header)
        for idx, (_field, label) in enumerate(self._right_columns):
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(not self.right_table.isColumnHidden(idx))
            if idx == 0:
                action.setEnabled(False)
                action.setToolTip("Name column is always visible.")
                continue
            action.triggered.connect(
                lambda checked, logical_idx=idx: self._toggle_right_column_visibility(
                    logical_idx, checked
                )
            )
        menu.addSeparator()
        reset_action = menu.addAction("Reset Columns Layout")
        reset_action.triggered.connect(self._on_reset_right_columns_layout)
        menu.exec(header.mapToGlobal(pos))

    def _on_right_header_section_moved(
        self,
        _logical_index: int,
        _old_visual_index: int,
        _new_visual_index: int,
    ) -> None:
        self._save_right_column_layout_prefs()

    def _on_right_header_section_resized(
        self,
        logical_index: int,
        _old_size: int,
        new_size: int,
    ) -> None:
        if logical_index < 0 or logical_index >= len(self._right_columns):
            return
        if new_size < 24:
            return
        self._right_column_widths[logical_index] = int(new_size)
        self._save_right_column_layout_prefs()

    def _on_right_header_handle_double_clicked(self, logical_index: int) -> None:
        if logical_index < 0 or logical_index >= len(self._right_columns):
            return
        self.right_table.resizeColumnToContents(logical_index)
        width = max(24, int(self.right_table.columnWidth(logical_index)))
        self._right_column_widths[logical_index] = width
        self._save_right_column_layout_prefs()

    def _on_reset_right_columns_layout(self) -> None:
        self._right_column_order_fields = [field for field, _label in self._right_columns]
        self._right_hidden_columns = set()
        self._right_column_widths = {}
        self._right_columns_autosized_once = False
        self._apply_right_column_layout()
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
        self._sync_right_sort_controls()
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
                    stores_col = self._right_column_index_by_field("stores")
                    if index.isValid() and index.column() == stores_col:
                        row = index.row()
                        if 0 <= row < len(self._visible_right_items):
                            entry = self._visible_right_items[row]
                            cell_rect = self.right_table.visualRect(index)
                            local_x = event.pos().x() - cell_rect.left() - 4
                            badge_size = self._stores_badge_size_for_table_row()
                            spacing = self._stores_badge_spacing_for_size(badge_size)
                            store = self._store_from_strip_click(
                                list(entry.owned_stores or []),
                                local_x=local_x,
                                badge_size=badge_size,
                                spacing=spacing,
                            )
                            if store and self._open_store_page_for_entry(entry, store):
                                return True
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
        if watched is self.right_icon_list.viewport():
            if event.type() == QEvent.Type.MouseButtonPress:
                if event.button() == Qt.MouseButton.LeftButton:
                    index = self.right_icon_list.indexAt(event.pos())
                    if index.isValid() and 0 <= index.row() < len(self._visible_right_items):
                        row = index.row()
                        entry = self._visible_right_items[row]
                        primary = normalize_store_name(entry.primary_store or "")
                        if primary:
                            item_rect = self.right_icon_list.visualRect(index)
                            icon_px = self._icon_view_sizes[self._right_icon_size_index]
                            icon_w = icon_px if icon_px >= 48 else icon_px + 16
                            icon_left = item_rect.left() + max(0, (item_rect.width() - icon_w) // 2)
                            icon_top = item_rect.top() + 4
                            if icon_px < 48:
                                badge_rect = QRect(icon_left + icon_w - 16, icon_top, 16, 16)
                            else:
                                badge_px = max(16, min(32, int(round(icon_px / 4.0))))
                                badge_rect = QRect(
                                    icon_left + icon_px - badge_px,
                                    icon_top,
                                    badge_px,
                                    badge_px,
                                )
                            if badge_rect.contains(event.pos()):
                                if self._open_store_page_for_entry(entry, primary):
                                    return True
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
        if column < 0 or column >= len(self._right_columns):
            return
        if column == self._right_sort_column:
            self._right_sort_ascending = not self._right_sort_ascending
        else:
            self._right_sort_column = column
            self._right_sort_ascending = True
        self._sync_right_sort_controls()
        self._update_right_headers()
        self._populate_right(self.inventory)

    def _update_right_headers(self) -> None:
        headers: list[str] = []
        for idx, (_field, label) in enumerate(self._right_columns):
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
            igdb_enabled=current.igdb_enabled,
            igdb_client_id=current.igdb_client_id,
            igdb_client_secret=current.igdb_client_secret,
            igdb_api_base=current.igdb_api_base,
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
            igdb_enabled=payload.igdb_enabled,
            igdb_client_id=payload.igdb_client_id,
            igdb_client_secret=payload.igdb_client_secret,
            igdb_api_base=payload.igdb_api_base,
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
            success_popups_enabled=self._success_popups_enabled(),
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
        self._set_success_popups_enabled(bool(payload.success_popups_enabled))
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
        self._show_success_popup(
            "Performance Settings",
            "Settings saved. Scan/cache settings apply on next refresh/operation. "
            "Startup preload mode applies on next app startup.",
        )

    def _on_store_accounts(self) -> None:
        dialog = self._store_accounts_dialog_cls(
            self.state,
            after_sync_callback=self.refresh_all,
            parent=self,
        )
        dialog.exec()

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
        if int(report.failed) > 0:
            QMessageBox.warning(self, "Clean Backup Icons", "\n".join(lines))
            return
        self._show_success_popup("Clean Backup Icons", "\n".join(lines))

    def _test_icon_provider_settings(self, payload: IconProviderSettingsResult) -> str:
        settings = IconSearchSettings(
            steamgriddb_enabled=payload.steamgriddb_enabled,
            steamgriddb_api_key=payload.steamgriddb_api_key,
            steamgriddb_api_base=payload.steamgriddb_api_base,
            igdb_enabled=payload.igdb_enabled,
            igdb_client_id=payload.igdb_client_id,
            igdb_client_secret=payload.igdb_client_secret,
            igdb_api_base=payload.igdb_api_base,
        )
        return self._run_ui_pumped_call(
            "Test icon providers",
            lambda: self.state.test_icon_search_settings(settings),
        )

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

    def _store_badge_pixmap(
        self,
        store_name: str,
        size_px: int,
        *,
        opacity_percent: int = 100,
    ) -> QPixmap:
        canonical = normalize_store_name(store_name)
        key = (canonical, int(size_px), int(opacity_percent))
        cached = self._store_badge_pixmap_cache.get(key)
        if cached is not None:
            return cached
        size = max(8, int(size_px))
        logo_name = STORE_BADGE_ICON_FILES.get(canonical, "")
        logo_path = STORE_BADGE_ASSETS_DIR / logo_name if logo_name else Path("")
        logo_pix: QPixmap | None = None
        if logo_name and logo_path.is_file():
            logo_key = (canonical, size)
            cached_logo = self._store_logo_pixmap_cache.get(logo_key)
            if cached_logo is not None:
                logo_pix = cached_logo
            else:
                loaded = QPixmap(str(logo_path))
                if not loaded.isNull():
                    logo_px = max(8, size - 2)
                    scaled = loaded.scaled(
                        logo_px,
                        logo_px,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    if not scaled.isNull():
                        self._store_logo_pixmap_cache[logo_key] = scaled
                        logo_pix = scaled
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setOpacity(max(0.0, min(1.0, opacity_percent / 100.0)))
            if logo_pix is not None and not logo_pix.isNull():
                x = (size - logo_pix.width()) // 2
                y = (size - logo_pix.height()) // 2
                painter.drawPixmap(x, y, logo_pix)
                self._store_badge_pixmap_cache[key] = pix
                return pix
            color = QColor(STORE_BADGE_COLORS.get(canonical, "#404040"))
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#111111"), 1))
            painter.drawRoundedRect(0, 0, size - 1, size - 1, 3, 3)
            label = STORE_SHORT_LABELS.get(canonical, canonical[:1].upper() if canonical else "?")
            font = QFont()
            font.setBold(True)
            font.setPixelSize(max(8, int(size * 0.52)))
            painter.setFont(font)
            painter.setPen(QColor("#F0F0F0"))
            painter.drawText(
                QRect(0, 0, size, size),
                int(Qt.AlignmentFlag.AlignCenter),
                label,
            )
        finally:
            painter.end()
        self._store_badge_pixmap_cache[key] = pix
        return pix

    @staticmethod
    def _stores_strip_required_width(
        store_count: int,
        *,
        badge_size: int = 16,
        spacing: int = 2,
    ) -> int:
        count = max(0, int(store_count))
        size = max(10, int(badge_size))
        if count <= 0:
            return 40
        return 8 + (count * size) + (max(0, count - 1) * spacing) + 8

    def _stores_badge_size_for_table_row(self) -> int:
        row_h = int(self.right_table.verticalHeader().defaultSectionSize())
        if self.right_table.rowCount() > 0:
            row_h = max(row_h, int(self.right_table.rowHeight(0)))
        size = max(12, row_h - 4)
        return size

    @staticmethod
    def _stores_badge_spacing_for_size(badge_size: int) -> int:
        return max(2, int(badge_size) // 8)

    def _stores_strip_widget(
        self,
        stores: list[str],
        *,
        badge_size: int = 16,
        spacing: int = 2,
    ) -> QWidget:
        ordered = sort_stores(list(stores or []))
        size = max(10, int(badge_size))
        container = QWidget(self.right_table)
        container.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(max(0, int(spacing)))
        if not ordered:
            layout.addStretch(1)
            return container
        for store in ordered:
            label = QLabel(container)
            label.setFixedSize(size, size)
            label.setPixmap(self._store_badge_pixmap(store, size, opacity_percent=100))
            label.setScaledContents(False)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            layout.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addStretch(1)
        return container

    def _stores_strip_icon(self, stores: list[str], *, badge_size: int = 16) -> QIcon:
        ordered = sort_stores(list(stores or []))
        if not ordered:
            return self._blank_icon
        size = max(10, int(badge_size))
        spacing = 2
        width = (len(ordered) * size) + (max(0, len(ordered) - 1) * spacing)
        pix = QPixmap(width, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        try:
            x = 0
            for store in ordered:
                badge = self._store_badge_pixmap(store, size, opacity_percent=100)
                painter.drawPixmap(x, 0, badge)
                x += size + spacing
        finally:
            painter.end()
        return QIcon(pix)

    def _icon_with_primary_store_badge(
        self,
        entry: InventoryItem,
        base_icon: QIcon,
        icon_px: int,
    ) -> QIcon:
        primary = normalize_store_name(entry.primary_store or "")
        if not primary:
            return base_icon
        if icon_px < 48:
            canvas_w = icon_px + 16
            canvas_h = icon_px
            base_pix = base_icon.pixmap(icon_px, icon_px)
            if base_pix.isNull():
                base_pix = self._icon_placeholder(icon_px).pixmap(icon_px, icon_px)
            if base_pix.isNull():
                return base_icon
            badge_px = 16
            badge = self._store_badge_pixmap(primary, badge_px, opacity_percent=100)
            out = QPixmap(canvas_w, canvas_h)
            out.fill(Qt.GlobalColor.transparent)
            painter = QPainter(out)
            try:
                painter.drawPixmap(0, 0, base_pix)
                badge_x = canvas_w - badge_px
                painter.drawPixmap(badge_x, 0, badge)
            finally:
                painter.end()
            return QIcon(out)

        base_pix = base_icon.pixmap(icon_px, icon_px)
        if base_pix.isNull():
            base_pix = self._icon_placeholder(icon_px).pixmap(icon_px, icon_px)
        if base_pix.isNull():
            return base_icon
        badge_px = max(16, min(32, int(round(icon_px / 4.0))))
        badge = self._store_badge_pixmap(primary, badge_px, opacity_percent=66)
        out = QPixmap(icon_px, icon_px)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        try:
            painter.drawPixmap(0, 0, base_pix)
            badge_x = max(0, icon_px - badge_px)
            painter.drawPixmap(badge_x, 0, badge)
        finally:
            painter.end()
        return QIcon(out)

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
            if int(report.failed) > 0 or int(conflict_count) > 0:
                QMessageBox.warning(self, "Cleanup Result", "\n".join(lines))
            else:
                self._show_success_popup("Cleanup Result", "\n".join(lines))
            self.refresh_all()

        self._start_report_operation("Cleanup names", _run, _done)

