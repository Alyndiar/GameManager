from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRectF, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from gamemanager.services.image_prep import (
    ImagePrepOptions,
    SUPPORTED_IMAGE_EXTENSIONS,
    apply_background_color_transparency,
    icon_templates_dir,
    normalize_to_square_png,
    prepare_images_to_template_folder,
    resolve_background_removal_config,
)
from gamemanager.services.template_transparency import (
    TemplateTransparencyOptions,
    default_curve_strength_for_falloff,
    falloff_uses_curve_strength,
    make_background_transparent,
    normalize_falloff_mode,
)
from gamemanager.ui.alpha_preview import composite_on_checkerboard, draw_checkerboard
from .shared import bind_dialog_shortcut as _bind_dialog_shortcut
_TEMPLATE_GALLERY_SIZE_PREF_KEY = "template_gallery_preview_size"
_TEMPLATE_GALLERY_SIZE_MIN = 48
_TEMPLATE_GALLERY_SIZE_MAX = 256
_TEMPLATE_GALLERY_SIZE_DEFAULT = 128
_template_gallery_size_runtime = _TEMPLATE_GALLERY_SIZE_DEFAULT


def _template_gallery_clamp_size(value: int) -> int:
    return max(_TEMPLATE_GALLERY_SIZE_MIN, min(_TEMPLATE_GALLERY_SIZE_MAX, int(value)))


def _resolve_ui_state(widget: QWidget | None):
    current = widget
    while current is not None:
        state = getattr(current, "state", None)
        if state is not None and hasattr(state, "get_ui_pref") and hasattr(state, "set_ui_pref"):
            return state
        current = current.parentWidget()
    return None


def _load_template_gallery_size(parent: QWidget | None) -> int:
    global _template_gallery_size_runtime
    state = _resolve_ui_state(parent)
    if state is not None:
        raw = str(state.get_ui_pref(_TEMPLATE_GALLERY_SIZE_PREF_KEY, str(_template_gallery_size_runtime))).strip()
        try:
            parsed = int(raw)
        except ValueError:
            parsed = _template_gallery_size_runtime
        _template_gallery_size_runtime = _template_gallery_clamp_size(parsed)
    return _template_gallery_size_runtime


def _save_template_gallery_size(parent: QWidget | None, size: int) -> None:
    global _template_gallery_size_runtime
    normalized = _template_gallery_clamp_size(size)
    _template_gallery_size_runtime = normalized
    state = _resolve_ui_state(parent)
    if state is not None:
        try:
            state.set_ui_pref(_TEMPLATE_GALLERY_SIZE_PREF_KEY, str(normalized))
        except Exception:
            pass


def _template_gallery_placeholder(size: int = 128) -> QPixmap:
    out = QPixmap(max(1, int(size)), max(1, int(size)))
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    draw_checkerboard(painter, QRectF(0.0, 0.0, float(out.width()), float(out.height())))
    pen = QPen(QColor(220, 220, 220, 190))
    pen.setWidth(2)
    pen.setStyle(Qt.PenStyle.DashLine)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRect(4, 4, out.width() - 8, out.height() - 8)
    painter.setPen(QColor(235, 235, 235, 230))
    painter.drawText(out.rect(), Qt.AlignmentFlag.AlignCenter, "No\nTemplate")
    painter.end()
    return out


def _template_gallery_pixmap(path: Path | None, *, size: int = 128) -> QPixmap:
    if path is None:
        return _template_gallery_placeholder(size)
    pix = QPixmap(str(path))
    if pix.isNull():
        return _template_gallery_placeholder(size)
    return composite_on_checkerboard(pix, width=size, height=size, keep_aspect=True)


class TemplateGalleryDialog(QDialog):
    def __init__(
        self,
        entries: list[tuple[str, str, Path | None]],
        *,
        current_key: str = "none",
        title: str = "Select Template",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._entries = list(entries)
        self._selected_key = str(current_key or "none")
        self._preview_size = _load_template_gallery_size(parent)
        layout = QVBoxLayout(self)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Display Size:", self))
        self.size_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.size_slider.setRange(_TEMPLATE_GALLERY_SIZE_MIN, _TEMPLATE_GALLERY_SIZE_MAX)
        self.size_slider.setValue(self._preview_size)
        top_row.addWidget(self.size_slider, 1)
        self.size_value = QLabel(f"{self._preview_size}px", self)
        self.size_value.setMinimumWidth(44)
        top_row.addWidget(self.size_value)
        layout.addLayout(top_row)
        self.list = QListWidget(self)
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setIconSize(QSize(self._preview_size, self._preview_size))
        self.list.setGridSize(QSize(self._preview_size + 26, self._preview_size + 50))
        self.list.setSpacing(8)
        self.list.setWordWrap(True)
        self._rebuild_items()
        self.list.itemClicked.connect(self._on_item_selected)
        self.list.itemActivated.connect(self._on_item_selected)
        self.size_slider.valueChanged.connect(self._on_size_changed)
        layout.addWidget(self.list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(900, 640)

    def _on_item_selected(self, item: QListWidgetItem) -> None:
        self._selected_key = str(item.data(Qt.ItemDataRole.UserRole) or "none")
        self.accept()

    def _rebuild_items(self) -> None:
        self.list.clear()
        self.list.setIconSize(QSize(self._preview_size, self._preview_size))
        self.list.setGridSize(QSize(self._preview_size + 26, self._preview_size + 50))
        current_item: QListWidgetItem | None = None
        for key, label, path in self._entries:
            item = QListWidgetItem(
                QIcon(_template_gallery_pixmap(path, size=self._preview_size)),
                label,
            )
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.list.addItem(item)
            if str(key) == self._selected_key:
                current_item = item
        if current_item is None and self.list.count() > 0:
            current_item = self.list.item(0)
        if current_item is not None:
            self.list.setCurrentItem(current_item)
            self.list.scrollToItem(
                current_item,
                QListWidget.ScrollHint.PositionAtCenter,
            )

    def _on_size_changed(self, value: int) -> None:
        self._preview_size = _template_gallery_clamp_size(value)
        self.size_value.setText(f"{self._preview_size}px")
        self._rebuild_items()
        _save_template_gallery_size(self, self._preview_size)

    def selected_key(self) -> str:
        return str(self._selected_key or "none")


class _DropPathListWidget(QListWidget):
    paths_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setAlternatingRowColors(True)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._default_stylesheet = self.styleSheet()

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        urls = event.mimeData().urls()
        paths = [url.toLocalFile() for url in urls if url.isLocalFile()]
        clean_paths = [p for p in paths if p]
        if clean_paths:
            self.paths_dropped.emit(clean_paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def set_background_preview(self, image_path: str | None) -> None:
        if not image_path:
            self.setStyleSheet(self._default_stylesheet)
            return
        path = Path(image_path)
        if not path.exists():
            self.setStyleSheet(self._default_stylesheet)
            return
        css_path = path.resolve().as_posix().replace("'", "\\'")
        self.setStyleSheet(
            "QListWidget {"
            f"background-image: url('{css_path}');"
            "background-position: center;"
            "background-repeat: no-repeat;"
            "background-origin: content;"
            "}"
            "QListWidget::item {"
            "background-color: rgba(16,16,16,170);"
            "color: #f0f0f0;"
            "}"
            "QListWidget::item:selected {"
            "background-color: rgba(55,110,200,220);"
            "}"
        )


class _LivePreviewLabel(QLabel):
    pixel_clicked = Signal(QPoint)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.pixel_clicked.emit(event.position().toPoint())
        super().mousePressEvent(event)


class _TemplatePrepWorker(QObject):
    progress = Signal(int, int, str, str, str, object, object)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        *,
        input_paths: list[str],
        options: ImagePrepOptions,
        output_dir: str,
    ) -> None:
        super().__init__()
        self._input_paths = list(input_paths)
        self._options = options
        self._output_dir = output_dir

    @Slot()
    def run(self) -> None:
        try:
            report = prepare_images_to_template_folder(
                input_paths=self._input_paths,
                options=self._options,
                output_dir=self._output_dir,
                progress_cb=self._emit_progress,
            )
            self.completed.emit(report)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def _emit_progress(
        self,
        current: int,
        total: int,
        source_path: str,
        destination_path: str,
        status: str,
        produced_path: str | None,
        error_text: str | None,
    ) -> None:
        self.progress.emit(
            int(current),
            int(total),
            str(source_path),
            str(destination_path),
            str(status),
            produced_path,
            error_text,
        )


class TemplatePrepDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Template Batch Prep")
        self._paths: list[str] = []
        self._output_dir = icon_templates_dir()
        self._run_in_progress = False
        self._run_worker_thread: QThread | None = None
        self._run_worker: _TemplatePrepWorker | None = None
        self._run_last_report: object | None = None
        self._run_last_error: str | None = None
        self._last_processed_background: str | None = None
        self._custom_bg_color: tuple[int, int, int] = (0, 0, 0)
        self._eyedropper_active = False
        self._preview_source_pixmap = QPixmap()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(260)
        self._preview_timer.timeout.connect(self._update_live_preview)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Drag files/folders below, tune parameters, then generate templateNNN PNGs "
                "in the IconTemplates folder."
            )
        )

        self.drop_list = _DropPathListWidget(self)
        self.drop_list.paths_dropped.connect(self._on_paths_dropped)
        self.drop_list.itemSelectionChanged.connect(self._schedule_live_preview)
        self.drop_list.currentItemChanged.connect(
            lambda _current, _previous: self._schedule_live_preview()
        )
        self.drop_list.setMinimumHeight(260)
        self.drop_list.setMinimumWidth(420)
        self.drop_list.setToolTip("Drop image files or folders here.")

        self.live_preview_label = _LivePreviewLabel("Live preview", self)
        self.live_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_preview_label.setMinimumSize(260, 260)
        self.live_preview_label.setFrameShape(QFrame.Shape.StyledPanel)
        self.live_preview_label.pixel_clicked.connect(self._on_live_preview_pixel_clicked)

        top_split = QHBoxLayout()
        top_split.setContentsMargins(0, 0, 0, 0)
        top_split.setSpacing(8)
        top_split.addWidget(self.drop_list, 3)
        top_split.addWidget(self.live_preview_label, 2)
        layout.addLayout(top_split, 1)

        bottom_split = QHBoxLayout()
        bottom_split.setContentsMargins(0, 0, 0, 0)
        bottom_split.setSpacing(8)
        layout.addLayout(bottom_split, 0)

        params_panel = QWidget(self)
        params_panel_layout = QVBoxLayout(params_panel)
        params_panel_layout.setContentsMargins(8, 8, 8, 8)
        params_panel_layout.setSpacing(5)
        params_panel_layout.addWidget(QLabel("Parameters", self))

        source_buttons = QHBoxLayout()
        source_buttons.setContentsMargins(0, 0, 0, 0)
        source_buttons.setSpacing(4)
        self.add_files_btn = QPushButton("Add Files...", self)
        self.add_files_btn.clicked.connect(self._on_add_files)
        self.add_files_btn.setToolTip("Add image files\nShortcut: Ctrl+O")
        source_buttons.addWidget(self.add_files_btn)
        self.add_folder_btn = QPushButton("Add Folder...", self)
        self.add_folder_btn.clicked.connect(self._on_add_folder)
        self.add_folder_btn.setToolTip("Add folder\nShortcut: Ctrl+Shift+O")
        source_buttons.addWidget(self.add_folder_btn)
        self.remove_selected_btn = QPushButton("Remove Selected", self)
        self.remove_selected_btn.clicked.connect(self._on_remove_selected)
        source_buttons.addWidget(self.remove_selected_btn)
        self.clear_btn = QPushButton("Clear", self)
        self.clear_btn.clicked.connect(self._on_clear_sources)
        source_buttons.addWidget(self.clear_btn)
        source_buttons.addStretch(1)
        params_panel_layout.addLayout(source_buttons)

        output_row = QHBoxLayout()
        output_row.setContentsMargins(0, 0, 0, 0)
        output_row.setSpacing(4)
        output_row.addWidget(QLabel("Output:", self))
        self.output_edit = QLineEdit(str(self._output_dir), self)
        self.output_edit.setReadOnly(True)
        output_row.addWidget(self.output_edit, 1)
        self.open_output_btn = QPushButton("Open Folder", self)
        self.open_output_btn.clicked.connect(self._on_open_output_folder)
        self.open_output_btn.setToolTip("Open output folder\nShortcut: Alt+O")
        output_row.addWidget(self.open_output_btn)
        params_panel_layout.addLayout(output_row)

        params = QHBoxLayout()
        params.setContentsMargins(0, 0, 0, 0)
        params.setSpacing(8)
        left_col = QVBoxLayout()
        left_col.setContentsMargins(0, 0, 0, 0)
        left_col.setSpacing(4)
        right_col = QVBoxLayout()
        right_col.setContentsMargins(0, 0, 0, 0)
        right_col.setSpacing(4)

        size_row = QHBoxLayout()
        size_row.setContentsMargins(0, 0, 0, 0)
        size_row.setSpacing(4)
        size_row.addWidget(QLabel("Output Size:", self))
        self.size_spin = QSpinBox(self)
        self.size_spin.setRange(32, 4096)
        self.size_spin.setValue(512)
        self.size_spin.setKeyboardTracking(False)
        self.size_spin.setSuffix(" px")
        size_row.addWidget(self.size_spin)
        size_row.addStretch(1)
        left_col.addLayout(size_row)

        pad_row = QHBoxLayout()
        pad_row.setContentsMargins(0, 0, 0, 0)
        pad_row.setSpacing(4)
        pad_row.addWidget(QLabel("Padding Ratio:", self))
        self.padding_ratio_spin = QDoubleSpinBox(self)
        self.padding_ratio_spin.setRange(0.0, 0.5)
        self.padding_ratio_spin.setSingleStep(0.005)
        self.padding_ratio_spin.setDecimals(3)
        self.padding_ratio_spin.setValue(0.0)
        self.padding_ratio_spin.setKeyboardTracking(False)
        pad_row.addWidget(self.padding_ratio_spin)
        pad_row.addStretch(1)
        left_col.addLayout(pad_row)

        min_pad_row = QHBoxLayout()
        min_pad_row.setContentsMargins(0, 0, 0, 0)
        min_pad_row.setSpacing(4)
        min_pad_row.addWidget(QLabel("Min Padding:", self))
        self.min_padding_spin = QSpinBox(self)
        self.min_padding_spin.setRange(0, 64)
        self.min_padding_spin.setValue(1)
        self.min_padding_spin.setKeyboardTracking(False)
        self.min_padding_spin.setSuffix(" px")
        min_pad_row.addWidget(self.min_padding_spin)
        min_pad_row.addStretch(1)
        left_col.addLayout(min_pad_row)

        alpha_row = QHBoxLayout()
        alpha_row.setContentsMargins(0, 0, 0, 0)
        alpha_row.setSpacing(4)
        alpha_row.addWidget(QLabel("Alpha Threshold:", self))
        self.alpha_threshold_spin = QSpinBox(self)
        self.alpha_threshold_spin.setRange(0, 255)
        self.alpha_threshold_spin.setValue(8)
        self.alpha_threshold_spin.setKeyboardTracking(False)
        alpha_row.addWidget(self.alpha_threshold_spin)
        alpha_row.addStretch(1)
        right_col.addLayout(alpha_row)

        border_row = QHBoxLayout()
        border_row.setContentsMargins(0, 0, 0, 0)
        border_row.setSpacing(4)
        border_row.addWidget(QLabel("Border Threshold:", self))
        self.border_threshold_spin = QSpinBox(self)
        self.border_threshold_spin.setRange(0, 255)
        self.border_threshold_spin.setValue(16)
        self.border_threshold_spin.setKeyboardTracking(False)
        border_row.addWidget(self.border_threshold_spin)
        border_row.addStretch(1)
        right_col.addLayout(border_row)

        recursive_row = QHBoxLayout()
        recursive_row.setContentsMargins(0, 0, 0, 0)
        recursive_row.setSpacing(4)
        self.recursive_check = QCheckBox("Recurse into subfolders", self)
        self.recursive_check.setChecked(True)
        recursive_row.addWidget(self.recursive_check)
        recursive_row.addStretch(1)
        right_col.addLayout(recursive_row)

        bg_mode_row = QHBoxLayout()
        bg_mode_row.setContentsMargins(0, 0, 0, 0)
        bg_mode_row.setSpacing(4)
        bg_mode_row.addWidget(QLabel("BG Removal:", self))
        self.bg_remove_mode_combo = QComboBox(self)
        self.bg_remove_mode_combo.addItem("Black", "black")
        self.bg_remove_mode_combo.addItem("White", "white")
        self.bg_remove_mode_combo.addItem("Custom", "custom")
        bg_mode_row.addWidget(self.bg_remove_mode_combo)
        bg_mode_row.addStretch(1)
        right_col.addLayout(bg_mode_row)

        tol_row = QHBoxLayout()
        tol_row.setContentsMargins(0, 0, 0, 0)
        tol_row.setSpacing(4)
        tol_row.addWidget(QLabel("Tolerance:", self))
        self.bg_tolerance_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.bg_tolerance_slider.setRange(0, 30)
        self.bg_tolerance_slider.setValue(10)
        self.bg_tolerance_slider.setTracking(False)
        self.bg_tolerance_slider.setToolTip(
            "0-30. For custom non-black/white, effective tolerance is half and uses HSV(0-255)."
        )
        tol_row.addWidget(self.bg_tolerance_slider, 1)
        self.bg_tolerance_value = QLabel("10", self)
        self.bg_tolerance_value.setMinimumWidth(28)
        tol_row.addWidget(self.bg_tolerance_value)
        right_col.addLayout(tol_row)

        falloff_row = QHBoxLayout()
        falloff_row.setContentsMargins(0, 0, 0, 0)
        falloff_row.setSpacing(4)
        falloff_row.addWidget(QLabel("Falloff:", self))
        self.bg_falloff_combo = QComboBox(self)
        self.bg_falloff_combo.addItem("Flat", "flat")
        self.bg_falloff_combo.addItem("Lin", "lin")
        self.bg_falloff_combo.addItem("Smooth", "smooth")
        self.bg_falloff_combo.addItem("Cos", "cos")
        self.bg_falloff_combo.addItem("Exp", "exp")
        self.bg_falloff_combo.addItem("Log", "log")
        self.bg_falloff_combo.addItem("Gauss", "gauss")
        falloff_row.addWidget(self.bg_falloff_combo)
        self.bg_falloff_adv_check = QCheckBox("Adv", self)
        falloff_row.addWidget(self.bg_falloff_adv_check)
        self.bg_curve_label = QLabel("Curve:", self)
        falloff_row.addWidget(self.bg_curve_label)
        self.bg_curve_spin = QSpinBox(self)
        self.bg_curve_spin.setRange(0, 100)
        self.bg_curve_spin.setValue(50)
        self.bg_curve_spin.setKeyboardTracking(False)
        falloff_row.addWidget(self.bg_curve_spin)
        falloff_row.addStretch(1)
        right_col.addLayout(falloff_row)

        color_row = QHBoxLayout()
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.setSpacing(4)
        color_row.addWidget(QLabel("Color:", self))
        self.bg_color_btn = QPushButton("#000000", self)
        self.bg_color_btn.clicked.connect(self._on_pick_custom_bg_color)
        color_row.addWidget(self.bg_color_btn)
        self.bg_eyedropper_btn = QPushButton("Eye-dropper", self)
        self.bg_eyedropper_btn.setCheckable(True)
        self.bg_eyedropper_btn.toggled.connect(self._on_toggle_eyedropper)
        color_row.addWidget(self.bg_eyedropper_btn)
        color_row.addStretch(1)
        right_col.addLayout(color_row)

        params.addLayout(left_col, 1)
        params.addLayout(right_col, 1)
        params_panel_layout.addLayout(params)

        status_panel = QWidget(self)
        status_panel_layout = QVBoxLayout(status_panel)
        status_panel_layout.setContentsMargins(8, 8, 8, 8)
        status_panel_layout.setSpacing(5)
        status_panel_layout.addWidget(QLabel("Status / Console", self))

        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(4)
        self.run_btn = QPushButton("Generate Templates", self)
        self.run_btn.clicked.connect(self._on_generate)
        self.run_btn.setToolTip("Generate templates\nShortcut: Ctrl+Enter")
        action_row.addWidget(self.run_btn)
        self.progress_label = QLabel("Progress: idle", self)
        action_row.addWidget(self.progress_label)
        action_row.addStretch(1)
        status_panel_layout.addLayout(action_row)

        self.result_box = QPlainTextEdit(self)
        self.result_box.setReadOnly(True)
        self.result_box.setMinimumHeight(90)
        self.result_box.setMaximumHeight(170)
        status_panel_layout.addWidget(self.result_box)

        bottom_split.addWidget(params_panel, 1)
        bottom_split.addWidget(status_panel, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.close_btn = buttons.button(QDialogButtonBox.StandardButton.Close)
        if self.close_btn is not None:
            self.close_btn.setToolTip("Close\nShortcut: Esc")
        self.resize(950, 650)
        self._adjust_drop_list_width()

        for widget in (
            self.size_spin,
            self.padding_ratio_spin,
            self.min_padding_spin,
            self.alpha_threshold_spin,
            self.border_threshold_spin,
            self.recursive_check,
            self.bg_tolerance_slider,
        ):
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self._schedule_live_preview)
            else:
                widget.valueChanged.connect(self._schedule_live_preview)
        self.bg_tolerance_slider.valueChanged.connect(
            lambda v: self.bg_tolerance_value.setText(str(int(v)))
        )
        self.bg_remove_mode_combo.currentIndexChanged.connect(self._on_bg_mode_changed)
        self.bg_falloff_combo.currentIndexChanged.connect(self._on_bg_falloff_mode_changed)
        self.bg_falloff_adv_check.toggled.connect(self._on_bg_falloff_advanced_toggled)
        self.bg_curve_spin.valueChanged.connect(self._schedule_live_preview)
        self._sync_bg_color_controls()
        self._sync_bg_falloff_controls()
        self._schedule_live_preview()
        _bind_dialog_shortcut(self, "Ctrl+O", self._on_add_files)
        _bind_dialog_shortcut(self, "Ctrl+Shift+O", self._on_add_folder)
        _bind_dialog_shortcut(self, "Alt+O", self._on_open_output_folder)
        _bind_dialog_shortcut(self, "Ctrl+Return", self._on_generate)
        _bind_dialog_shortcut(self, "Ctrl+Enter", self._on_generate)
        _bind_dialog_shortcut(self, "F1", self._show_shortcuts)

    def _append_result(self, text: str) -> None:
        existing = self.result_box.toPlainText().strip()
        merged = f"{existing}\n{text}".strip() if existing else text
        self.result_box.setPlainText(merged)
        self.result_box.verticalScrollBar().setValue(
            self.result_box.verticalScrollBar().maximum()
        )

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Template Prep Shortcuts",
            "\n".join(
                [
                    "Ctrl+O - Add Files",
                    "Ctrl+Shift+O - Add Folder",
                    "Alt+O - Open Output Folder",
                    "Ctrl+Enter - Generate Templates",
                    "Esc - Close",
                    "F1 - Show Shortcuts",
                ]
            ),
        )

    def _refresh_list(self) -> None:
        self.drop_list.clear()
        for path in self._paths:
            label = Path(path).name or path
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path)
            self.drop_list.addItem(item)
        self._adjust_drop_list_width()
        self._schedule_live_preview()

    def _adjust_drop_list_width(self) -> None:
        metrics = self.drop_list.fontMetrics()
        max_text_width = 0
        for path in self._paths:
            label = Path(path).name or path
            max_text_width = max(max_text_width, int(metrics.horizontalAdvance(label)))
        if not self._paths:
            target = 260
        else:
            target = max_text_width + 66
        target = max(220, min(520, int(target)))
        self.drop_list.setFixedWidth(target)

    def _normalize_input_paths(self, paths: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in paths:
            if not raw:
                continue
            path = Path(raw).expanduser()
            try:
                resolved = str(path.resolve())
            except OSError:
                resolved = str(path)
            if resolved not in normalized:
                normalized.append(resolved)
        return normalized

    def _add_paths(self, paths: list[str]) -> None:
        for path in self._normalize_input_paths(paths):
            if path not in self._paths:
                self._paths.append(path)
        self._refresh_list()

    def _on_paths_dropped(self, paths: list[str]) -> None:
        self._add_paths(paths)

    def _on_add_files(self) -> None:
        selected, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose Images",
            "",
            "Images (*.png *.jpg *.jpe *.jpeg *.jfif *.avif *.webp *.bmp *.gif *.tif *.tiff);;All Files (*)",
        )
        if not selected:
            return
        self._add_paths(selected)

    def _on_add_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Choose Folder")
        if not selected:
            return
        self._add_paths([selected])

    def _on_remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.drop_list.selectedIndexes()}, reverse=True)
        for row in rows:
            if 0 <= row < len(self._paths):
                self._paths.pop(row)
        self._refresh_list()

    def _on_clear_sources(self) -> None:
        self._paths.clear()
        self._refresh_list()

    def _on_open_output_folder(self) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(self._output_dir))
        except OSError as exc:
            QMessageBox.warning(self, "Open Folder Failed", f"{exc}")

    def _schedule_live_preview(self) -> None:
        self._preview_timer.start()

    def _current_bg_remove_mode(self) -> str:
        mode = str(self.bg_remove_mode_combo.currentData() or "black").strip().casefold()
        if mode not in {"black", "white", "custom"}:
            return "black"
        return mode

    def _current_bg_tolerance(self) -> int:
        return max(0, min(30, int(self.bg_tolerance_slider.value())))

    def _current_bg_color(self) -> tuple[int, int, int]:
        mode = self._current_bg_remove_mode()
        if mode == "white":
            return (255, 255, 255)
        if mode == "custom":
            return self._custom_bg_color
        return (0, 0, 0)

    def _current_bg_falloff_mode(self) -> str:
        return normalize_falloff_mode(str(self.bg_falloff_combo.currentData() or "flat"))

    def _current_bg_curve_strength(self) -> int:
        if not self.bg_falloff_adv_check.isChecked():
            return 50
        return max(0, min(100, int(self.bg_curve_spin.value())))

    def _sync_bg_color_controls(self) -> None:
        custom_mode = self._current_bg_remove_mode() == "custom"
        self.bg_color_btn.setEnabled(custom_mode)
        self.bg_eyedropper_btn.setEnabled(custom_mode)
        if not custom_mode and self.bg_eyedropper_btn.isChecked():
            self.bg_eyedropper_btn.setChecked(False)
        self._update_bg_color_button()

    def _update_bg_color_button(self) -> None:
        r, g, b = self._custom_bg_color
        text = f"#{r:02X}{g:02X}{b:02X}"
        self.bg_color_btn.setText(text)
        text_color = "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#f0f0f0"
        self.bg_color_btn.setStyleSheet(
            f"QPushButton {{ background-color: rgb({r},{g},{b}); color: {text_color}; }}"
        )

    def _sync_bg_falloff_controls(self) -> None:
        show_curve = self.bg_falloff_adv_check.isChecked() and falloff_uses_curve_strength(
            self._current_bg_falloff_mode()
        )
        self.bg_curve_label.setVisible(show_curve)
        self.bg_curve_spin.setVisible(show_curve)
        self.bg_curve_spin.setEnabled(show_curve)

    def _on_bg_mode_changed(self, _index: int) -> None:
        self._sync_bg_color_controls()
        self._schedule_live_preview()

    def _on_bg_falloff_mode_changed(self, _index: int) -> None:
        mode = self._current_bg_falloff_mode()
        if falloff_uses_curve_strength(mode) and not self.bg_falloff_adv_check.isChecked():
            blocked = self.bg_curve_spin.blockSignals(True)
            self.bg_curve_spin.setValue(default_curve_strength_for_falloff(mode))
            self.bg_curve_spin.blockSignals(blocked)
        self._sync_bg_falloff_controls()
        self._schedule_live_preview()

    def _on_bg_falloff_advanced_toggled(self, checked: bool) -> None:
        if not checked:
            blocked = self.bg_curve_spin.blockSignals(True)
            self.bg_curve_spin.setValue(50)
            self.bg_curve_spin.blockSignals(blocked)
        self._sync_bg_falloff_controls()
        self._schedule_live_preview()

    def _on_pick_custom_bg_color(self) -> None:
        r, g, b = self._custom_bg_color
        selected = QColorDialog.getColor(QColor(r, g, b), self, "Pick Background Color")
        if not selected.isValid():
            return
        self._custom_bg_color = (int(selected.red()), int(selected.green()), int(selected.blue()))
        self._update_bg_color_button()
        self._schedule_live_preview()

    def _on_toggle_eyedropper(self, checked: bool) -> None:
        self._eyedropper_active = bool(checked)
        if checked:
            self.progress_label.setText("Progress: click a pixel in Live preview to pick color")

    def _on_live_preview_pixel_clicked(self, position: QPoint) -> None:
        if not self._eyedropper_active:
            return
        if self._preview_source_pixmap.isNull():
            return
        label_w = max(1, self.live_preview_label.width())
        label_h = max(1, self.live_preview_label.height())
        scaled = self._preview_source_pixmap.scaled(
            QSize(label_w, label_h),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x_off = (label_w - scaled.width()) // 2
        y_off = (label_h - scaled.height()) // 2
        if (
            position.x() < x_off
            or position.y() < y_off
            or position.x() >= x_off + scaled.width()
            or position.y() >= y_off + scaled.height()
        ):
            return
        rel_x = position.x() - x_off
        rel_y = position.y() - y_off
        src_x = max(
            0,
            min(
                self._preview_source_pixmap.width() - 1,
                int(rel_x * self._preview_source_pixmap.width() / max(1, scaled.width())),
            ),
        )
        src_y = max(
            0,
            min(
                self._preview_source_pixmap.height() - 1,
                int(rel_y * self._preview_source_pixmap.height() / max(1, scaled.height())),
            ),
        )
        sampled = self._preview_source_pixmap.toImage().pixelColor(src_x, src_y)
        self._custom_bg_color = (
            int(sampled.red()),
            int(sampled.green()),
            int(sampled.blue()),
        )
        custom_idx = self.bg_remove_mode_combo.findData("custom")
        if custom_idx >= 0:
            self.bg_remove_mode_combo.setCurrentIndex(custom_idx)
        self._update_bg_color_button()
        self.progress_label.setText(
            f"Progress: picked color #{self._custom_bg_color[0]:02X}{self._custom_bg_color[1]:02X}{self._custom_bg_color[2]:02X}"
        )
        self.bg_eyedropper_btn.setChecked(False)
        self._schedule_live_preview()

    def _resolve_preview_source(self) -> Path | None:
        current_item = self.drop_list.currentItem()
        if current_item is not None:
            selected_data = current_item.data(Qt.ItemDataRole.UserRole)
            candidate_path = str(selected_data or "").strip()
            if not candidate_path:
                candidate_path = current_item.toolTip().strip()
        else:
            selected_items = self.drop_list.selectedItems()
            if selected_items:
                selected_data = selected_items[-1].data(Qt.ItemDataRole.UserRole)
                candidate_path = str(selected_data or "").strip()
                if not candidate_path:
                    candidate_path = selected_items[-1].toolTip().strip()
            else:
                candidate_path = self._paths[0] if self._paths else ""
        if not candidate_path:
            return None
        path = Path(candidate_path)
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
            return path
        if not path.is_dir():
            return None
        iterator = path.rglob("*") if self.recursive_check.isChecked() else path.glob("*")
        for child in iterator:
            if child.is_file() and child.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
                return child
        return None

    def _update_live_preview(self) -> None:
        source = self._resolve_preview_source()
        if source is None:
            self._preview_source_pixmap = QPixmap()
            self.live_preview_label.setText("Live preview\n(no image selected)")
            self.live_preview_label.setPixmap(QPixmap())
            return
        try:
            payload = source.read_bytes()
            payload = apply_background_color_transparency(
                payload,
                mode=self._current_bg_remove_mode(),
                tolerance=self._current_bg_tolerance(),
                custom_color_rgb=self._current_bg_color(),
                use_hsv_for_custom=True,
                falloff_mode=self._current_bg_falloff_mode(),
                curve_strength=self._current_bg_curve_strength(),
                use_center_flood_fill=True,
            )
            payload = normalize_to_square_png(
                payload,
                output_size=256,
                alpha_threshold=int(self.alpha_threshold_spin.value()),
                border_threshold=int(self.border_threshold_spin.value()),
                padding_ratio=float(self.padding_ratio_spin.value()),
                min_padding_pixels=int(self.min_padding_spin.value()),
            )
            pix = QPixmap()
            if not pix.loadFromData(payload):
                raise ValueError("Could not decode preview image.")
            self._preview_source_pixmap = pix
            composed = composite_on_checkerboard(
                pix,
                width=self.live_preview_label.width(),
                height=self.live_preview_label.height(),
                keep_aspect=True,
            )
            self.live_preview_label.setText("")
            self.live_preview_label.setPixmap(composed)
            self.live_preview_label.setToolTip(str(source))
        except Exception as exc:
            self._preview_source_pixmap = QPixmap()
            self.live_preview_label.setPixmap(QPixmap())
            self.live_preview_label.setText(f"Preview error:\n{exc}")

    def _set_run_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.add_files_btn,
            self.add_folder_btn,
            self.remove_selected_btn,
            self.clear_btn,
            self.open_output_btn,
            self.size_spin,
            self.padding_ratio_spin,
            self.min_padding_spin,
            self.alpha_threshold_spin,
            self.border_threshold_spin,
            self.recursive_check,
            self.bg_remove_mode_combo,
            self.bg_tolerance_slider,
            self.bg_falloff_combo,
            self.bg_falloff_adv_check,
            self.bg_curve_spin,
            self.bg_color_btn,
            self.bg_eyedropper_btn,
            self.run_btn,
        ):
            widget.setEnabled(enabled)

    def _on_generate(self) -> None:
        if self._run_in_progress:
            return
        if not self._paths:
            QMessageBox.information(
                self,
                "No Inputs",
                "Drop or add at least one file/folder first.",
            )
            return
        self._run_in_progress = True
        self._set_run_controls_enabled(False)
        self.result_box.setPlainText("")
        self.progress_label.setText("Progress: starting...")
        options = ImagePrepOptions(
            output_size=int(self.size_spin.value()),
            padding_ratio=float(self.padding_ratio_spin.value()),
            min_padding_pixels=int(self.min_padding_spin.value()),
            alpha_threshold=int(self.alpha_threshold_spin.value()),
            border_threshold=int(self.border_threshold_spin.value()),
            background_remove_mode=self._current_bg_remove_mode(),
            background_color_rgb=self._current_bg_color(),
            background_tolerance=self._current_bg_tolerance(),
            background_use_hsv=True,
            background_falloff_mode=self._current_bg_falloff_mode(),
            background_curve_strength=self._current_bg_curve_strength(),
            background_use_center_flood_fill=True,
            min_black_level=0,
            recursive=self.recursive_check.isChecked(),
        )
        self._last_processed_background = None
        self._run_last_report = None
        self._run_last_error = None

        thread = QThread(self)
        worker = _TemplatePrepWorker(
            input_paths=list(self._paths),
            options=options,
            output_dir=str(self._output_dir),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_generate_progress)
        worker.completed.connect(self._on_generate_completed)
        worker.failed.connect(self._on_generate_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_generate_finished)
        thread.finished.connect(thread.deleteLater)
        self._run_worker_thread = thread
        self._run_worker = worker
        thread.start()

    @Slot(int, int, str, str, str, object, object)
    def _on_generate_progress(
        self,
        current: int,
        total: int,
        source_path: str,
        _destination_path: str,
        status: str,
        produced_path: object,
        _error_text: object,
    ) -> None:
        source_name = Path(str(source_path)).name
        produced = str(produced_path) if isinstance(produced_path, str) and produced_path else ""
        if status == "succeeded" and produced:
            self._last_processed_background = produced
            self.drop_list.set_background_preview(produced)
            self.progress_label.setText(f"Progress: {current}/{total} (ok) {source_name}")
            return
        if status == "failed":
            self.progress_label.setText(f"Progress: {current}/{total} (failed) {source_name}")
            return
        self.progress_label.setText(f"Progress: {current}/{total} ({status}) {source_name}")

    @Slot(object)
    def _on_generate_completed(self, report: object) -> None:
        self._run_last_report = report

    @Slot(str)
    def _on_generate_failed(self, error: str) -> None:
        self._run_last_error = str(error).strip() or "Run failed unexpectedly."

    @Slot()
    def _on_generate_finished(self) -> None:
        self._run_worker_thread = None
        self._run_worker = None
        self._run_in_progress = False
        self._set_run_controls_enabled(True)
        if self._last_processed_background:
            self.drop_list.set_background_preview(self._last_processed_background)

        if self._run_last_error:
            QMessageBox.warning(self, "Template Prep", self._run_last_error)
            return
        report = self._run_last_report
        if report is None:
            QMessageBox.warning(self, "Template Prep", "Run failed unexpectedly.")
            return
        try:
            attempted = int(getattr(report, "attempted"))
            succeeded = int(getattr(report, "succeeded"))
            failed = int(getattr(report, "failed"))
            skipped = int(getattr(report, "skipped"))
            output_files = list(getattr(report, "output_files"))
            details = list(getattr(report, "details"))
        except Exception:
            QMessageBox.warning(self, "Template Prep", "Run returned an invalid report.")
            return
        lines = [
            f"Attempted: {attempted}",
            f"Succeeded: {succeeded}",
            f"Failed: {failed}",
            f"Skipped: {skipped}",
        ]
        if output_files:
            lines.append("")
            lines.append("Generated:")
            lines.extend([str(item) for item in output_files[:30]])
        if details:
            lines.append("")
            lines.append("Details:")
            lines.extend([str(item) for item in details[:30]])
        self.result_box.setPlainText("\n".join(lines))
        self.progress_label.setText(f"Progress: {attempted}/{attempted} complete")
        if failed:
            QMessageBox.warning(
                self,
                "Template Prep",
                f"Completed with errors. Failed: {failed}",
            )
            return
        QMessageBox.information(
            self,
            "Template Prep",
            f"Completed. Generated: {succeeded}",
        )

    def reject(self) -> None:
        if self._run_in_progress:
            self.progress_label.setText("Progress: generation in progress (wait for completion)")
            return
        super().reject()

    def _shutdown_run_worker_thread(
        self,
        *,
        timeout_ms: int = 2000,
        allow_terminate: bool = False,
    ) -> None:
        thread = self._run_worker_thread
        if thread is None:
            return
        try:
            thread.requestInterruption()
        except Exception:
            pass
        try:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(max(100, int(timeout_ms))) and allow_terminate:
                    thread.terminate()
                    thread.wait(max(100, int(timeout_ms // 2)))
        except Exception:
            pass
        self._run_worker_thread = None
        self._run_worker = None
        self._run_in_progress = False

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._shutdown_run_worker_thread(timeout_ms=1800, allow_terminate=True)
        super().closeEvent(event)


class TemplateTransparencyDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Template Transparency")
        self._template_dir = icon_templates_dir()
        self._template_files: list[Path] = []
        self._current_path: Path | None = None
        self._original_bytes: bytes | None = None
        self._preview_bytes: bytes | None = None
        self._loading = False
        self._last_combo_index = -1
        self._custom_bg_color: tuple[int, int, int] = (0, 0, 0)
        self._eyedropper_active = False
        self._preview_source_pixmap = QPixmap()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(260)
        self._preview_timer.timeout.connect(self._rebuild_preview)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Select an existing template, adjust background removal (color/tolerance/falloff), then Apply to save."
            )
        )

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Template:", self))
        self.template_combo = QComboBox(self)
        self.template_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        top_row.addWidget(self.template_combo, 1)
        self.template_gallery_btn = QPushButton("", self)
        self.template_gallery_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        )
        self.template_gallery_btn.setToolTip("Pick Template from Gallery\nShortcut: Alt+G")
        self.template_gallery_btn.clicked.connect(self._on_pick_template_from_gallery)
        top_row.addWidget(self.template_gallery_btn)
        self.reload_btn = QPushButton("Reload", self)
        self.reload_btn.setToolTip("Reload templates\nShortcut: Ctrl+R")
        self.reload_btn.clicked.connect(self._reload_templates)
        top_row.addWidget(self.reload_btn)
        layout.addLayout(top_row)

        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.setSpacing(4)
        mode_row.addWidget(QLabel("BG Removal:", self))
        self.bg_remove_mode_combo = QComboBox(self)
        self.bg_remove_mode_combo.addItem("Black", "black")
        self.bg_remove_mode_combo.addItem("White", "white")
        self.bg_remove_mode_combo.addItem("Custom", "custom")
        mode_row.addWidget(self.bg_remove_mode_combo)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        tol_row = QHBoxLayout()
        tol_row.setContentsMargins(0, 0, 0, 0)
        tol_row.setSpacing(4)
        tol_row.addWidget(QLabel("Tolerance:", self))
        self.bg_tolerance_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.bg_tolerance_slider.setRange(0, 30)
        self.bg_tolerance_slider.setValue(10)
        self.bg_tolerance_slider.setTracking(False)
        self.bg_tolerance_slider.setToolTip(
            "0-30. For custom non-black/white, effective tolerance is half and uses HSV(0-255)."
        )
        tol_row.addWidget(self.bg_tolerance_slider, 1)
        self.bg_tolerance_value = QLabel("10", self)
        self.bg_tolerance_value.setMinimumWidth(28)
        tol_row.addWidget(self.bg_tolerance_value)
        layout.addLayout(tol_row)

        color_row = QHBoxLayout()
        color_row.setContentsMargins(0, 0, 0, 0)
        color_row.setSpacing(4)
        color_row.addWidget(QLabel("Color:", self))
        self.bg_color_btn = QPushButton("#000000", self)
        self.bg_color_btn.clicked.connect(self._on_pick_custom_bg_color)
        color_row.addWidget(self.bg_color_btn)
        self.bg_eyedropper_btn = QPushButton("Eye-dropper", self)
        self.bg_eyedropper_btn.setCheckable(True)
        self.bg_eyedropper_btn.toggled.connect(self._on_toggle_eyedropper)
        color_row.addWidget(self.bg_eyedropper_btn)
        color_row.addStretch(1)
        layout.addLayout(color_row)

        falloff_row = QHBoxLayout()
        falloff_row.setContentsMargins(0, 0, 0, 0)
        falloff_row.setSpacing(4)
        falloff_row.addWidget(QLabel("Falloff:", self))
        self.bg_falloff_combo = QComboBox(self)
        self.bg_falloff_combo.addItem("Flat", "flat")
        self.bg_falloff_combo.addItem("Lin", "lin")
        self.bg_falloff_combo.addItem("Smooth", "smooth")
        self.bg_falloff_combo.addItem("Cos", "cos")
        self.bg_falloff_combo.addItem("Exp", "exp")
        self.bg_falloff_combo.addItem("Log", "log")
        self.bg_falloff_combo.addItem("Gauss", "gauss")
        falloff_row.addWidget(self.bg_falloff_combo)
        self.bg_falloff_adv_check = QCheckBox("Adv", self)
        falloff_row.addWidget(self.bg_falloff_adv_check)
        self.bg_curve_label = QLabel("Curve:", self)
        falloff_row.addWidget(self.bg_curve_label)
        self.bg_curve_spin = QSpinBox(self)
        self.bg_curve_spin.setRange(0, 100)
        self.bg_curve_spin.setValue(50)
        self.bg_curve_spin.setKeyboardTracking(False)
        falloff_row.addWidget(self.bg_curve_spin)
        falloff_row.addStretch(1)
        layout.addLayout(falloff_row)

        self.preview_label = _LivePreviewLabel("No template selected.", self)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(420, 420)
        self.preview_label.setFrameShape(QFrame.Shape.StyledPanel)
        layout.addWidget(self.preview_label, 1)

        actions = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.setToolTip("Discard preview changes\nShortcut: Alt+C")
        self.cancel_btn.clicked.connect(self._on_cancel_changes)
        actions.addWidget(self.cancel_btn)
        self.apply_btn = QPushButton("Apply", self)
        self.apply_btn.setToolTip("Apply current changes\nShortcut: Ctrl+S")
        self.apply_btn.clicked.connect(self._on_apply_changes)
        actions.addWidget(self.apply_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel("", self)
        layout.addWidget(self.status_label)
        self.resize(900, 760)

        self.template_combo.currentIndexChanged.connect(self._on_template_changed)
        self.bg_remove_mode_combo.currentIndexChanged.connect(self._on_bg_mode_changed)
        self.bg_tolerance_slider.valueChanged.connect(self._on_bg_tolerance_changed)
        self.bg_falloff_combo.currentIndexChanged.connect(self._on_bg_falloff_mode_changed)
        self.bg_falloff_adv_check.toggled.connect(self._on_bg_falloff_advanced_toggled)
        self.bg_curve_spin.valueChanged.connect(self._schedule_preview)
        self.preview_label.pixel_clicked.connect(self._on_preview_pixel_clicked)
        self._sync_bg_color_controls()
        self._sync_bg_falloff_controls()
        self._reload_templates()
        _bind_dialog_shortcut(self, "Ctrl+R", self._reload_templates)
        _bind_dialog_shortcut(self, "Ctrl+S", self._on_apply_changes)
        _bind_dialog_shortcut(self, "Alt+C", self._on_cancel_changes)
        _bind_dialog_shortcut(self, "Alt+G", self._on_pick_template_from_gallery)
        _bind_dialog_shortcut(self, "F1", self._show_shortcuts)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Template Transparency Shortcuts",
            "\n".join(
                [
                    "Ctrl+R - Reload templates",
                    "Alt+G - Open template gallery",
                    "Ctrl+S - Apply changes",
                    "Alt+C - Cancel changes",
                    "Esc - Close",
                    "F1 - Show Shortcuts",
                ]
            ),
        )

    def _reload_templates(self) -> None:
        self._template_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(self._template_dir.glob("*.png"), key=lambda p: p.name.casefold())
        self._template_files = files
        current = str(self._current_path) if self._current_path else ""
        self._loading = True
        self.template_combo.clear()
        self.template_combo.addItem("No Template", "")
        for path in files:
            self.template_combo.addItem(path.name, str(path))
        self._loading = False
        if not files:
            self.template_combo.setCurrentIndex(0)
            self._select_no_template()
            self._update_action_buttons()
            return
        target_idx = 0
        if current:
            for idx, path in enumerate(files):
                if str(path) == current:
                    target_idx = idx + 1
                    break
        was_blocked = self.template_combo.blockSignals(True)
        self.template_combo.setCurrentIndex(target_idx)
        self.template_combo.blockSignals(was_blocked)
        self._last_combo_index = target_idx
        if target_idx <= 0:
            self._select_no_template()
        else:
            self._load_template(files[target_idx - 1])

    def _select_no_template(self) -> None:
        self._current_path = None
        self._original_bytes = None
        self._preview_bytes = None
        self._preview_source_pixmap = QPixmap()
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText("No template selected.")
        self._set_status("")

    def _current_bg_remove_mode(self) -> str:
        mode = str(self.bg_remove_mode_combo.currentData() or "black").strip().casefold()
        if mode not in {"black", "white", "custom"}:
            return "black"
        return mode

    def _current_bg_tolerance(self) -> int:
        return max(0, min(30, int(self.bg_tolerance_slider.value())))

    def _current_bg_color(self) -> tuple[int, int, int]:
        mode = self._current_bg_remove_mode()
        if mode == "white":
            return (255, 255, 255)
        if mode == "custom":
            return self._custom_bg_color
        return (0, 0, 0)

    def _current_bg_falloff_mode(self) -> str:
        return normalize_falloff_mode(str(self.bg_falloff_combo.currentData() or "flat"))

    def _current_bg_curve_strength(self) -> int:
        if not self.bg_falloff_adv_check.isChecked():
            return 50
        return max(0, min(100, int(self.bg_curve_spin.value())))

    def _sync_bg_color_controls(self) -> None:
        custom_mode = self._current_bg_remove_mode() == "custom"
        self.bg_color_btn.setEnabled(custom_mode)
        self.bg_eyedropper_btn.setEnabled(custom_mode)
        if not custom_mode and self.bg_eyedropper_btn.isChecked():
            self.bg_eyedropper_btn.setChecked(False)
        self._update_bg_color_button()

    def _update_bg_color_button(self) -> None:
        r, g, b = self._custom_bg_color
        self.bg_color_btn.setText(f"#{r:02X}{g:02X}{b:02X}")
        text_color = "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 160 else "#f0f0f0"
        self.bg_color_btn.setStyleSheet(
            f"QPushButton {{ background-color: rgb({r},{g},{b}); color: {text_color}; }}"
        )

    def _sync_bg_falloff_controls(self) -> None:
        show_curve = self.bg_falloff_adv_check.isChecked() and falloff_uses_curve_strength(
            self._current_bg_falloff_mode()
        )
        self.bg_curve_label.setVisible(show_curve)
        self.bg_curve_spin.setVisible(show_curve)
        self.bg_curve_spin.setEnabled(show_curve)

    def _on_bg_mode_changed(self, _index: int) -> None:
        self._sync_bg_color_controls()
        self._schedule_preview()

    def _on_bg_tolerance_changed(self, value: int) -> None:
        self.bg_tolerance_value.setText(str(int(value)))
        self._schedule_preview()

    def _on_bg_falloff_mode_changed(self, _index: int) -> None:
        mode = self._current_bg_falloff_mode()
        if falloff_uses_curve_strength(mode) and not self.bg_falloff_adv_check.isChecked():
            blocked = self.bg_curve_spin.blockSignals(True)
            self.bg_curve_spin.setValue(default_curve_strength_for_falloff(mode))
            self.bg_curve_spin.blockSignals(blocked)
        self._sync_bg_falloff_controls()
        self._schedule_preview()

    def _on_bg_falloff_advanced_toggled(self, checked: bool) -> None:
        if not checked:
            blocked = self.bg_curve_spin.blockSignals(True)
            self.bg_curve_spin.setValue(50)
            self.bg_curve_spin.blockSignals(blocked)
        self._sync_bg_falloff_controls()
        self._schedule_preview()

    def _on_pick_custom_bg_color(self) -> None:
        r, g, b = self._custom_bg_color
        selected = QColorDialog.getColor(QColor(r, g, b), self, "Pick Background Color")
        if not selected.isValid():
            return
        self._custom_bg_color = (int(selected.red()), int(selected.green()), int(selected.blue()))
        self._update_bg_color_button()
        self._schedule_preview()

    def _on_toggle_eyedropper(self, checked: bool) -> None:
        self._eyedropper_active = bool(checked)
        if checked:
            self._set_status("Click preview to pick background color.")

    def _on_preview_pixel_clicked(self, position: QPoint) -> None:
        if not self._eyedropper_active:
            return
        if self._preview_source_pixmap.isNull():
            return
        label_w = max(1, self.preview_label.width())
        label_h = max(1, self.preview_label.height())
        scaled = self._preview_source_pixmap.scaled(
            QSize(label_w, label_h),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        x_off = (label_w - scaled.width()) // 2
        y_off = (label_h - scaled.height()) // 2
        if (
            position.x() < x_off
            or position.y() < y_off
            or position.x() >= x_off + scaled.width()
            or position.y() >= y_off + scaled.height()
        ):
            return
        rel_x = position.x() - x_off
        rel_y = position.y() - y_off
        src_x = max(
            0,
            min(
                self._preview_source_pixmap.width() - 1,
                int(rel_x * self._preview_source_pixmap.width() / max(1, scaled.width())),
            ),
        )
        src_y = max(
            0,
            min(
                self._preview_source_pixmap.height() - 1,
                int(rel_y * self._preview_source_pixmap.height() / max(1, scaled.height())),
            ),
        )
        sampled = self._preview_source_pixmap.toImage().pixelColor(src_x, src_y)
        self._custom_bg_color = (
            int(sampled.red()),
            int(sampled.green()),
            int(sampled.blue()),
        )
        idx = self.bg_remove_mode_combo.findData("custom")
        if idx >= 0:
            self.bg_remove_mode_combo.setCurrentIndex(idx)
        self._update_bg_color_button()
        self._set_status(
            f"Picked color #{self._custom_bg_color[0]:02X}{self._custom_bg_color[1]:02X}{self._custom_bg_color[2]:02X}"
        )
        self.bg_eyedropper_btn.setChecked(False)
        self._schedule_preview()

    def _schedule_preview(self) -> None:
        self._preview_timer.start()

    def _update_action_buttons(self) -> None:
        dirty = self._is_dirty()
        has_template = self._current_path is not None
        self.apply_btn.setEnabled(dirty and has_template)
        self.cancel_btn.setEnabled(dirty and has_template)

    def _is_dirty(self) -> bool:
        return bool(
            self._current_path is not None
            and self._original_bytes is not None
            and self._preview_bytes is not None
            and self._preview_bytes != self._original_bytes
        )

    def _load_template(self, path: Path) -> None:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "Load Failed", f"Could not read template:\n{exc}")
            return
        self._current_path = path
        self._original_bytes = payload
        self._preview_bytes = payload
        self._set_status(f"Loaded: {path.name}")
        self._rebuild_preview()

    def _render_preview(self, payload: bytes) -> None:
        pix = QPixmap()
        if not pix.loadFromData(payload):
            self._preview_source_pixmap = QPixmap()
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Could not decode image preview.")
            return
        self._preview_source_pixmap = pix
        composed = composite_on_checkerboard(
            pix,
            width=max(420, self.preview_label.width()),
            height=max(420, self.preview_label.height()),
            keep_aspect=True,
        )
        self.preview_label.setText("")
        self.preview_label.setPixmap(composed)

    def _rebuild_preview(self) -> None:
        if self._original_bytes is None:
            self._preview_source_pixmap = QPixmap()
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("No template selected.")
            self._update_action_buttons()
            return
        mode = self._current_bg_remove_mode()
        tolerance = self._current_bg_tolerance()
        base_color, level, color_space = resolve_background_removal_config(
            mode=mode,
            tolerance=tolerance,
            custom_color_rgb=self._current_bg_color(),
            use_hsv_for_custom=True,
        )
        if level <= 0:
            self._preview_bytes = self._original_bytes
        else:
            try:
                self._preview_bytes = make_background_transparent(
                    self._original_bytes,
                    options=TemplateTransparencyOptions(
                        threshold=max(0, min(30, level)),
                        color_tolerance_mode="max",
                        compare_color_space=color_space,
                        falloff_mode=self._current_bg_falloff_mode(),
                        curve_strength=self._current_bg_curve_strength(),
                        use_edge_flood_fill=True,
                        use_center_flood_fill=True,
                        preserve_existing_alpha=True,
                    ),
                    background_color=base_color,
                )
            except Exception as exc:
                self._preview_bytes = self._original_bytes
                self._set_status(f"Preview error: {exc}")
        if self._preview_bytes is not None:
            self._render_preview(self._preview_bytes)
        self._update_action_buttons()

    def _apply_current_changes(self) -> bool:
        if self._current_path is None or self._preview_bytes is None:
            return False
        if not self._is_dirty():
            return True
        try:
            self._current_path.write_bytes(self._preview_bytes)
        except OSError as exc:
            QMessageBox.warning(self, "Apply Failed", f"Could not save template:\n{exc}")
            return False
        self._original_bytes = self._preview_bytes
        self._set_status(f"Applied: {self._current_path.name}")
        self._update_action_buttons()
        return True

    def _on_apply_changes(self) -> None:
        self._apply_current_changes()

    def _on_cancel_changes(self) -> None:
        if self._original_bytes is None:
            return
        self._preview_bytes = self._original_bytes
        self._render_preview(self._original_bytes)
        self._set_status("Changes discarded.")
        self._update_action_buttons()

    def _confirm_switch_if_dirty(self) -> bool:
        if not self._is_dirty():
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved Changes")
        box.setText(
            "Changes were made to the current template.\n"
            "Apply saves changes. Cancel discards changes and loads the new template."
        )
        apply_btn = box.addButton("Apply", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(apply_btn)
        box.exec()
        if box.clickedButton() == apply_btn:
            if not self._apply_current_changes():
                return False
        elif box.clickedButton() == cancel_btn:
            self._on_cancel_changes()
        return True

    def _on_template_changed(self, index: int) -> None:
        if self._loading:
            return
        if index < 0 or index >= self.template_combo.count():
            return
        selected_data = self.template_combo.itemData(index)
        if not selected_data:
            if not self._confirm_switch_if_dirty():
                if 0 <= self._last_combo_index < self.template_combo.count():
                    self._loading = True
                    self.template_combo.setCurrentIndex(self._last_combo_index)
                    self._loading = False
                return
            self._last_combo_index = index
            self._select_no_template()
            return
        selected_path = Path(str(selected_data))
        if self._current_path is not None and selected_path == self._current_path:
            self._last_combo_index = index
            return
        if not self._confirm_switch_if_dirty():
            if 0 <= self._last_combo_index < self.template_combo.count():
                self._loading = True
                self.template_combo.setCurrentIndex(self._last_combo_index)
                self._loading = False
            return
        self._last_combo_index = index
        self._load_template(selected_path)

    def _on_pick_template_from_gallery(self) -> None:
        entries: list[tuple[str, str, Path | None]] = [("none", "No Template", None)]
        for path in self._template_files:
            entries.append((str(path), path.name, path))
        current_key = str(self._current_path) if self._current_path is not None else "none"
        dialog = TemplateGalleryDialog(
            entries,
            current_key=current_key,
            title="Select Template",
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = dialog.selected_key()
        if selected == "none":
            idx = self.template_combo.findData("")
            if idx >= 0:
                self.template_combo.setCurrentIndex(idx)
            return
        idx = self.template_combo.findData(selected)
        if idx >= 0:
            self.template_combo.setCurrentIndex(idx)
__all__ = [
    "TemplateGalleryDialog",
    "TemplatePrepDialog",
    "TemplateTransparencyDialog",
]
