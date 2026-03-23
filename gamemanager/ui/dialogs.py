from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import quote_plus
import webbrowser

from PySide6.QtCore import (
    QByteArray,
    QBuffer,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QImage,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QWheelEvent,
)
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
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QSpinBox,
    QSlider,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gamemanager.models import (
    IconCandidate,
    InventoryItem,
    MovePlanItem,
    RenamePlanItem,
    TagCandidate,
)
from gamemanager.services.background_removal import (
    BACKGROUND_REMOVAL_OPTIONS,
    DEFAULT_BG_REMOVAL_PARAMS,
    normalize_background_removal_engine,
    normalize_background_removal_params,
    remove_background_bytes,
)
from gamemanager.services.icon_pipeline import (
    BorderShaderConfig,
    TEXT_EXTRACTION_METHOD_OPTIONS,
    TextPreserveConfig,
    build_text_extraction_alpha_mask,
    build_text_extraction_overlay,
    border_shader_to_dict,
    build_template_overlay_preview,
    build_multi_size_ico,
    build_template_interior_mask_png,
    build_preview_png,
    normalize_text_extraction_method,
    normalize_text_preserve_config,
    icon_style_options,
    normalize_icon_style,
    normalize_border_shader_config,
    text_preserve_to_dict,
    resolve_icon_template,
)
from gamemanager.services.image_prep import (
    ImagePrepOptions,
    SUPPORTED_IMAGE_EXTENSIONS,
    apply_min_black_transparency,
    icon_templates_dir,
    normalize_to_square_png,
    prepare_images_to_template_folder,
)
from gamemanager.services.template_transparency import (
    TemplateTransparencyOptions,
    make_background_transparent,
)
from gamemanager.ui.alpha_preview import composite_on_checkerboard, draw_checkerboard

try:
    from PIL import Image, ImageFilter, ImageOps
except ImportError:
    Image = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


@dataclass(slots=True)
class TagReviewResult:
    decisions: dict[str, str]
    display_map: dict[str, str]


@dataclass(slots=True)
class IconPickerResult:
    candidate: IconCandidate | None
    local_image_path: str | None
    source_image_bytes: bytes | None
    prepared_image_bytes: bytes | None
    prepared_is_final_composite: bool
    info_tip: str
    icon_style: str
    bg_removal_engine: str
    bg_removal_params: dict[str, object]
    text_preserve_config: dict[str, object]
    border_shader: dict[str, object]


@dataclass(slots=True)
class IconProviderSettingsResult:
    steamgriddb_enabled: bool
    steamgriddb_api_key: str
    steamgriddb_api_base: str
    iconfinder_enabled: bool
    iconfinder_api_key: str
    iconfinder_api_base: str


@dataclass(slots=True)
class PerformanceSettingsResult:
    scan_size_workers: int
    progress_interval_ms: int
    dir_cache_enabled: bool
    dir_cache_max_entries: int
    startup_prewarm_mode: str


SGDB_RESOURCE_OPTIONS: list[tuple[str, str]] = [
    ("Icon", "icons"),
    ("Logo", "logos"),
    ("Grid", "grids"),
    ("Hero", "heroes"),
]


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


class TemplatePrepDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Template Batch Prep")
        self._paths: list[str] = []
        self._output_dir = icon_templates_dir()
        self._run_in_progress = False
        self._last_processed_background: str | None = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(120)
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
        self.drop_list.setMinimumHeight(170)
        self.drop_list.setToolTip("Drop image files or folders here.")
        layout.addWidget(self.drop_list)

        self.live_preview_label = QLabel("Live preview", self)
        self.live_preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.live_preview_label.setMinimumSize(260, 260)
        self.live_preview_label.setFrameShape(QFrame.Shape.StyledPanel)
        layout.addWidget(self.live_preview_label)

        source_buttons = QHBoxLayout()
        self.add_files_btn = QPushButton("Add Files...", self)
        self.add_files_btn.clicked.connect(self._on_add_files)
        source_buttons.addWidget(self.add_files_btn)
        self.add_folder_btn = QPushButton("Add Folder...", self)
        self.add_folder_btn.clicked.connect(self._on_add_folder)
        source_buttons.addWidget(self.add_folder_btn)
        self.remove_selected_btn = QPushButton("Remove Selected", self)
        self.remove_selected_btn.clicked.connect(self._on_remove_selected)
        source_buttons.addWidget(self.remove_selected_btn)
        self.clear_btn = QPushButton("Clear", self)
        self.clear_btn.clicked.connect(self._on_clear_sources)
        source_buttons.addWidget(self.clear_btn)
        source_buttons.addStretch(1)
        layout.addLayout(source_buttons)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output:", self))
        self.output_edit = QLineEdit(str(self._output_dir), self)
        self.output_edit.setReadOnly(True)
        output_row.addWidget(self.output_edit, 1)
        self.open_output_btn = QPushButton("Open Folder", self)
        self.open_output_btn.clicked.connect(self._on_open_output_folder)
        output_row.addWidget(self.open_output_btn)
        layout.addLayout(output_row)

        params = QHBoxLayout()
        left_col = QVBoxLayout()
        right_col = QVBoxLayout()

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("Output Size:", self))
        self.size_spin = QSpinBox(self)
        self.size_spin.setRange(32, 4096)
        self.size_spin.setValue(512)
        self.size_spin.setSuffix(" px")
        size_row.addWidget(self.size_spin)
        size_row.addStretch(1)
        left_col.addLayout(size_row)

        pad_row = QHBoxLayout()
        pad_row.addWidget(QLabel("Padding Ratio:", self))
        self.padding_ratio_spin = QDoubleSpinBox(self)
        self.padding_ratio_spin.setRange(0.0, 0.5)
        self.padding_ratio_spin.setSingleStep(0.005)
        self.padding_ratio_spin.setDecimals(3)
        self.padding_ratio_spin.setValue(0.0)
        pad_row.addWidget(self.padding_ratio_spin)
        pad_row.addStretch(1)
        left_col.addLayout(pad_row)

        min_pad_row = QHBoxLayout()
        min_pad_row.addWidget(QLabel("Min Padding:", self))
        self.min_padding_spin = QSpinBox(self)
        self.min_padding_spin.setRange(0, 64)
        self.min_padding_spin.setValue(1)
        self.min_padding_spin.setSuffix(" px")
        min_pad_row.addWidget(self.min_padding_spin)
        min_pad_row.addStretch(1)
        left_col.addLayout(min_pad_row)

        alpha_row = QHBoxLayout()
        alpha_row.addWidget(QLabel("Alpha Threshold:", self))
        self.alpha_threshold_spin = QSpinBox(self)
        self.alpha_threshold_spin.setRange(0, 255)
        self.alpha_threshold_spin.setValue(8)
        alpha_row.addWidget(self.alpha_threshold_spin)
        alpha_row.addStretch(1)
        right_col.addLayout(alpha_row)

        border_row = QHBoxLayout()
        border_row.addWidget(QLabel("Border Threshold:", self))
        self.border_threshold_spin = QSpinBox(self)
        self.border_threshold_spin.setRange(0, 255)
        self.border_threshold_spin.setValue(16)
        border_row.addWidget(self.border_threshold_spin)
        border_row.addStretch(1)
        right_col.addLayout(border_row)

        recursive_row = QHBoxLayout()
        self.recursive_check = QCheckBox("Recurse into subfolders", self)
        self.recursive_check.setChecked(True)
        recursive_row.addWidget(self.recursive_check)
        recursive_row.addStretch(1)
        right_col.addLayout(recursive_row)

        black_row = QHBoxLayout()
        black_row.addWidget(QLabel("Min Black Level:", self))
        self.min_black_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.min_black_slider.setRange(0, 30)
        self.min_black_slider.setValue(10)
        self.min_black_slider.setToolTip("Pixels near pure black are made transparent.")
        black_row.addWidget(self.min_black_slider, 1)
        self.min_black_value = QLabel("10", self)
        self.min_black_value.setMinimumWidth(28)
        black_row.addWidget(self.min_black_value)
        right_col.addLayout(black_row)

        params.addLayout(left_col, 1)
        params.addLayout(right_col, 1)
        layout.addLayout(params)

        action_row = QHBoxLayout()
        self.run_btn = QPushButton("Generate Templates", self)
        self.run_btn.clicked.connect(self._on_generate)
        action_row.addWidget(self.run_btn)
        self.progress_label = QLabel("Progress: idle", self)
        action_row.addWidget(self.progress_label)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        self.result_box = QPlainTextEdit(self)
        self.result_box.setReadOnly(True)
        self.result_box.setMinimumHeight(120)
        layout.addWidget(self.result_box)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self.resize(950, 650)

        for widget in (
            self.size_spin,
            self.padding_ratio_spin,
            self.min_padding_spin,
            self.alpha_threshold_spin,
            self.border_threshold_spin,
            self.recursive_check,
            self.min_black_slider,
        ):
            if isinstance(widget, QCheckBox):
                widget.toggled.connect(self._schedule_live_preview)
            else:
                widget.valueChanged.connect(self._schedule_live_preview)
        self.min_black_slider.valueChanged.connect(
            lambda v: self.min_black_value.setText(str(int(v)))
        )
        self._schedule_live_preview()

    def _append_result(self, text: str) -> None:
        existing = self.result_box.toPlainText().strip()
        merged = f"{existing}\n{text}".strip() if existing else text
        self.result_box.setPlainText(merged)
        self.result_box.verticalScrollBar().setValue(
            self.result_box.verticalScrollBar().maximum()
        )

    def _refresh_list(self) -> None:
        self.drop_list.clear()
        for path in self._paths:
            item = QListWidgetItem(path)
            item.setToolTip(path)
            self.drop_list.addItem(item)
        self._schedule_live_preview()

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

    def _resolve_preview_source(self) -> Path | None:
        selected_items = self.drop_list.selectedItems()
        candidate_path = selected_items[0].text() if selected_items else (
            self._paths[0] if self._paths else ""
        )
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
            self.live_preview_label.setText("Live preview\n(no image selected)")
            self.live_preview_label.setPixmap(QPixmap())
            return
        try:
            payload = source.read_bytes()
            payload = apply_min_black_transparency(
                payload,
                min_black_level=int(self.min_black_slider.value()),
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
            min_black_level=int(self.min_black_slider.value()),
            recursive=self.recursive_check.isChecked(),
        )
        self._last_processed_background = None

        def _on_progress(
            current: int,
            total: int,
            source_path: str,
            destination_path: str,
            status: str,
            produced_path: str | None,
            error_text: str | None,
        ) -> None:
            source_name = Path(source_path).name
            if status == "succeeded" and produced_path:
                self._last_processed_background = produced_path
                self.drop_list.set_background_preview(produced_path)
                self.progress_label.setText(
                    f"Progress: {current}/{total} (ok) {source_name}"
                )
            elif status == "failed":
                self.progress_label.setText(
                    f"Progress: {current}/{total} (failed) {source_name}"
                )
            else:
                self.progress_label.setText(
                    f"Progress: {current}/{total} ({status}) {source_name}"
                )
            QApplication.processEvents()

        report = None
        try:
            report = prepare_images_to_template_folder(
                input_paths=list(self._paths),
                options=options,
                output_dir=str(self._output_dir),
                progress_cb=_on_progress,
            )
            if self._last_processed_background:
                self.drop_list.set_background_preview(self._last_processed_background)
            lines = [
                f"Attempted: {report.attempted}",
                f"Succeeded: {report.succeeded}",
                f"Failed: {report.failed}",
                f"Skipped: {report.skipped}",
            ]
            if report.output_files:
                lines.append("")
                lines.append("Generated:")
                lines.extend(report.output_files[:30])
            if report.details:
                lines.append("")
                lines.append("Details:")
                lines.extend(report.details[:30])
            self.result_box.setPlainText("\n".join(lines))
            self.progress_label.setText(
                f"Progress: {report.attempted}/{report.attempted} complete"
            )
        finally:
            self._run_in_progress = False
            self._set_run_controls_enabled(True)
        if report is None:
            QMessageBox.warning(self, "Template Prep", "Run failed unexpectedly.")
            return
        if report.failed:
            QMessageBox.warning(
                self,
                "Template Prep",
                f"Completed with errors. Failed: {report.failed}",
            )
            return
        QMessageBox.information(
            self,
            "Template Prep",
            f"Completed. Generated: {report.succeeded}",
        )


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

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Select an existing template, adjust black removal level, then Apply to save."
            )
        )

        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Template:", self))
        self.template_combo = QComboBox(self)
        self.template_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToContentsOnFirstShow
        )
        top_row.addWidget(self.template_combo, 1)
        self.reload_btn = QPushButton("Reload", self)
        self.reload_btn.clicked.connect(self._reload_templates)
        top_row.addWidget(self.reload_btn)
        layout.addLayout(top_row)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("Max Black Level:", self))
        self.black_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.black_slider.setRange(0, 30)
        self.black_slider.setValue(10)
        slider_row.addWidget(self.black_slider, 1)
        self.black_value = QLabel("10", self)
        self.black_value.setMinimumWidth(28)
        slider_row.addWidget(self.black_value)
        layout.addLayout(slider_row)

        self.preview_label = QLabel("No template selected.", self)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(420, 420)
        self.preview_label.setFrameShape(QFrame.Shape.StyledPanel)
        layout.addWidget(self.preview_label, 1)

        actions = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel", self)
        self.cancel_btn.clicked.connect(self._on_cancel_changes)
        actions.addWidget(self.cancel_btn)
        self.apply_btn = QPushButton("Apply", self)
        self.apply_btn.clicked.connect(self._on_apply_changes)
        actions.addWidget(self.apply_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.status_label = QLabel("", self)
        layout.addWidget(self.status_label)
        self.resize(900, 760)

        self.template_combo.currentIndexChanged.connect(self._on_template_changed)
        self.black_slider.valueChanged.connect(self._on_black_level_changed)
        self._reload_templates()

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _reload_templates(self) -> None:
        self._template_dir.mkdir(parents=True, exist_ok=True)
        files = sorted(self._template_dir.glob("*.png"), key=lambda p: p.name.casefold())
        self._template_files = files
        current = str(self._current_path) if self._current_path else ""
        self._loading = True
        self.template_combo.clear()
        for path in files:
            self.template_combo.addItem(path.name, str(path))
        self._loading = False
        if not files:
            self._current_path = None
            self._original_bytes = None
            self._preview_bytes = None
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("No templates found in IconTemplates.")
            self._set_status("")
            self._update_action_buttons()
            return
        target_idx = 0
        if current:
            for idx, path in enumerate(files):
                if str(path) == current:
                    target_idx = idx
                    break
        was_blocked = self.template_combo.blockSignals(True)
        self.template_combo.setCurrentIndex(target_idx)
        self.template_combo.blockSignals(was_blocked)
        self._last_combo_index = target_idx
        self._load_template(files[target_idx])

    def _on_black_level_changed(self, value: int) -> None:
        self.black_value.setText(str(int(value)))
        self._rebuild_preview()

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
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Could not decode image preview.")
            return
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
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("No template selected.")
            self._update_action_buttons()
            return
        level = int(self.black_slider.value())
        if level <= 0:
            self._preview_bytes = self._original_bytes
        else:
            try:
                self._preview_bytes = make_background_transparent(
                    self._original_bytes,
                    options=TemplateTransparencyOptions(
                        threshold=max(0, min(30, level)),
                        color_tolerance_mode="max",
                        use_edge_flood_fill=True,
                        use_center_flood_fill=True,
                        preserve_existing_alpha=True,
                    ),
                    background_color=(0, 0, 0),
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
        selected_path = Path(str(selected_data)) if selected_data else None
        if selected_path is None:
            return
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


class TagReviewDialog(QDialog):
    def __init__(
        self,
        candidates: list[TagCandidate],
        approved_tags: set[str],
        non_tags: set[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Tag Finder")
        self._combos: dict[str, QComboBox] = {}
        self._display: dict[str, str] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Review suffix tag candidates and classify as approved or non-tag.")
        )
        self.table = QTableWidget(len(candidates), 4, self)
        self.table.setHorizontalHeaderLabels(["Tag", "Count", "Example", "Decision"])
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        for row, candidate in enumerate(candidates):
            canonical = candidate.canonical_tag
            self._display[canonical] = candidate.observed_tag

            self.table.setItem(row, 0, QTableWidgetItem(candidate.observed_tag))
            self.table.setItem(row, 1, QTableWidgetItem(str(candidate.count)))
            self.table.setItem(row, 2, QTableWidgetItem(candidate.example_name))
            combo = QComboBox(self.table)
            combo.addItems(["ignore", "approved", "non_tag"])
            if canonical in approved_tags:
                combo.setCurrentText("approved")
            elif canonical in non_tags:
                combo.setCurrentText("non_tag")
            else:
                combo.setCurrentText("ignore")
            self.table.setCellWidget(row, 3, combo)
            self._combos[canonical] = combo
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def result_payload(self) -> TagReviewResult:
        decisions: dict[str, str] = {}
        for canonical, combo in self._combos.items():
            choice = combo.currentText()
            if choice in {"approved", "non_tag"}:
                decisions[canonical] = choice
        return TagReviewResult(decisions=decisions, display_map=self._display)


class CleanupPreviewDialog(QDialog):
    def __init__(self, plan_items: list[RenamePlanItem], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Cleanup Preview")
        self.plan_items = plan_items
        self.visible_items = [item for item in plan_items if item.status != "unchanged"]
        self._action_by_item_id: dict[int, QComboBox] = {}

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Safe cleanup will rename non-conflicting root-level items on disk. "
                "Conflicts are flagged for manual rename."
            )
        )

        self.table = QTableWidget(len(self.visible_items), 4, self)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Proposed Name", "Destination", "Status"]
        )
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        for row, item in enumerate(self.visible_items):
            status = item.status
            self.table.setItem(row, 0, QTableWidgetItem(str(item.src_path)))
            self.table.setItem(row, 1, QTableWidgetItem(item.proposed_name))
            self.table.setItem(row, 2, QTableWidgetItem(str(item.dst_path)))
            if status == "ready":
                combo = QComboBox(self.table)
                combo.addItems(["Rename", "Skip"])
                combo.setCurrentText("Rename")
                self.table.setCellWidget(row, 3, combo)
                self._action_by_item_id[id(item)] = combo
            else:
                self.table.setItem(
                    row, 3, QTableWidgetItem("Conflict - manual rename required")
                )
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(self)
        self.apply_btn = QPushButton("Apply Safe Renames")
        self.cancel_btn = QPushButton("Cancel")
        buttons.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def safe_items(self) -> list[RenamePlanItem]:
        selected: list[RenamePlanItem] = []
        for item in self.plan_items:
            if item.status != "ready":
                continue
            combo = self._action_by_item_id.get(id(item))
            if combo is None or combo.currentText() == "Rename":
                selected.append(item)
        return selected


class MovePreviewDialog(QDialog):
    def __init__(self, plan_items: list[MovePlanItem], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Archive Move Preview")
        self.plan_items = plan_items
        self._widgets: list[tuple[QComboBox, QLineEdit]] = []

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Preview archive/ISO moves into same-name subfolders. "
                "Conflicts default to skip."
            )
        )

        self.table = QTableWidget(len(plan_items), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Source", "Destination", "Status", "Action", "Manual Name"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setWordWrap(True)
        for row, item in enumerate(plan_items):
            self.table.setItem(row, 0, QTableWidgetItem(str(item.src_path)))
            self.table.setItem(row, 1, QTableWidgetItem(str(item.dst_path)))
            status_text = (
                "Ready to move"
                if item.status == "ready"
                else f"Conflict: {item.conflict_type or 'unknown'}"
            )
            self.table.setItem(row, 2, QTableWidgetItem(status_text))

            combo = QComboBox(self.table)
            if item.status == "ready":
                combo.addItems(["move", "skip"])
                combo.setCurrentText("move")
            else:
                combo.addItems(["skip", "overwrite", "rename", "delete_destination"])
                combo.setCurrentText("skip")
            self.table.setCellWidget(row, 3, combo)

            line = QLineEdit(self.table)
            line.setPlaceholderText("Only for action=rename")
            line.setEnabled(False)
            combo.currentTextChanged.connect(
                lambda text, edit=line: edit.setEnabled(text == "rename")
            )
            self.table.setCellWidget(row, 4, line)
            self._widgets.append((combo, line))

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(self)
        self.apply_btn = QPushButton("Execute Moves")
        self.cancel_btn = QPushButton("Cancel")
        buttons.addButton(self.apply_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.apply_btn.clicked.connect(self._validate_then_accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _validate_then_accept(self) -> None:
        for idx, (combo, line) in enumerate(self._widgets):
            if combo.currentText() == "rename" and not line.text().strip():
                QMessageBox.warning(
                    self,
                    "Missing Manual Name",
                    f"Row {idx + 1} uses action 'rename' but manual name is empty.",
                )
                return
        self.accept()

    def applied_items(self) -> list[MovePlanItem]:
        for idx, item in enumerate(self.plan_items):
            combo, line = self._widgets[idx]
            item.selected_action = combo.currentText()
            manual = line.text().strip()
            item.manual_name = manual if manual else None
        return self.plan_items


def _is_supported_image_path(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def _normalize_image_bytes_for_canvas(payload: bytes) -> bytes:
    # Re-encode to PNG to avoid Qt decoder edge-cases on some web images.
    if not payload or Image is None or ImageOps is None:
        return payload
    try:
        with Image.open(BytesIO(payload)) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.mode not in {"RGB", "RGBA"}:
                img = img.convert("RGBA")
            out = BytesIO()
            img.save(out, format="PNG")
            return out.getvalue()
    except Exception:
        return payload


UPSCALE_METHOD_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Qt Smooth (fast)", "qt_smooth"),
    ("Pillow Bicubic", "bicubic"),
    ("Pillow Lanczos", "lanczos"),
    ("Pillow Lanczos + Unsharp", "lanczos_unsharp"),
)


def _normalize_upscale_method(method: str | None) -> str:
    value = str(method or "qt_smooth").strip().casefold()
    valid = {item[1] for item in UPSCALE_METHOD_OPTIONS}
    return value if value in valid else "qt_smooth"


def _google_image_search_url(query: str) -> str:
    if not query.strip():
        query = "game icon png transparent"
    return f"https://www.google.com/search?tbm=isch&q={quote_plus(query)}"


def _project_data_dir() -> Path:
    override = os.environ.get("GAMEMANAGER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    project_root = Path(__file__).resolve().parents[2]
    return project_root / ".gamemanager_data"


def _web_capture_session_root() -> Path:
    legacy = _project_data_dir() / "web_capture_sessions"
    if legacy.exists() and legacy.is_dir():
        shutil.rmtree(legacy, ignore_errors=True)
    root = _project_data_dir() / "web_capture_session"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _empty_directory(path: Path) -> None:
    if not path.exists():
        return
    for entry in list(path.iterdir()):
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        except OSError:
            continue


def _web_capture_session_dir() -> Path:
    session_dir = _web_capture_session_root()
    return session_dir


def _cleanup_web_capture_session() -> None:
    _empty_directory(_web_capture_session_root())


class ExternalDownloadWatcher:
    def __init__(
        self,
        downloads_dir: Path,
        poll_seconds: float = 1.0,
        on_detect: Callable[[Path], str | None] | None = None,
    ):
        self._downloads_dir = downloads_dir
        self._poll_seconds = max(0.25, poll_seconds)
        self._on_detect = on_detect
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._baseline: dict[str, tuple[int, int]] = {}
        self._captured_paths: set[str] = set()
        self._processed_sources: set[str] = set()
        self._new_paths: list[str] = []
        self._start_ts = 0.0
        self._end_ts: float | None = None

    @staticmethod
    def _is_image(path: Path) -> bool:
        return path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        if not self._downloads_dir.exists():
            return snapshot
        try:
            with os.scandir(self._downloads_dir) as entries:
                for entry in entries:
                    if not entry.is_file():
                        continue
                    path = Path(entry.path)
                    if not self._is_image(path):
                        continue
                    try:
                        stat = entry.stat()
                    except OSError:
                        continue
                    snapshot[str(path.resolve())] = (int(stat.st_size), int(stat.st_mtime_ns))
        except OSError:
            return snapshot
        return snapshot

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._baseline = self._snapshot()
        self._captured_paths.clear()
        self._processed_sources.clear()
        self._new_paths.clear()
        self._start_ts = time.time()
        self._end_ts = None
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _capture_from_snapshot(
        self,
        snapshot: dict[str, tuple[int, int]],
        *,
        final_pass: bool = False,
    ) -> None:
        end_ts = self._end_ts
        for path_str, payload in snapshot.items():
            size, mtime_ns = payload
            baseline_payload = self._baseline.get(path_str)
            if baseline_payload == (size, mtime_ns):
                continue
            if path_str in self._processed_sources:
                continue
            mtime_s = mtime_ns / 1_000_000_000
            if mtime_s + 2.0 < self._start_ts:
                continue
            if end_ts is not None and mtime_s > end_ts + 2.0:
                continue
            # Avoid moving while the browser is still writing.
            if not final_pass and (time.time() - mtime_s) < 1.0:
                continue
            source_path = Path(path_str)
            if not source_path.exists():
                continue
            captured_path = path_str
            if self._on_detect is not None:
                captured_path = self._on_detect(source_path) or ""
                if not captured_path:
                    continue
            with self._lock:
                self._captured_paths.add(captured_path)
                self._processed_sources.add(path_str)
                self._new_paths.append(captured_path)

    def _run(self) -> None:
        while not self._stop_event.wait(self._poll_seconds):
            snap = self._snapshot()
            self._capture_from_snapshot(snap)

    def stop(self) -> list[str]:
        self._end_ts = time.time()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        final_snapshot = self._snapshot()
        self._capture_from_snapshot(final_snapshot, final_pass=True)
        with self._lock:
            captured = [p for p in sorted(self._captured_paths) if Path(p).exists()]
        return captured

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def pop_new_paths(self) -> list[str]:
        with self._lock:
            out = list(self._new_paths)
            self._new_paths.clear()
        return [path for path in out if Path(path).exists()]


class WebDownloadCaptureDialog(QDialog):
    def __init__(
        self,
        query: str,
        parent: QWidget | None = None,
        selection_callback: Callable[[str], None] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Web Image Capture")
        self._capture_dir = _web_capture_session_dir()
        self._external_download_dir = Path(r"E:\Downloads")
        self._external_watcher: ExternalDownloadWatcher | None = None
        self._external_stage_lock = threading.Lock()
        self._selection_callback = selection_callback
        self._captured_files: list[str] = []
        self._thumb_icon_cache: dict[str, QIcon] = {}
        self._tearing_down = False

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Open your browser, download images, then pick one from the captured list."
            )
        )

        url_row = QHBoxLayout()
        self.url_edit = QLineEdit(_google_image_search_url(query), self)
        url_row.addWidget(self.url_edit, 1)
        self.open_btn = QPushButton("Open External", self)
        self.open_btn.clicked.connect(self._open_url)
        url_row.addWidget(self.open_btn)
        self.open_external_btn = QPushButton("Open External + Capture", self)
        self.open_external_btn.clicked.connect(self._toggle_external_capture)
        self.open_external_btn.setToolTip(
            "Open in your default browser and capture new image downloads from E:\\Downloads."
        )
        url_row.addWidget(self.open_external_btn)
        layout.addLayout(url_row)

        mode_note = QLabel(
            "Embedded browser capture is disabled. "
            "Use external browser capture for stability.",
            self,
        )
        mode_note.setWordWrap(True)
        layout.addWidget(mode_note)

        self.download_table = QTableWidget(0, 4, self)
        self.download_table.setHorizontalHeaderLabels(["Preview", "File", "Size", "Status"])
        self.download_table.verticalHeader().setVisible(False)
        self.download_table.horizontalHeader().setStretchLastSection(True)
        self.download_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.download_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.download_table.setIconSize(QSize(64, 64))
        self.download_table.itemDoubleClicked.connect(
            lambda _item: self._keep_selected_capture(close_after=True)
        )
        layout.addWidget(self.download_table)

        self.status_label = QLabel("Captured images: 0", self)
        layout.addWidget(self.status_label)
        self.auto_focus_check = QCheckBox("Auto-focus on new capture", self)
        self.auto_focus_check.setChecked(True)
        self.auto_focus_check.setToolTip(
            "When enabled, bring this window to front when a new downloaded image is captured."
        )
        layout.addWidget(self.auto_focus_check)

        buttons = QDialogButtonBox(self)
        self.keep_selected_btn = QPushButton("Keep Selected")
        self.keep_all_btn = QPushButton("Keep All")
        self.done_btn = QPushButton("Close Browser")
        self.cancel_btn = QPushButton("Cancel")
        buttons.addButton(self.keep_selected_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.keep_all_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(self.done_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.keep_selected_btn.clicked.connect(
            lambda: self._keep_selected_capture(close_after=True)
        )
        self.keep_all_btn.clicked.connect(self._keep_all_captures)
        self.done_btn.clicked.connect(self._validate_then_accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)

        self._external_poll_timer = QTimer(self)
        self._external_poll_timer.setInterval(600)
        self._external_poll_timer.timeout.connect(self._poll_external_updates)
        self._external_poll_timer.start()

        self._ingest_existing_capture_files()
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        QTimer.singleShot(0, self._auto_start_external_capture)

    def _refresh_capture_status_label(self) -> None:
        text = f"Captured images: {len(self._captured_files)}"
        if self._external_watcher is not None and self._external_watcher.is_running():
            text += f" | External capture active ({self._external_download_dir})"
        self.status_label.setText(text)

    def _poll_external_updates(self) -> None:
        try:
            watcher = self._external_watcher
            if watcher is None or not watcher.is_running():
                return
            self._ingest_external_captures(watcher.pop_new_paths())
        except Exception:
            return

    def _selected_capture_path(self) -> str | None:
        row = self.download_table.currentRow()
        if row < 0:
            return None
        file_item = self.download_table.item(row, 1)
        preview_item = self.download_table.item(row, 0)
        item = file_item or preview_item
        if item is None and preview_item is None:
            return None
        path = str((item.data(Qt.ItemDataRole.UserRole) if item is not None else "") or "").strip()
        if not path and preview_item is not None and preview_item is not item:
            path = str(preview_item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if path:
            resolved = self._resolve_existing_capture_path(path)
            if resolved is not None:
                if item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, resolved)
                if preview_item is not None:
                    preview_item.setData(Qt.ItemDataRole.UserRole, resolved)
                return resolved
        # Fallback by filename shown in table if role path is missing/stale.
        fallback_name = (file_item.text().strip() if file_item is not None else "").strip()
        if fallback_name:
            resolved = self._resolve_existing_capture_by_name(fallback_name)
            if resolved is not None:
                if item is not None:
                    item.setData(Qt.ItemDataRole.UserRole, resolved)
                if preview_item is not None:
                    preview_item.setData(Qt.ItemDataRole.UserRole, resolved)
                return resolved
        return None

    def _keep_selected_capture(self, close_after: bool) -> None:
        path = self._selected_capture_path()
        if not path:
            QMessageBox.information(
                self,
                "No Selection",
                "Select a completed captured image first.",
            )
            return
        if self._selection_callback is not None:
            try:
                self._selection_callback(path)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Keep Selected Failed",
                    f"Could not keep selected image:\n{exc}",
                )
                return
        if close_after:
            self._validate_then_accept()

    def _keep_all_captures(self) -> None:
        # Keep all staged captures and optionally set a selected/default one
        # as the current source for the icon picker.
        selected_path = self._selected_capture_path()
        if selected_path is None:
            existing = self.captured_files()
            if existing:
                selected_path = existing[-1]
        if selected_path and self._selection_callback is not None:
            try:
                self._selection_callback(selected_path)
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Keep All Failed",
                    f"Could not keep captured images:\n{exc}",
                )
                return
        self._validate_then_accept()

    def _open_url(self) -> None:
        target = self.url_edit.text().strip()
        if not target:
            return
        self._open_external_browser()

    def _open_external_browser(self) -> None:
        target = self.url_edit.text().strip()
        if not target:
            return
        try:
            # Prefer a new browser window (not a tab) when launching external search.
            webbrowser.open(target, new=1)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Open External Browser Failed",
                f"Could not open your default browser:\n{exc}",
            )

    def _toggle_external_capture(self) -> None:
        if self._external_watcher is not None and self._external_watcher.is_running():
            self._stop_external_capture(import_results=True)
            return
        if not self._external_download_dir.exists():
            QMessageBox.warning(
                self,
                "Download Folder Missing",
                f"Configured download folder does not exist:\n{self._external_download_dir}",
            )
            return
        self._external_watcher = ExternalDownloadWatcher(
            self._external_download_dir,
            on_detect=self._stage_external_capture,
        )
        started = self._external_watcher.start()
        if not started:
            QMessageBox.warning(
                self,
                "External Capture",
                "External capture watcher is already running.",
            )
            return
        self.open_external_btn.setText("Stop External Capture")
        self.open_external_btn.setToolTip(
            "Stop the external browser capture session and import matching image files."
        )
        self._refresh_capture_status_label()
        self._open_external_browser()

    def _auto_start_external_capture(self) -> None:
        if self._tearing_down:
            return
        if self._external_watcher is not None and self._external_watcher.is_running():
            return
        self._toggle_external_capture()

    def _stop_external_capture(self, import_results: bool) -> None:
        watcher = self._external_watcher
        if watcher is None:
            return
        self._external_watcher = None
        paths = watcher.stop()
        if import_results:
            self._ingest_external_captures(paths)
        self.open_external_btn.setText("Open External + Capture")
        self.open_external_btn.setToolTip(
            "Open in your default browser and capture new image downloads from E:\\Downloads."
        )
        self._refresh_capture_status_label()

    def _stage_external_capture(self, source_path: Path) -> str | None:
        if not source_path.exists() or not _is_supported_image_path(source_path):
            return None
        with self._external_stage_lock:
            target_name = self._unique_name(source_path.name)
            target_path = self._capture_dir / target_name
        for _ in range(4):
            try:
                moved = Path(shutil.move(str(source_path), str(target_path)))
                return str(moved.resolve())
            except OSError:
                time.sleep(0.25)
        try:
            shutil.copy2(str(source_path), str(target_path))
            try:
                source_path.unlink()
            except OSError:
                pass
            return str(target_path.resolve())
        except OSError:
            return None

    def _ingest_external_captures(self, paths: list[str]) -> None:
        existing = {Path(path).resolve() for path in self._captured_files if Path(path).exists()}
        added = 0
        first_added_row = -1
        for path_str in paths:
            path = Path(path_str)
            if not path.exists() or not _is_supported_image_path(path):
                continue
            resolved = path.resolve()
            if resolved in existing:
                continue
            existing.add(resolved)
            self._captured_files.append(str(resolved))
            row = self.download_table.rowCount()
            self.download_table.insertRow(row)
            self.download_table.setRowHeight(row, 72)
            preview_item = QTableWidgetItem("")
            preview_item.setData(Qt.ItemDataRole.UserRole, str(resolved))
            preview_item.setIcon(self._thumbnail_icon_for_path(path))
            self.download_table.setItem(row, 0, preview_item)
            file_item = QTableWidgetItem(path.name)
            file_item.setData(Qt.ItemDataRole.UserRole, str(resolved))
            self.download_table.setItem(row, 1, file_item)
            try:
                size_text = f"{path.stat().st_size // 1024} KB"
            except OSError:
                size_text = "?"
            self.download_table.setItem(row, 2, QTableWidgetItem(size_text))
            self.download_table.setItem(row, 3, QTableWidgetItem("Captured (External, moved)"))
            if first_added_row < 0:
                first_added_row = row
            added += 1
        if added:
            self._focus_on_capture_added(first_added_row)
            self._refresh_capture_status_label()

    def _ingest_existing_capture_files(self) -> None:
        if not self._capture_dir.exists():
            return
        existing = [str(path.resolve()) for path in sorted(self._capture_dir.glob("*"))]
        self._ingest_external_captures(existing)

    def _thumbnail_icon_for_path(self, path: Path) -> QIcon:
        try:
            stat = path.stat()
            cache_key = f"{path.resolve()}::{int(stat.st_mtime_ns)}::{int(stat.st_size)}"
        except OSError:
            cache_key = str(path)
        cached = self._thumb_icon_cache.get(cache_key)
        if cached is not None:
            return cached
        pix = QPixmap(str(path))
        if pix.isNull():
            icon = QIcon()
            self._thumb_icon_cache[cache_key] = icon
            return icon
        composed = composite_on_checkerboard(
            pix,
            width=64,
            height=64,
            keep_aspect=True,
        )
        icon = QIcon(composed)
        self._thumb_icon_cache[cache_key] = icon
        return icon

    def _focus_on_capture_added(self, row: int) -> None:
        if row >= 0:
            blocked = self.download_table.blockSignals(True)
            self.download_table.selectRow(row)
            self.download_table.blockSignals(blocked)
            self.download_table.scrollToItem(self.download_table.item(row, 1))
        if not self.auto_focus_check.isChecked():
            return
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _unique_name(self, filename: str) -> str:
        candidate = filename
        stem = Path(filename).stem or "image"
        suffix = Path(filename).suffix
        idx = 2
        while (self._capture_dir / candidate).exists():
            candidate = f"{stem}_{idx}{suffix}"
            idx += 1
        return candidate

    def _resolve_existing_capture_path(self, path: str) -> str | None:
        candidate = Path(path)
        if candidate.exists():
            return str(candidate)
        # If Qt finalized the file under capture dir with same filename, reuse it.
        if candidate.name:
            fallback = self._capture_dir / candidate.name
            if fallback.exists():
                return str(fallback)
        return None

    def _resolve_existing_capture_by_name(self, filename: str) -> str | None:
        direct = self._capture_dir / filename
        if direct.exists():
            return str(direct)
        for path_str in reversed(self._captured_files):
            path = Path(path_str)
            if path.exists() and path.name.casefold() == filename.casefold():
                return str(path)
        return None

    def _validate_then_accept(self) -> None:
        self._stop_external_capture(import_results=True)
        if not self._captured_files:
            answer = QMessageBox.question(
                self,
                "No Captured Image",
                "No image download was captured. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def shutdown_session(self) -> None:
        self._shutdown_web_capture(import_results=True)
        self.accept()

    def captured_files(self) -> list[str]:
        return [path for path in self._captured_files if Path(path).exists()]

    def reject(self) -> None:  # type: ignore[override]
        self._shutdown_web_capture(import_results=False)
        super().reject()

    def _shutdown_web_capture(self, import_results: bool) -> None:
        if self._tearing_down:
            return
        self._tearing_down = True
        self._suspend_web_capture(import_results=import_results)

    def _suspend_web_capture(self, import_results: bool) -> None:
        try:
            self._external_poll_timer.stop()
        except Exception:
            pass

        self._stop_external_capture(import_results=import_results)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._shutdown_web_capture(import_results=False)
        super().closeEvent(event)


class DownloadedImagePickerDialog(QDialog):
    def __init__(self, image_paths: list[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("Select Captured Image")
        self._image_paths = [p for p in image_paths if Path(p).exists()]

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select one downloaded image to use as icon source."))
        self.table = QTableWidget(len(self._image_paths), 3, self)
        self.table.setHorizontalHeaderLabels(["Preview", "File", "Path"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setIconSize(QSize(64, 64))
        for row, path_str in enumerate(self._image_paths):
            path = Path(path_str)
            preview_item = QTableWidgetItem("")
            pix = QPixmap(path_str)
            if not pix.isNull():
                preview_item.setIcon(
                    QIcon(
                        composite_on_checkerboard(
                            pix,
                            width=64,
                            height=64,
                            keep_aspect=True,
                        )
                    )
                )
            self.table.setItem(row, 0, preview_item)
            self.table.setItem(row, 1, QTableWidgetItem(path.name))
            path_item = QTableWidgetItem(path_str)
            path_item.setToolTip(path_str)
            self.table.setItem(row, 2, path_item)
            self.table.setRowHeight(row, 72)
        self.table.setColumnWidth(0, 84)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemDoubleClicked.connect(lambda _item: self.accept())
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_then_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _validate_then_accept(self) -> None:
        if self.table.currentRow() < 0:
            QMessageBox.information(self, "No Selection", "Select one downloaded image.")
            return
        self.accept()

    def selected_path(self) -> str | None:
        row = self.table.currentRow()
        if row < 0 or row >= len(self._image_paths):
            return None
        return self._image_paths[row]


def _shader_tone_label(mode: str) -> str:
    return "Lightness" if mode == "hsl" else "Value"


def _shader_swatch_css(rgb: tuple[int, int, int]) -> str:
    red, green, blue = rgb
    return (
        "QPushButton {"
        f" background-color: rgb({red}, {green}, {blue});"
        " border: 1px solid #555;"
        " min-width: 28px;"
        " min-height: 18px;"
        " }"
    )


class BorderShaderControls(QWidget):
    def __init__(
        self,
        initial_config: dict[str, object] | BorderShaderConfig | None = None,
        on_change: Callable[[dict[str, object]], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._on_change = on_change
        cfg = normalize_border_shader_config(initial_config)
        self._updating = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.enable_checkbox = QCheckBox("Enable Border Shader", self)
        self.enable_checkbox.setChecked(cfg.enabled)
        layout.addWidget(self.enable_checkbox)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Mode:", self))
        self.mode_combo = QComboBox(self)
        self.mode_combo.addItem("HSV", "hsv")
        self.mode_combo.addItem("HSL", "hsl")
        mode_idx = self.mode_combo.findData(cfg.mode)
        if mode_idx >= 0:
            self.mode_combo.setCurrentIndex(mode_idx)
        mode_row.addWidget(self.mode_combo)
        mode_row.addWidget(QLabel("Color:", self))
        self.color_btn = QPushButton("", self)
        self.color_btn.setFixedWidth(34)
        mode_row.addWidget(self.color_btn)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.hue_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.hue_slider.setRange(0, 359)
        self.hue_slider.setValue(cfg.hue)
        self.sat_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.sat_slider.setRange(0, 100)
        self.sat_slider.setValue(cfg.saturation)
        self.tone_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.tone_slider.setRange(0, 100)
        self.tone_slider.setValue(cfg.tone)
        self.intensity_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.intensity_slider.setRange(0, 100)
        self.intensity_slider.setValue(cfg.intensity)

        self.hue_value = QLabel(str(cfg.hue), self)
        self.sat_value = QLabel(str(cfg.saturation), self)
        self.tone_value = QLabel(str(cfg.tone), self)
        self.intensity_value = QLabel(str(cfg.intensity), self)
        self.tone_label = QLabel(_shader_tone_label(cfg.mode), self)

        self._add_slider_row(layout, "Hue", self.hue_slider, self.hue_value)
        self._add_slider_row(layout, "Saturation", self.sat_slider, self.sat_value)
        self._add_slider_row(layout, None, self.tone_slider, self.tone_value, dynamic_label=self.tone_label)
        self._add_slider_row(layout, "Intensity", self.intensity_slider, self.intensity_value)

        self.enable_checkbox.toggled.connect(self._on_controls_changed)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        self.color_btn.clicked.connect(self._pick_color)
        self.hue_slider.valueChanged.connect(self._on_controls_changed)
        self.sat_slider.valueChanged.connect(self._on_controls_changed)
        self.tone_slider.valueChanged.connect(self._on_controls_changed)
        self.intensity_slider.valueChanged.connect(self._on_controls_changed)

        self._refresh_ui()

    def _add_slider_row(
        self,
        layout: QVBoxLayout,
        label_text: str | None,
        slider: QSlider,
        value_label: QLabel,
        dynamic_label: QLabel | None = None,
    ) -> None:
        row = QHBoxLayout()
        if dynamic_label is not None:
            row.addWidget(dynamic_label)
        else:
            row.addWidget(QLabel(label_text or "", self))
        row.addWidget(slider, 1)
        value_label.setMinimumWidth(28)
        value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(value_label)
        layout.addLayout(row)

    def _current_mode(self) -> str:
        return str(self.mode_combo.currentData() or "hsv")

    def _current_rgb(self) -> tuple[int, int, int]:
        cfg = normalize_border_shader_config(self.config())
        color = QColor()
        if cfg.mode == "hsl":
            color.setHsl(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        else:
            color.setHsv(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        return color.red(), color.green(), color.blue()

    def _pick_color(self) -> None:
        red, green, blue = self._current_rgb()
        selected = QColorDialog.getColor(QColor(red, green, blue), self, "Border Tint")
        if not selected.isValid():
            return
        self._updating = True
        mode = self._current_mode()
        if mode == "hsl":
            hue, sat, light, _alpha = selected.getHsl()
            self.hue_slider.setValue(max(0, hue))
            self.sat_slider.setValue(max(0, min(100, int(round(sat * 100 / 255)))))
            self.tone_slider.setValue(max(0, min(100, int(round(light * 100 / 255)))))
        else:
            hue, sat, val, _alpha = selected.getHsv()
            self.hue_slider.setValue(max(0, hue))
            self.sat_slider.setValue(max(0, min(100, int(round(sat * 100 / 255)))))
            self.tone_slider.setValue(max(0, min(100, int(round(val * 100 / 255)))))
        self._updating = False
        self._on_controls_changed()

    def _on_mode_changed(self, *_args) -> None:
        self.tone_label.setText(_shader_tone_label(self._current_mode()))
        self._on_controls_changed()

    def _refresh_ui(self) -> None:
        self.hue_value.setText(str(self.hue_slider.value()))
        self.sat_value.setText(str(self.sat_slider.value()))
        self.tone_value.setText(str(self.tone_slider.value()))
        self.intensity_value.setText(str(self.intensity_slider.value()))
        self.color_btn.setStyleSheet(_shader_swatch_css(self._current_rgb()))
        enabled = self.enable_checkbox.isChecked()
        for widget in (
            self.mode_combo,
            self.color_btn,
            self.hue_slider,
            self.sat_slider,
            self.tone_slider,
            self.intensity_slider,
        ):
            widget.setEnabled(enabled)

    def _on_controls_changed(self, *_args) -> None:
        if self._updating:
            return
        self._refresh_ui()
        if self._on_change is not None:
            self._on_change(self.config())

    def config(self) -> dict[str, object]:
        return border_shader_to_dict(
            BorderShaderConfig(
                enabled=self.enable_checkbox.isChecked(),
                mode=self._current_mode(),
                hue=int(self.hue_slider.value()),
                saturation=int(self.sat_slider.value()),
                tone=int(self.tone_slider.value()),
                intensity=int(self.intensity_slider.value()),
            )
        )

    def set_config(self, config: dict[str, object] | BorderShaderConfig | None) -> None:
        cfg = normalize_border_shader_config(config)
        self._updating = True
        self.enable_checkbox.setChecked(cfg.enabled)
        idx = self.mode_combo.findData(cfg.mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)
        self.hue_slider.setValue(cfg.hue)
        self.sat_slider.setValue(cfg.saturation)
        self.tone_slider.setValue(cfg.tone)
        self.intensity_slider.setValue(cfg.intensity)
        self._updating = False
        self._refresh_ui()
        if self._on_change is not None:
            self._on_change(self.config())


class BorderShaderDialog(QDialog):
    def __init__(
        self,
        icon_style: str,
        initial_config: dict[str, object] | BorderShaderConfig | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Border Shader")
        self._icon_style = icon_style
        self._config = border_shader_to_dict(initial_config)

        layout = QHBoxLayout(self)
        left = QVBoxLayout()
        left.addWidget(QLabel("Template Preview", self))
        self.preview_label = QLabel(self)
        self.preview_label.setMinimumSize(256, 256)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setFrameStyle(QFrame.Shape.Panel | QFrame.Shadow.Sunken)
        left.addWidget(self.preview_label, 1)
        layout.addLayout(left, 1)

        self.controls = BorderShaderControls(
            initial_config=self._config,
            on_change=self._on_config_changed,
            parent=self,
        )
        layout.addWidget(self.controls, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        left.addWidget(buttons)
        self._refresh_preview()

    def _on_config_changed(self, config: dict[str, object]) -> None:
        self._config = dict(config)
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        try:
            payload = build_template_overlay_preview(
                self._icon_style,
                size=256,
                border_shader=self._config,
            )
            pix = QPixmap()
            if pix.loadFromData(payload):
                self.preview_label.setPixmap(
                    composite_on_checkerboard(
                        pix,
                        width=max(256, self.preview_label.width()),
                        height=max(256, self.preview_label.height()),
                        keep_aspect=True,
                    )
                )
                return
        except Exception:
            pass
        self.preview_label.setPixmap(QPixmap())

    def set_icon_style(self, icon_style: str) -> None:
        self._icon_style = icon_style
        self._refresh_preview()

    def result_config(self) -> dict[str, object]:
        return dict(self.controls.config())


class IconFrameCanvas(QWidget):
    roiChanged = Signal(object)
    seedColorPicked = Signal(object)
    manualTextMarkPoint = Signal(object)

    def __init__(
        self, image_bytes: bytes, border_style: str = "none", parent: QWidget | None = None
    ):
        super().__init__(parent)
        self.setMinimumSize(420, 420)
        self._source_image_bytes = _normalize_image_bytes_for_canvas(image_bytes)
        self._pixmap = QPixmap()
        if not self._pixmap.loadFromData(self._source_image_bytes):
            raise ValueError("Unsupported image format.")
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._dragging = False
        self._last_pos = QPointF(0.0, 0.0)
        self._roi_draw_mode = False
        self._roi_dragging = False
        self._roi_drag_start_image = QPointF()
        self._roi_drag_current_image = QPointF()
        self._text_roi: tuple[float, float, float, float] | None = None
        self._seed_pick_mode = False
        self._text_mark_mode = "none"
        self._manual_add_points: list[tuple[float, float]] = []
        self._manual_remove_points: list[tuple[float, float]] = []
        self._template_pixmaps: dict[str, QPixmap] = {}
        self._tinted_template_pixmaps: dict[tuple[str, str], QPixmap] = {}
        self._template_interior_path_cache: dict[tuple[str, int, int], QPainterPath | None] = {}
        self._border_style = normalize_icon_style(border_style, circular_ring=False)
        self._border_shader = border_shader_to_dict(None)
        self._upscale_method = "lanczos_unsharp"
        self._bg_removal_engine = "none"
        self._bg_removal_params = normalize_background_removal_params(None)
        self._text_preserve_config = text_preserve_to_dict(None)
        self._cutout_pixmap_cache: dict[str, QPixmap | None] = {}
        self._text_overlay_pixmap_cache: dict[str, QPixmap | None] = {}
        self._text_alpha_mask_pixmap_cache: dict[str, QPixmap | None] = {}
        self._debug_text_alpha_only = False
        self._cutout_error: str | None = None
        self._async_processing_busy = False
        self._layer_visibility: dict[str, bool] = {
            "base": True,
            "cutout": True,
            "text": True,
            "template": True,
        }

    def reset_view(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(0.2, min(8.0, zoom))
        self._clamp_pan()
        self.update()

    def zoom(self) -> float:
        return self._zoom

    def set_roi_draw_mode(self, enabled: bool) -> None:
        self._roi_draw_mode = bool(enabled)
        if not self._roi_draw_mode and self._roi_dragging:
            self._roi_dragging = False
        self.update()

    def set_seed_pick_mode(self, enabled: bool) -> None:
        self._seed_pick_mode = bool(enabled)
        if self._seed_pick_mode or self._text_mark_mode != "none":
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()

    def seed_pick_mode(self) -> bool:
        return self._seed_pick_mode

    def set_text_mark_mode(self, mode: str | None) -> None:
        normalized = str(mode or "none").strip().casefold()
        if normalized not in {"none", "add", "remove"}:
            normalized = "none"
        self._text_mark_mode = normalized
        if self._seed_pick_mode or self._text_mark_mode != "none":
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.unsetCursor()
        self.update()

    def text_mark_mode(self) -> str:
        return self._text_mark_mode

    def set_manual_text_points(
        self,
        add_points: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None,
        remove_points: list[tuple[float, float]] | tuple[tuple[float, float], ...] | None,
    ) -> None:
        normalized_add: list[tuple[float, float]] = []
        normalized_remove: list[tuple[float, float]] = []
        for raw_points, out in ((add_points, normalized_add), (remove_points, normalized_remove)):
            if not raw_points:
                continue
            for point in raw_points:
                try:
                    x_val = float(point[0])
                    y_val = float(point[1])
                except Exception:
                    continue
                item = (max(0.0, min(1.0, x_val)), max(0.0, min(1.0, y_val)))
                if item not in out:
                    out.append(item)
        self._manual_add_points = normalized_add
        self._manual_remove_points = normalized_remove
        self.update()

    def set_text_roi(self, roi: tuple[float, float, float, float] | list[float] | None) -> None:
        normalized: tuple[float, float, float, float] | None
        if roi is None:
            normalized = None
        else:
            try:
                x = float(roi[0])
                y = float(roi[1])
                w = float(roi[2])
                h = float(roi[3])
            except (TypeError, ValueError, IndexError):
                normalized = None
            else:
                x = max(0.0, min(1.0, x))
                y = max(0.0, min(1.0, y))
                w = max(0.0, min(1.0 - x, w))
                h = max(0.0, min(1.0 - y, h))
                normalized = (x, y, w, h) if w > 0.0 and h > 0.0 else None
        if normalized == self._text_roi:
            return
        self._text_roi = normalized
        self.update()

    def text_roi(self) -> tuple[float, float, float, float] | None:
        return self._text_roi

    def set_border_style(self, border_style: str) -> None:
        style = normalize_icon_style(border_style, circular_ring=False)
        self._border_style = style
        self._tinted_template_pixmaps.clear()
        self._template_interior_path_cache.clear()
        self.update()

    def border_style(self) -> str:
        return self._border_style

    def set_border_shader(self, border_shader: dict[str, object] | None) -> None:
        self._border_shader = border_shader_to_dict(border_shader)
        self._tinted_template_pixmaps.clear()
        self.update()

    def border_shader(self) -> dict[str, object]:
        return dict(self._border_shader)

    def set_upscale_method(self, method: str | None) -> None:
        normalized = _normalize_upscale_method(method)
        if normalized == self._upscale_method:
            return
        self._upscale_method = normalized
        self.update()

    def upscale_method(self) -> str:
        return self._upscale_method

    def source_image_bytes(self) -> bytes:
        return self._source_image_bytes

    def set_bg_removal_engine(self, engine: str) -> None:
        normalized = normalize_background_removal_engine(engine)
        if normalized == self._bg_removal_engine:
            return
        self._bg_removal_engine = normalized
        self._cutout_error = None
        self._cutout_pixmap_cache.clear()
        self._text_overlay_pixmap_cache.clear()
        self._text_alpha_mask_pixmap_cache.clear()
        self.update()

    def bg_removal_engine(self) -> str:
        return self._bg_removal_engine

    def set_bg_removal_params(self, params: dict[str, object] | None) -> None:
        normalized = normalize_background_removal_params(params)
        if normalized == self._bg_removal_params:
            return
        self._bg_removal_params = normalized
        self._cutout_error = None
        self._cutout_pixmap_cache.clear()
        self._text_overlay_pixmap_cache.clear()
        self._text_alpha_mask_pixmap_cache.clear()
        self.update()

    def bg_removal_params(self) -> dict[str, object]:
        return dict(self._bg_removal_params)

    def set_text_preserve_config(self, config: dict[str, object] | TextPreserveConfig | None) -> None:
        normalized = text_preserve_to_dict(normalize_text_preserve_config(config))
        if normalized == self._text_preserve_config:
            return
        self._text_preserve_config = normalized
        self._cutout_error = None
        self._cutout_pixmap_cache.clear()
        self._text_overlay_pixmap_cache.clear()
        self._text_alpha_mask_pixmap_cache.clear()
        self.update()

    def text_preserve_config(self) -> dict[str, object]:
        return dict(self._text_preserve_config)

    def set_layer_visibility(self, layer: str, visible: bool) -> None:
        if layer not in self._layer_visibility:
            return
        new_value = bool(visible)
        if self._layer_visibility[layer] == new_value:
            return
        self._layer_visibility[layer] = new_value
        self._cutout_error = None
        self.update()

    def set_debug_text_alpha_only(self, enabled: bool) -> None:
        new_value = bool(enabled)
        if new_value == self._debug_text_alpha_only:
            return
        self._debug_text_alpha_only = new_value
        self.update()

    def layer_visibility(self, layer: str) -> bool:
        return bool(self._layer_visibility.get(layer, False))

    def cutout_error(self) -> str | None:
        return self._cutout_error

    def set_async_processing_busy(self, busy: bool) -> None:
        self._async_processing_busy = bool(busy)
        if busy:
            self._cutout_error = None
        self.update()

    @staticmethod
    def build_cutout_cache_key(engine: str, params: dict[str, object] | None) -> str:
        return json.dumps(
            {
                "engine": normalize_background_removal_engine(engine),
                "params": normalize_background_removal_params(params),
            },
            sort_keys=True,
        )

    @staticmethod
    def build_text_overlay_cache_key(
        engine: str,
        params: dict[str, object] | None,
        text_config: dict[str, object] | TextPreserveConfig | None,
    ) -> str:
        return json.dumps(
            {
                "engine": normalize_background_removal_engine(engine),
                "params": normalize_background_removal_params(params),
                "text_preserve": text_preserve_to_dict(
                    normalize_text_preserve_config(text_config)
                ),
            },
            sort_keys=True,
        )

    @staticmethod
    def build_text_alpha_cache_key(
        engine: str,
        params: dict[str, object] | None,
        text_config: dict[str, object] | TextPreserveConfig | None,
    ) -> str:
        return json.dumps(
            {
                "engine": normalize_background_removal_engine(engine),
                "params": normalize_background_removal_params(params),
                "text_preserve": text_preserve_to_dict(
                    normalize_text_preserve_config(text_config)
                ),
                "debug": "alpha_mask",
            },
            sort_keys=True,
        )

    @staticmethod
    def _pixmap_from_payload(payload: bytes) -> QPixmap | None:
        pix = QPixmap()
        if not payload or not pix.loadFromData(payload):
            return None
        return pix

    def store_cutout_payload(
        self,
        cache_key: str,
        payload: bytes | None,
        *,
        error: str | None = None,
    ) -> None:
        if error:
            self._cutout_error = error
            self._cutout_pixmap_cache[cache_key] = None
            return
        if payload is None:
            self._cutout_pixmap_cache[cache_key] = None
            return
        pix = self._pixmap_from_payload(payload)
        self._cutout_pixmap_cache[cache_key] = pix
        if pix is None:
            self._cutout_error = "Cutout decode failed."

    def store_text_overlay_payload(self, cache_key: str, payload: bytes | None) -> None:
        if payload is None:
            self._text_overlay_pixmap_cache[cache_key] = None
            return
        self._text_overlay_pixmap_cache[cache_key] = self._pixmap_from_payload(payload)

    def store_text_alpha_payload(self, cache_key: str, payload: bytes | None) -> None:
        if payload is None:
            self._text_alpha_mask_pixmap_cache[cache_key] = None
            return
        self._text_alpha_mask_pixmap_cache[cache_key] = self._pixmap_from_payload(payload)

    def _ensure_cutout_pixmap(self) -> QPixmap | None:
        engine = self._bg_removal_engine
        if engine == "none":
            return None
        cache_key = json.dumps(
            {
                "engine": engine,
                "params": self._bg_removal_params,
            },
            sort_keys=True,
        )
        if cache_key in self._cutout_pixmap_cache:
            return self._cutout_pixmap_cache[cache_key]
        if self._async_processing_busy:
            return None
        try:
            has_transparency = True
            if Image is not None:
                try:
                    with Image.open(BytesIO(self._source_image_bytes)) as src_img:
                        src_img.load()
                        src_img = ImageOps.exif_transpose(src_img).convert("RGBA")
                        raw_cutout = remove_background_bytes(
                            self._source_image_bytes,
                            engine=engine,
                            params=self._bg_removal_params,
                        )
                        raw_cutout = _normalize_image_bytes_for_canvas(raw_cutout)
                        with Image.open(BytesIO(raw_cutout)) as loaded_cut_img:
                            loaded_cut_img.load()
                            cut_img = ImageOps.exif_transpose(loaded_cut_img).convert("RGBA")
                        extrema = cut_img.getchannel("A").getextrema()
                        has_transparency = bool(extrema and extrema[0] < 255)
                        out = BytesIO()
                        cut_img.save(out, format="PNG")
                        cutout_bytes = out.getvalue()
                except Exception:
                    has_transparency = True
                    cutout_bytes = remove_background_bytes(
                        self._source_image_bytes,
                        engine=engine,
                        params=self._bg_removal_params,
                    )
                    cutout_bytes = _normalize_image_bytes_for_canvas(cutout_bytes)
            else:
                cutout_bytes = remove_background_bytes(
                    self._source_image_bytes,
                    engine=engine,
                    params=self._bg_removal_params,
                )
                cutout_bytes = _normalize_image_bytes_for_canvas(cutout_bytes)
            pix = QPixmap()
            if not pix.loadFromData(cutout_bytes):
                self._cutout_error = "Cutout decode failed."
                self._cutout_pixmap_cache[cache_key] = None
                return None
            if not has_transparency:
                self._cutout_error = "Cutout output has no transparency."
                self._cutout_pixmap_cache[cache_key] = None
                return None
            self._cutout_pixmap_cache[cache_key] = pix
            return pix
        except Exception as exc:
            self._cutout_error = str(exc)
            self._cutout_pixmap_cache[cache_key] = None
            return None

    def _ensure_text_overlay_pixmap(self) -> QPixmap | None:
        text_cfg = text_preserve_to_dict(self._text_preserve_config)
        if not bool(text_cfg.get("enabled", False)):
            return None
        cache_key = json.dumps(
            {
                "engine": self._bg_removal_engine,
                "params": self._bg_removal_params,
                "text_preserve": text_cfg,
            },
            sort_keys=True,
        )
        if cache_key in self._text_overlay_pixmap_cache:
            return self._text_overlay_pixmap_cache[cache_key]
        if self._async_processing_busy:
            return None
        try:
            if Image is None:
                return None
            with Image.open(BytesIO(self._source_image_bytes)) as src_img:
                src_img.load()
                src_img = ImageOps.exif_transpose(src_img).convert("RGBA")
                if self._bg_removal_engine == "none":
                    cut_img = Image.new("RGBA", src_img.size, (0, 0, 0, 0))
                else:
                    raw_cutout = remove_background_bytes(
                        self._source_image_bytes,
                        engine=self._bg_removal_engine,
                        params=self._bg_removal_params,
                    )
                    raw_cutout = _normalize_image_bytes_for_canvas(raw_cutout)
                    with Image.open(BytesIO(raw_cutout)) as loaded_cut_img:
                        loaded_cut_img.load()
                        cut_img = ImageOps.exif_transpose(loaded_cut_img).convert("RGBA")
                text_overlay = build_text_extraction_overlay(
                    src_img,
                    cut_img,
                    text_cfg,
                )
                if text_overlay is None:
                    self._text_overlay_pixmap_cache[cache_key] = None
                    return None
                out = BytesIO()
                text_overlay.save(out, format="PNG")
                overlay_bytes = out.getvalue()
            pix = QPixmap()
            if not pix.loadFromData(overlay_bytes):
                self._text_overlay_pixmap_cache[cache_key] = None
                return None
            self._text_overlay_pixmap_cache[cache_key] = pix
            return pix
        except Exception as exc:
            self._cutout_error = str(exc)
            self._text_overlay_pixmap_cache[cache_key] = None
            return None

    def _ensure_text_alpha_mask_pixmap(self) -> QPixmap | None:
        text_cfg = text_preserve_to_dict(self._text_preserve_config)
        if not bool(text_cfg.get("enabled", False)):
            return None
        cache_key = json.dumps(
            {
                "engine": self._bg_removal_engine,
                "params": self._bg_removal_params,
                "text_preserve": text_cfg,
                "debug": "alpha_mask",
            },
            sort_keys=True,
        )
        if cache_key in self._text_alpha_mask_pixmap_cache:
            return self._text_alpha_mask_pixmap_cache[cache_key]
        if self._async_processing_busy:
            return None
        try:
            if Image is None:
                return None
            with Image.open(BytesIO(self._source_image_bytes)) as src_img:
                src_img.load()
                src_img = ImageOps.exif_transpose(src_img).convert("RGBA")
                if self._bg_removal_engine == "none":
                    cut_img = Image.new("RGBA", src_img.size, (0, 0, 0, 0))
                else:
                    raw_cutout = remove_background_bytes(
                        self._source_image_bytes,
                        engine=self._bg_removal_engine,
                        params=self._bg_removal_params,
                    )
                    raw_cutout = _normalize_image_bytes_for_canvas(raw_cutout)
                    with Image.open(BytesIO(raw_cutout)) as loaded_cut_img:
                        loaded_cut_img.load()
                        cut_img = ImageOps.exif_transpose(loaded_cut_img).convert("RGBA")
                alpha_mask = build_text_extraction_alpha_mask(
                    src_img,
                    cut_img,
                    text_cfg,
                )
                if alpha_mask is None:
                    self._text_alpha_mask_pixmap_cache[cache_key] = None
                    return None
                mask_rgba = Image.new("RGBA", alpha_mask.size, (255, 255, 255, 0))
                mask_rgba.putalpha(alpha_mask)
                out = BytesIO()
                mask_rgba.save(out, format="PNG")
                overlay_bytes = out.getvalue()
            pix = QPixmap()
            if not pix.loadFromData(overlay_bytes):
                self._text_alpha_mask_pixmap_cache[cache_key] = None
                return None
            self._text_alpha_mask_pixmap_cache[cache_key] = pix
            return pix
        except Exception as exc:
            self._cutout_error = str(exc)
            self._text_alpha_mask_pixmap_cache[cache_key] = None
            return None

    def _display_pixmap(self) -> QPixmap:
        base = self._layer_visibility.get("base", True)
        cutout_visible = self._layer_visibility.get("cutout", True)
        text_visible = self._layer_visibility.get("text", True)
        template_visible = self._layer_visibility.get("template", True)
        if base and cutout_visible and text_visible and template_visible:
            return self._pixmap
        if text_visible and not base and not cutout_visible and not template_visible:
            text = (
                self._ensure_text_alpha_mask_pixmap()
                if self._debug_text_alpha_only
                else self._ensure_text_overlay_pixmap()
            )
            if text is not None and not text.isNull():
                return text
        if cutout_visible and not base and not text_visible and not template_visible:
            cutout = self._ensure_cutout_pixmap()
            if cutout is not None and not cutout.isNull():
                return cutout
        return self._pixmap

    def _cutout_overlay_pixmap(self) -> QPixmap | None:
        if not self._layer_visibility.get("cutout", True):
            return None
        cutout = self._ensure_cutout_pixmap()
        if cutout is None or cutout.isNull():
            return None
        return cutout

    def _text_overlay_pixmap(self) -> QPixmap | None:
        if not self._layer_visibility.get("text", True):
            return None
        text_overlay = self._ensure_text_overlay_pixmap()
        if text_overlay is None or text_overlay.isNull():
            return None
        return text_overlay

    def _composite_template_enabled(self) -> bool:
        return self._border_style != "none"

    def _composite_cutout_enabled(self) -> bool:
        return self._composite_template_enabled() and self._bg_removal_engine != "none"

    def _composite_text_enabled(self) -> bool:
        cfg = normalize_text_preserve_config(self._text_preserve_config)
        has_manual = bool(cfg.manual_add_seeds) or bool(cfg.manual_remove_seeds)
        return self._composite_template_enabled() and bool(cfg.enabled) and (
            cfg.method != "none" or has_manual
        )

    def _template_rect(self) -> QRectF:
        side = max(120.0, min(float(self.width()), float(self.height())) - 36.0)
        x = (self.width() - side) / 2.0
        y = (self.height() - side) / 2.0
        return QRectF(x, y, side, side)

    def _base_scale(self, template: QRectF) -> float:
        pix = self._display_pixmap()
        return max(
            template.width() / max(1, pix.width()),
            template.height() / max(1, pix.height()),
        )

    def _image_rect(self, template: QRectF) -> QRectF:
        pix = self._display_pixmap()
        scale = self._base_scale(template) * self._zoom
        width = pix.width() * scale
        height = pix.height() * scale
        center = template.center() + self._pan
        return QRectF(
            center.x() - (width / 2.0),
            center.y() - (height / 2.0),
            width,
            height,
        )

    def _clamp_pan(self) -> None:
        # Free pan: allow selecting any crop region, including empty/black areas.
        return

    def _canvas_point_to_source_image_point(self, point: QPointF) -> QPointF:
        template = self._template_rect()
        image_rect = self._image_rect(template)
        width = max(1.0, image_rect.width())
        height = max(1.0, image_rect.height())
        px = ((point.x() - image_rect.x()) / width) * self._pixmap.width()
        py = ((point.y() - image_rect.y()) / height) * self._pixmap.height()
        px = max(0.0, min(float(self._pixmap.width()), px))
        py = max(0.0, min(float(self._pixmap.height()), py))
        return QPointF(px, py)

    def _source_image_point_to_canvas(self, point: QPointF) -> QPointF:
        template = self._template_rect()
        image_rect = self._image_rect(template)
        px = image_rect.x() + (point.x() / max(1, self._pixmap.width())) * image_rect.width()
        py = image_rect.y() + (point.y() / max(1, self._pixmap.height())) * image_rect.height()
        return QPointF(px, py)

    def _current_or_drag_roi_canvas_rect(self) -> QRectF | None:
        roi = self._text_roi
        if self._roi_dragging:
            left = min(self._roi_drag_start_image.x(), self._roi_drag_current_image.x())
            top = min(self._roi_drag_start_image.y(), self._roi_drag_current_image.y())
            right = max(self._roi_drag_start_image.x(), self._roi_drag_current_image.x())
            bottom = max(self._roi_drag_start_image.y(), self._roi_drag_current_image.y())
            if right - left > 1.0 and bottom - top > 1.0:
                width = float(max(1, self._pixmap.width()))
                height = float(max(1, self._pixmap.height()))
                roi = (
                    max(0.0, min(1.0, left / width)),
                    max(0.0, min(1.0, top / height)),
                    max(0.0, min(1.0, (right - left) / width)),
                    max(0.0, min(1.0, (bottom - top) / height)),
                )
        if roi is None:
            return None
        x, y, w, h = roi
        p1 = self._source_image_point_to_canvas(
            QPointF(x * self._pixmap.width(), y * self._pixmap.height())
        )
        p2 = self._source_image_point_to_canvas(
            QPointF((x + w) * self._pixmap.width(), (y + h) * self._pixmap.height())
        )
        return QRectF(p1, p2).normalized()

    def wheelEvent(self, event: QWheelEvent) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return
        base_factor = 1.04
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            base_factor = 1.02
        elif modifiers & Qt.KeyboardModifier.ShiftModifier:
            base_factor = 1.08
        factor = base_factor ** (delta / 120.0)
        self.set_zoom(self._zoom * factor)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._seed_pick_mode and event.button() == Qt.MouseButton.LeftButton:
            source_point = self._canvas_point_to_source_image_point(event.position())
            image = self._pixmap.toImage()
            if not image.isNull():
                px = max(0, min(image.width() - 1, int(round(source_point.x()))))
                py = max(0, min(image.height() - 1, int(round(source_point.y()))))
                color = image.pixelColor(px, py)
                self.seedColorPicked.emit((color.red(), color.green(), color.blue()))
            self.set_seed_pick_mode(False)
            event.accept()
            return
        if self._text_mark_mode != "none" and event.button() == Qt.MouseButton.LeftButton:
            source_point = self._canvas_point_to_source_image_point(event.position())
            nx = max(0.0, min(1.0, source_point.x() / max(1.0, float(self._pixmap.width()))))
            ny = max(0.0, min(1.0, source_point.y() / max(1.0, float(self._pixmap.height()))))
            self.manualTextMarkPoint.emit((nx, ny))
            event.accept()
            return
        if self._roi_draw_mode and event.button() == Qt.MouseButton.RightButton:
            self._roi_dragging = True
            self._roi_drag_start_image = self._canvas_point_to_source_image_point(event.position())
            self._roi_drag_current_image = QPointF(self._roi_drag_start_image)
            self.update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._last_pos = event.position()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._roi_dragging:
            self._roi_drag_current_image = self._canvas_point_to_source_image_point(event.position())
            self.update()
            event.accept()
            return
        if self._dragging:
            delta = event.position() - self._last_pos
            self._last_pos = event.position()
            self._pan += delta
            self._clamp_pan()
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._roi_dragging and event.button() == Qt.MouseButton.RightButton:
            self._roi_drag_current_image = self._canvas_point_to_source_image_point(event.position())
            self._roi_dragging = False
            left = min(self._roi_drag_start_image.x(), self._roi_drag_current_image.x())
            top = min(self._roi_drag_start_image.y(), self._roi_drag_current_image.y())
            right = max(self._roi_drag_start_image.x(), self._roi_drag_current_image.x())
            bottom = max(self._roi_drag_start_image.y(), self._roi_drag_current_image.y())
            if right - left > 1.0 and bottom - top > 1.0:
                width = float(max(1, self._pixmap.width()))
                height = float(max(1, self._pixmap.height()))
                roi = (
                    max(0.0, min(1.0, left / width)),
                    max(0.0, min(1.0, top / height)),
                    max(0.0, min(1.0, (right - left) / width)),
                    max(0.0, min(1.0, (bottom - top) / height)),
                )
                self._text_roi = roi
                self.roiChanged.emit(roi)
            self.update()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _draw_metallic_template_ring(self, painter: QPainter, circle_rect: QRectF) -> float:
        ring_width = max(2.0, circle_rect.width() * 0.035)
        ring_width_int = max(1, int(round(ring_width)))
        for idx in range(ring_width_int):
            t = idx / max(1, ring_width_int - 1)
            brightness = int(145 + (1.0 - abs((2.0 * t) - 1.0)) * 90)
            color = QColor(brightness, brightness, min(255, brightness + 8), 220)
            ring_rect = circle_rect.adjusted(idx * 0.5, idx * 0.5, -idx * 0.5, -idx * 0.5)
            painter.setPen(QPen(color, 1))
            painter.drawEllipse(ring_rect)

        highlight_pen = QPen(QColor(255, 255, 255, 190), max(1, ring_width_int // 3))
        painter.setPen(highlight_pen)
        painter.drawArc(circle_rect, 35 * 16, 110 * 16)

        shadow_pen = QPen(QColor(52, 52, 58, 170), max(1, ring_width_int // 3))
        painter.setPen(shadow_pen)
        painter.drawArc(circle_rect, 210 * 16, 110 * 16)
        return ring_width

    def _draw_template_overlay(self, painter: QPainter, template: QRectF) -> None:
        if self._border_style == "none":
            frame_pen = QPen(QColor(220, 220, 220, 120), 1, Qt.PenStyle.DashLine)
            painter.setPen(frame_pen)
            painter.drawRect(template)
            return
        template_spec = resolve_icon_template(self._border_style, circular_ring=False)
        template_pix = None
        if template_spec.path is not None:
            key = str(template_spec.path)
            template_pix = self._template_pixmaps.get(key)
            if template_pix is None:
                pix = QPixmap(key)
                self._template_pixmaps[key] = pix
                template_pix = pix
        if template_pix is not None and not template_pix.isNull():
            rendered = self._apply_border_shader_to_pixmap(template_pix, template_spec)
            painter.drawPixmap(template, rendered, QRectF(rendered.rect()))
            return

        if template_spec.shape == "square":
            frame_pen = QPen(QColor(220, 220, 220, 150), 2)
            painter.setPen(frame_pen)
            painter.drawRoundedRect(template.adjusted(8, 8, -8, -8), 28, 28)
            return
        circle = template.adjusted(8, 8, -8, -8)
        circle_path = QPainterPath()
        circle_path.addEllipse(circle)
        square = QPainterPath()
        square.addRect(template)
        painter.fillPath(square - circle_path, QColor(0, 0, 0, 100))
        frame_pen = QPen(QColor(220, 220, 220, 150), 1, Qt.PenStyle.DashLine)
        painter.setPen(frame_pen)
        painter.drawRect(template)
        self._draw_metallic_template_ring(painter, circle)

    def _draw_template_overlay_for_export(self, painter: QPainter, template: QRectF) -> None:
        if self._border_style == "none":
            return
        template_spec = resolve_icon_template(self._border_style, circular_ring=False)
        template_pix = None
        if template_spec.path is not None:
            key = str(template_spec.path)
            template_pix = self._template_pixmaps.get(key)
            if template_pix is None:
                pix = QPixmap(key)
                self._template_pixmaps[key] = pix
                template_pix = pix
        if template_pix is not None and not template_pix.isNull():
            rendered = self._apply_border_shader_to_pixmap(template_pix, template_spec)
            painter.drawPixmap(template, rendered, QRectF(rendered.rect()))
            return
        if template_spec.shape == "square":
            frame_pen = QPen(QColor(220, 220, 220, 150), 2)
            painter.setPen(frame_pen)
            painter.drawRoundedRect(template.adjusted(8, 8, -8, -8), 28, 28)
            return
        circle = template.adjusted(8, 8, -8, -8)
        self._draw_metallic_template_ring(painter, circle)

    def _template_interior_clip_path(self, template: QRectF) -> QPainterPath | None:
        if self._border_style == "none":
            return None
        width = max(1, int(round(template.width())))
        height = max(1, int(round(template.height())))
        cache_key = (self._border_style, width, height)
        if cache_key in self._template_interior_path_cache:
            return self._template_interior_path_cache[cache_key]
        mask_bytes = build_template_interior_mask_png(
            self._border_style,
            size=max(width, height),
            circular_ring=False,
        )
        if not mask_bytes:
            self._template_interior_path_cache[cache_key] = None
            return None
        mask_image = QImage()
        if not mask_image.loadFromData(mask_bytes):
            self._template_interior_path_cache[cache_key] = None
            return None
        mask_image = mask_image.convertToFormat(QImage.Format.Format_Grayscale8)
        if mask_image.width() <= 0 or mask_image.height() <= 0:
            self._template_interior_path_cache[cache_key] = None
            return None
        if mask_image.width() != width or mask_image.height() != height:
            mask_image = mask_image.scaled(
                width,
                height,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )

        bits = mask_image.constBits()
        if bits is None:
            self._template_interior_path_cache[cache_key] = None
            return None
        size_bytes = int(mask_image.sizeInBytes())
        try:
            if hasattr(bits, "asstring"):
                raw = bits.asstring(size_bytes)
            elif hasattr(bits, "setsize"):
                bits.setsize(size_bytes)
                raw = bytes(bits)
            else:
                raw = memoryview(bits).tobytes()
        except Exception:
            try:
                raw = bytes(bits[:size_bytes])
            except Exception:
                self._template_interior_path_cache[cache_key] = None
                return None
        stride = mask_image.bytesPerLine()
        min_size = int(stride) * int(mask_image.height())
        if len(raw) < min_size:
            self._template_interior_path_cache[cache_key] = None
            return None
        path = QPainterPath()
        x_off = float(template.x())
        y_off = float(template.y())
        w_img = mask_image.width()
        h_img = mask_image.height()
        for y in range(h_img):
            row = raw[y * stride : y * stride + w_img]
            run_start = -1
            for x in range(w_img):
                inside = row[x] >= 16
                if inside and run_start < 0:
                    run_start = x
                elif not inside and run_start >= 0:
                    path.addRect(
                        QRectF(
                            x_off + float(run_start),
                            y_off + float(y),
                            float(x - run_start),
                            1.0,
                        )
                    )
                    run_start = -1
            if run_start >= 0:
                path.addRect(
                    QRectF(
                        x_off + float(run_start),
                        y_off + float(y),
                        float(w_img - run_start),
                        1.0,
                    )
                )
        if path.isEmpty():
            self._template_interior_path_cache[cache_key] = None
            return None
        self._template_interior_path_cache[cache_key] = path
        return path

    def _dim_non_interior_template_area_for_preview(
        self, painter: QPainter, template: QRectF
    ) -> None:
        interior = self._template_interior_clip_path(template)
        if interior is None:
            return
        template_path = QPainterPath()
        template_path.addRect(template)
        # Preview-only dim: area inside template bounds but outside the real
        # interior shape should be masked significantly.
        painter.fillPath(template_path - interior, QColor(0, 0, 0, 165))

    def _apply_border_shader_to_pixmap(
        self, pixmap: QPixmap, template_spec
    ) -> QPixmap:
        if pixmap.isNull():
            return pixmap
        if template_spec.template_id == "none":
            return pixmap
        shader = normalize_border_shader_config(self._border_shader)
        if not shader.enabled or shader.intensity <= 0:
            return pixmap
        key = (
            f"{template_spec.template_id}:{pixmap.cacheKey()}",
            json.dumps(border_shader_to_dict(shader), sort_keys=True),
        )
        cached = self._tinted_template_pixmaps.get(key)
        if cached is not None:
            return cached
        tinted = QPixmap(pixmap.size())
        tinted.fill(Qt.GlobalColor.transparent)
        tint = QColor()
        if shader.mode == "hsl":
            tint.setHsl(
                shader.hue,
                int(round(shader.saturation * 255 / 100)),
                int(round(shader.tone * 255 / 100)),
            )
        else:
            tint.setHsv(
                shader.hue,
                int(round(shader.saturation * 255 / 100)),
                int(round(shader.tone * 255 / 100)),
            )
        alpha = shader.intensity / 100.0
        qp = QPainter(tinted)
        qp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        qp.drawPixmap(0, 0, pixmap)
        qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        qp.fillRect(tinted.rect(), tint)
        qp.end()

        blended = QPixmap(pixmap.size())
        blended.fill(Qt.GlobalColor.transparent)
        bp = QPainter(blended)
        bp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        bp.drawPixmap(0, 0, pixmap)
        bp.setOpacity(alpha)
        bp.drawPixmap(0, 0, tinted)
        bp.end()
        self._tinted_template_pixmaps[key] = blended
        return blended

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor(20, 20, 20))

        template = self._template_rect()
        self._clamp_pan()
        image_rect = self._image_rect(template)
        draw_checkerboard(painter, QRectF(self.rect()), tile_size=12)

        show_background = self._layer_visibility.get("base", True)
        show_template = self._layer_visibility.get("template", True)
        show_cutout = self._layer_visibility.get("cutout", True)
        show_text = self._layer_visibility.get("text", True)

        if show_background:
            if self._border_style != "none":
                painter.fillRect(template, QColor(0, 0, 0))
            painter.drawPixmap(image_rect, self._pixmap, QRectF(self._pixmap.rect()))

        if show_template:
            outside = QPainterPath()
            outside.addRect(QRectF(self.rect()))
            template_path = QPainterPath()
            template_path.addRect(template)
            painter.fillPath(outside - template_path, QColor(0, 0, 0, 120))
            self._dim_non_interior_template_area_for_preview(painter, template)
            self._draw_template_overlay(painter, template)

        cutout_overlay = self._cutout_overlay_pixmap()
        if show_cutout and cutout_overlay is not None:
            painter.drawPixmap(image_rect, cutout_overlay, QRectF(cutout_overlay.rect()))
        text_overlay = (
            self._ensure_text_alpha_mask_pixmap()
            if self._debug_text_alpha_only
            else self._text_overlay_pixmap()
        )
        if show_text and text_overlay is not None:
            painter.drawPixmap(image_rect, text_overlay, QRectF(text_overlay.rect()))

        roi_rect = self._current_or_drag_roi_canvas_rect()
        if roi_rect is not None:
            painter.setPen(QPen(QColor(255, 210, 80, 220), 2, Qt.PenStyle.DashLine))
            painter.fillRect(roi_rect, QColor(255, 210, 80, 35))
            painter.drawRect(roi_rect)
        if self._manual_add_points:
            painter.setPen(QPen(QColor(74, 220, 112, 220), 1))
            painter.setBrush(QColor(74, 220, 112, 160))
            for x_norm, y_norm in self._manual_add_points:
                point = self._source_image_point_to_canvas(
                    QPointF(x_norm * self._pixmap.width(), y_norm * self._pixmap.height())
                )
                painter.drawEllipse(point, 4.0, 4.0)
        if self._manual_remove_points:
            painter.setPen(QPen(QColor(225, 96, 88, 220), 1))
            painter.setBrush(QColor(225, 96, 88, 160))
            for x_norm, y_norm in self._manual_remove_points:
                point = self._source_image_point_to_canvas(
                    QPointF(x_norm * self._pixmap.width(), y_norm * self._pixmap.height())
                )
                painter.drawEllipse(point, 4.0, 4.0)

    def _pillow_resample_mode(self) -> int | None:
        method = _normalize_upscale_method(self._upscale_method)
        if method == "bicubic":
            return Image.Resampling.BICUBIC if Image is not None else None  # type: ignore[return-value]
        if method in {"lanczos", "lanczos_unsharp"}:
            return Image.Resampling.LANCZOS if Image is not None else None  # type: ignore[return-value]
        return None

    def _effective_source_span_in_template(self, output_size: int) -> tuple[float, float]:
        template = self._template_rect()
        image_rect = self._image_rect(template)
        scale = output_size / max(1.0, template.width())
        target = QRectF(
            (image_rect.x() - template.x()) * scale,
            (image_rect.y() - template.y()) * scale,
            image_rect.width() * scale,
            image_rect.height() * scale,
        )
        src_w = float(max(1, self._pixmap.width()))
        src_h = float(max(1, self._pixmap.height()))
        if target.width() <= 0.0 or target.height() <= 0.0:
            return (0.0, 0.0)
        sx0 = (0.0 - target.x()) / target.width() * src_w
        sx1 = (float(output_size) - target.x()) / target.width() * src_w
        sy0 = (0.0 - target.y()) / target.height() * src_h
        sy1 = (float(output_size) - target.y()) / target.height() * src_h
        sx_lo = max(0.0, min(sx0, sx1))
        sx_hi = min(src_w, max(sx0, sx1))
        sy_lo = max(0.0, min(sy0, sy1))
        sy_hi = min(src_h, max(sy0, sy1))
        return (max(0.0, sx_hi - sx_lo), max(0.0, sy_hi - sy_lo))

    def _should_use_resample_pipeline(self, output_size: int) -> bool:
        if Image is None or self._upscale_method == "qt_smooth":
            return False
        if self._pillow_resample_mode() is None:
            return False
        src_w, src_h = self._effective_source_span_in_template(output_size)
        return src_w < float(output_size) or src_h < float(output_size)

    def _render_layer_with_pillow(
        self,
        layer: QPixmap,
        target: QRectF,
        output_size: int,
    ) -> QImage | None:
        if Image is None:
            return None
        resample = self._pillow_resample_mode()
        if resample is None:
            return None
        if layer.isNull() or target.width() <= 0.0 or target.height() <= 0.0:
            return None
        payload = QByteArray()
        buf = QBuffer(payload)
        if not buf.open(QBuffer.OpenModeFlag.WriteOnly):
            return None
        if not layer.save(buf, "PNG"):
            return None
        source_bytes = bytes(payload)
        if not source_bytes:
            return None
        try:
            with Image.open(BytesIO(source_bytes)) as loaded:
                loaded.load()
                src = ImageOps.exif_transpose(loaded).convert("RGBA")
            src_w = float(max(1, src.width))
            src_h = float(max(1, src.height))
            sx0 = (0.0 - target.x()) / target.width() * src_w
            sx1 = (float(output_size) - target.x()) / target.width() * src_w
            sy0 = (0.0 - target.y()) / target.height() * src_h
            sy1 = (float(output_size) - target.y()) / target.height() * src_h
            transformed = src.transform(
                (output_size, output_size),
                Image.Transform.EXTENT,
                (sx0, sy0, sx1, sy1),
                resample=resample,
                fillcolor=(0, 0, 0, 0),
            )
            if self._upscale_method == "lanczos_unsharp" and ImageFilter is not None:
                transformed = transformed.filter(
                    ImageFilter.UnsharpMask(radius=1.2, percent=90, threshold=2)
                )
            out = BytesIO()
            transformed.save(out, format="PNG")
            image = QImage()
            if not image.loadFromData(out.getvalue()):
                return None
            return image
        except Exception:
            return None

    def export_png_bytes(self, output_size: int = 512) -> bytes:
        template = self._template_rect()
        image_rect = self._image_rect(template)
        scale = output_size / max(1.0, template.width())

        target = QRectF(
            (image_rect.x() - template.x()) * scale,
            (image_rect.y() - template.y()) * scale,
            image_rect.width() * scale,
            image_rect.height() * scale,
        )
        out_image = QImage(output_size, output_size, QImage.Format.Format_ARGB32)
        if self._border_style == "none":
            out_image.fill(Qt.GlobalColor.transparent)
        else:
            out_image.fill(Qt.GlobalColor.transparent)

        painter = QPainter(out_image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        output_rect = QRectF(0, 0, output_size, output_size)
        clip_path = self._template_interior_clip_path(output_rect)
        if clip_path is not None:
            painter.save()
            painter.setClipPath(clip_path)
            painter.fillRect(output_rect, QColor(0, 0, 0))
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            painter.restore()
        else:
            painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
        painter.end()

        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        out_image.save(buffer, "PNG")
        return bytes(payload)

    def export_composited_png_bytes(self, output_size: int = 512) -> bytes:
        template = self._template_rect()
        image_rect = self._image_rect(template)
        scale = output_size / max(1.0, template.width())
        target = QRectF(
            (image_rect.x() - template.x()) * scale,
            (image_rect.y() - template.y()) * scale,
            image_rect.width() * scale,
            image_rect.height() * scale,
        )
        out_image = QImage(output_size, output_size, QImage.Format.Format_ARGB32)
        out_image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out_image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        use_resample_pipeline = self._should_use_resample_pipeline(output_size)
        # Final compositing is driven by mode selectors (dropdowns), not by
        # layer visibility checkboxes. Visibility is preview-only.
        show_background = True
        show_template = self._composite_template_enabled()
        show_cutout = self._composite_cutout_enabled()
        show_text = self._composite_text_enabled()
        if show_background:
            output_rect = QRectF(0, 0, output_size, output_size)
            base_resampled = (
                self._render_layer_with_pillow(self._pixmap, target, output_size)
                if use_resample_pipeline
                else None
            )
            if show_template:
                clip_path = self._template_interior_clip_path(output_rect)
                if clip_path is not None:
                    painter.save()
                    painter.setClipPath(clip_path)
                    painter.fillRect(output_rect, QColor(0, 0, 0))
                    if base_resampled is not None:
                        painter.drawImage(output_rect, base_resampled)
                    else:
                        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
                    painter.restore()
                else:
                    painter.fillRect(output_rect, QColor(0, 0, 0))
                    if base_resampled is not None:
                        painter.drawImage(output_rect, base_resampled)
                    else:
                        painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
            else:
                if base_resampled is not None:
                    painter.drawImage(output_rect, base_resampled)
                else:
                    painter.drawPixmap(target, self._pixmap, QRectF(self._pixmap.rect()))
        if show_template and self._border_style != "none":
            self._draw_template_overlay_for_export(
                painter,
                QRectF(0, 0, output_size, output_size),
            )
        cutout_overlay = self._cutout_overlay_pixmap()
        if show_cutout and cutout_overlay is not None:
            cutout_resampled = (
                self._render_layer_with_pillow(cutout_overlay, target, output_size)
                if use_resample_pipeline
                else None
            )
            if cutout_resampled is not None:
                painter.drawImage(QRectF(0, 0, output_size, output_size), cutout_resampled)
            else:
                painter.drawPixmap(target, cutout_overlay, QRectF(cutout_overlay.rect()))
        text_overlay = self._text_overlay_pixmap()
        if show_text and text_overlay is not None:
            text_resampled = (
                self._render_layer_with_pillow(text_overlay, target, output_size)
                if use_resample_pipeline
                else None
            )
            if text_resampled is not None:
                painter.drawImage(QRectF(0, 0, output_size, output_size), text_resampled)
            else:
                painter.drawPixmap(target, text_overlay, QRectF(text_overlay.rect()))
        painter.end()
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        out_image.save(buffer, "PNG")
        return bytes(payload)


class FramingProcessingWorker(QObject):
    progress = Signal(str, int, int)
    completed = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        source_image_bytes: bytes,
        bg_engine: str,
        bg_params: dict[str, object],
        text_config: dict[str, object],
        include_cutout: bool,
        include_text_overlay: bool,
        include_text_alpha: bool,
    ):
        super().__init__()
        self._source_image_bytes = source_image_bytes
        self._bg_engine = normalize_background_removal_engine(bg_engine)
        self._bg_params = normalize_background_removal_params(bg_params)
        self._text_config = text_preserve_to_dict(
            normalize_text_preserve_config(text_config)
        )
        self._include_cutout = bool(include_cutout)
        self._include_text_overlay = bool(include_text_overlay)
        self._include_text_alpha = bool(include_text_alpha)

    @Slot()
    def run(self) -> None:
        total = int(self._include_cutout) + int(self._include_text_overlay or self._include_text_alpha)
        if total <= 0:
            self.completed.emit({})
            self.finished.emit()


        result: dict[str, object] = {}
        step = 0
        cut_img = None
        src_img = None
        try:
            if Image is not None:
                with Image.open(BytesIO(self._source_image_bytes)) as loaded:
                    loaded.load()
                    src_img = ImageOps.exif_transpose(loaded).convert("RGBA")
            if self._include_cutout or self._include_text_overlay or self._include_text_alpha:
                if self._bg_engine == "none":
                    if src_img is not None:
                        cut_img = Image.new("RGBA", src_img.size, (0, 0, 0, 0))
                else:
                    step += 1
                    self.progress.emit(
                        f"Preparing cutout ({self._bg_engine})",
                        step - 1,
                        total,
                    )
                    raw_cutout = remove_background_bytes(
                        self._source_image_bytes,
                        engine=self._bg_engine,
                        params=self._bg_params,
                    )
                    raw_cutout = _normalize_image_bytes_for_canvas(raw_cutout)
                    result["cutout_bytes"] = raw_cutout
                    if Image is not None:
                        with Image.open(BytesIO(raw_cutout)) as loaded_cut:
                            loaded_cut.load()
                            cut_img = ImageOps.exif_transpose(loaded_cut).convert("RGBA")
                            extrema = cut_img.getchannel("A").getextrema()
                            if not (extrema and extrema[0] < 255):
                                result["cutout_error"] = "Cutout output has no transparency."
                    self.progress.emit(
                        f"Preparing cutout ({self._bg_engine})",
                        step,
                        total,
                    )
            if self._include_text_overlay or self._include_text_alpha:
                step += 1
                method = str(self._text_config.get("method", "none") or "none")
                self.progress.emit(f"Extracting text ({method})", step - 1, total)
                if Image is not None and src_img is not None:
                    if cut_img is None:
                        if self._bg_engine == "none":
                            cut_img = Image.new("RGBA", src_img.size, (0, 0, 0, 0))
                        else:
                            raw_cutout = remove_background_bytes(
                                self._source_image_bytes,
                                engine=self._bg_engine,
                                params=self._bg_params,
                            )
                            raw_cutout = _normalize_image_bytes_for_canvas(raw_cutout)
                            with Image.open(BytesIO(raw_cutout)) as loaded_cut:
                                loaded_cut.load()
                                cut_img = ImageOps.exif_transpose(loaded_cut).convert("RGBA")
                    if self._include_text_overlay:
                        overlay = build_text_extraction_overlay(src_img, cut_img, self._text_config)
                        if overlay is not None:
                            out = BytesIO()
                            overlay.save(out, format="PNG")
                            result["text_overlay_bytes"] = out.getvalue()
                    if self._include_text_alpha:
                        alpha_mask = build_text_extraction_alpha_mask(
                            src_img, cut_img, self._text_config
                        )
                        if alpha_mask is not None:
                            mask_rgba = Image.new("RGBA", alpha_mask.size, (255, 255, 255, 0))
                            mask_rgba.putalpha(alpha_mask)
                            out = BytesIO()
                            mask_rgba.save(out, format="PNG")
                            result["text_alpha_bytes"] = out.getvalue()
                self.progress.emit(f"Extracting text ({method})", step, total)
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class SeedColorButton(QPushButton):
    singleClicked = Signal()
    doubleClicked = Signal()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ignore_release_once = False
        self._single_click_timer = QTimer(self)
        self._single_click_timer.setSingleShot(True)
        self._single_click_timer.timeout.connect(self.singleClicked.emit)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._ignore_release_once:
                self._ignore_release_once = False
                event.accept()
                return
            self._single_click_timer.start(max(1, QApplication.doubleClickInterval()))
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            if self._single_click_timer.isActive():
                self._single_click_timer.stop()
            self._ignore_release_once = True
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class IconFramingDialog(QDialog):
    def __init__(
        self,
        image_bytes: bytes,
        border_style: str = "none",
        initial_bg_removal_engine: str = "none",
        initial_bg_removal_params: dict[str, object] | None = None,
        initial_text_preserve_config: dict[str, object] | None = None,
        border_shader: dict[str, object] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Adjust Icon Framing")
        self._result_bytes: bytes | None = None
        self._processing_thread: QThread | None = None
        self._processing_worker: FramingProcessingWorker | None = None
        self._processing_in_progress = False
        self._pending_processing = False
        self._apply_after_processing = False
        self._spinner_apply_timer = QTimer(self)
        self._spinner_apply_timer.setSingleShot(True)
        self._spinner_apply_timer.setInterval(1500)
        self._spinner_apply_timer.timeout.connect(self._apply_processing_settings)
        initial_text_cfg = normalize_text_preserve_config(initial_text_preserve_config)
        initial_group_count = max(2, min(8, int(initial_text_cfg.color_groups)))
        self._seed_colors: list[tuple[int, int, int] | None] = [None] * initial_group_count
        for idx, color in enumerate(initial_text_cfg.seed_colors[:initial_group_count]):
            self._seed_colors[idx] = (int(color[0]), int(color[1]), int(color[2]))
        self._active_seed_pick_index: int | None = None
        self._manual_text_mark_mode = "none"
        self._manual_add_points: list[tuple[float, float]] = list(initial_text_cfg.manual_add_seeds)
        self._manual_remove_points: list[tuple[float, float]] = list(
            initial_text_cfg.manual_remove_seeds
        )
        self._manual_undo_stack: list[
            tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]
        ] = []
        self._manual_redo_stack: list[
            tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]
        ] = []
        self._manual_history_limit = 256
        self._text_roi = (
            list(initial_text_cfg.roi) if initial_text_cfg.roi is not None else None
        )
        self._canvas = IconFrameCanvas(image_bytes, border_style=border_style, parent=self)
        self._canvas.set_border_shader(border_shader)
        self._canvas.set_bg_removal_engine(initial_bg_removal_engine)
        self._canvas.set_bg_removal_params(initial_bg_removal_params or {})
        self._canvas.set_text_preserve_config(text_preserve_to_dict(initial_text_cfg))
        self._canvas.set_upscale_method("lanczos_unsharp")
        self._canvas.roiChanged.connect(self._on_canvas_roi_changed)
        self._canvas.seedColorPicked.connect(self._on_canvas_seed_color_picked)
        self._canvas.manualTextMarkPoint.connect(self._on_canvas_manual_text_mark_point)
        self._canvas.set_text_roi(
            tuple(self._text_roi) if self._text_roi is not None else None
        )
        self._canvas.set_manual_text_points(self._manual_add_points, self._manual_remove_points)

        layout = QVBoxLayout(self)
        content = QHBoxLayout()
        content.addWidget(self._canvas, 1)
        self._side_container = QWidget(self)
        self._side_container.setMinimumWidth(360)
        side = QVBoxLayout(self._side_container)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(7)

        template_row = QHBoxLayout()
        template_row.setContentsMargins(0, 0, 0, 0)
        template_row.setSpacing(6)
        template_row.addWidget(QLabel("Template:", self))
        self.border_combo = QComboBox(self)
        for label, value in icon_style_options():
            self.border_combo.addItem(label, value)
        current_idx = self.border_combo.findData(self._canvas.border_style())
        if current_idx >= 0:
            self.border_combo.setCurrentIndex(current_idx)
        self.border_combo.currentIndexChanged.connect(self._on_border_changed)
        template_row.addWidget(self.border_combo, 1)
        side.addLayout(template_row)

        zoom_row = QHBoxLayout()
        self.zoom_label = QLabel("Zoom: 100%", self)
        zoom_row.addWidget(self.zoom_label)
        zoom_row.addStretch(1)
        self.zoom_spin = QDoubleSpinBox(self)
        self.zoom_spin.setRange(20.0, 800.0)
        self.zoom_spin.setDecimals(1)
        self.zoom_spin.setSingleStep(0.5)
        self.zoom_spin.setKeyboardTracking(False)
        self.zoom_spin.setSuffix("%")
        self.zoom_spin.setValue(100.0)
        self.zoom_spin.valueChanged.connect(self._on_zoom_spin_changed)
        self.zoom_out_btn = QPushButton("Zoom -", self)
        self.zoom_in_btn = QPushButton("Zoom +", self)
        self.reset_btn = QPushButton("Reset View", self)
        self.zoom_out_btn.clicked.connect(lambda: self._set_zoom(self._canvas.zoom() / 1.03))
        self.zoom_in_btn.clicked.connect(lambda: self._set_zoom(self._canvas.zoom() * 1.03))
        self.reset_btn.clicked.connect(self._on_reset)
        zoom_row.addWidget(self.zoom_spin)
        zoom_row.addWidget(self.zoom_out_btn)
        zoom_row.addWidget(self.zoom_in_btn)
        zoom_row.addWidget(self.reset_btn)
        side.addLayout(zoom_row)

        upscale_row = QHBoxLayout()
        upscale_row.setContentsMargins(0, 0, 0, 0)
        upscale_row.setSpacing(6)
        upscale_row.addWidget(QLabel("Upscale:", self))
        self.upscale_method_combo = QComboBox(self)
        for label, value in UPSCALE_METHOD_OPTIONS:
            self.upscale_method_combo.addItem(label, value)
        upscale_idx = self.upscale_method_combo.findData(self._canvas.upscale_method())
        if upscale_idx >= 0:
            self.upscale_method_combo.setCurrentIndex(upscale_idx)
        self.upscale_method_combo.currentIndexChanged.connect(self._on_upscale_method_changed)
        self.upscale_method_combo.setToolTip(
            "Applied on final export when captured source area is smaller than 512x512."
        )
        upscale_row.addWidget(self.upscale_method_combo, 1)
        side.addLayout(upscale_row)

        self.shader_controls = BorderShaderControls(
            initial_config=self._canvas.border_shader(),
            on_change=self._on_border_shader_changed,
            parent=self,
        )
        side.addWidget(self.shader_controls, 0)

        self.cutout_label = QLabel("Cutout", self)
        cutout_row = QHBoxLayout()
        cutout_row.setContentsMargins(0, 0, 0, 0)
        cutout_row.setSpacing(6)
        cutout_row.addWidget(self.cutout_label)
        self.bg_removal_combo = QComboBox(self)
        for label, value in BACKGROUND_REMOVAL_OPTIONS:
            self.bg_removal_combo.addItem(label, value)
        bg_idx = self.bg_removal_combo.findData(
            normalize_background_removal_engine(initial_bg_removal_engine)
        )
        if bg_idx >= 0:
            self.bg_removal_combo.setCurrentIndex(bg_idx)
        self.bg_removal_combo.currentIndexChanged.connect(self._on_bg_engine_changed)
        cutout_row.addWidget(self.bg_removal_combo, 1)
        side.addLayout(cutout_row)

        self.cutout_advanced_container = QWidget(self)
        cutout_grid = QGridLayout(self.cutout_advanced_container)
        cutout_grid.setContentsMargins(0, 0, 0, 0)
        cutout_grid.setHorizontalSpacing(8)
        cutout_grid.setVerticalSpacing(3)
        cutout_grid.addWidget(QLabel("FG Threshold:", self), 0, 0)
        self.fg_threshold_spin = QSpinBox(self)
        self.fg_threshold_spin.setRange(1, 255)
        self.fg_threshold_spin.setKeyboardTracking(False)
        self.fg_threshold_spin.setValue(
            int((initial_bg_removal_params or {}).get("alpha_matting_foreground_threshold", 220))
        )
        self.fg_threshold_spin.valueChanged.connect(self._on_spinner_param_changed)
        self.fg_threshold_spin.editingFinished.connect(self._on_spinner_param_changed)
        cutout_grid.addWidget(self.fg_threshold_spin, 0, 1)
        cutout_grid.addWidget(QLabel("BG Threshold:", self), 0, 2)
        self.bg_threshold_spin = QSpinBox(self)
        self.bg_threshold_spin.setRange(0, 254)
        self.bg_threshold_spin.setKeyboardTracking(False)
        self.bg_threshold_spin.setValue(
            int((initial_bg_removal_params or {}).get("alpha_matting_background_threshold", 8))
        )
        self.bg_threshold_spin.valueChanged.connect(self._on_spinner_param_changed)
        self.bg_threshold_spin.editingFinished.connect(self._on_spinner_param_changed)
        cutout_grid.addWidget(self.bg_threshold_spin, 0, 3)
        cutout_grid.addWidget(QLabel("Erode Size:", self), 1, 0)
        self.erode_spin = QSpinBox(self)
        self.erode_spin.setRange(0, 64)
        self.erode_spin.setKeyboardTracking(False)
        self.erode_spin.setValue(
            int((initial_bg_removal_params or {}).get("alpha_matting_erode_size", 1))
        )
        self.erode_spin.valueChanged.connect(self._on_spinner_param_changed)
        self.erode_spin.editingFinished.connect(self._on_spinner_param_changed)
        cutout_grid.addWidget(self.erode_spin, 1, 1)
        cutout_grid.addWidget(QLabel("Edge Feather:", self), 1, 2)
        self.edge_feather_spin = QSpinBox(self)
        self.edge_feather_spin.setRange(0, 24)
        self.edge_feather_spin.setKeyboardTracking(False)
        self.edge_feather_spin.setValue(
            int((initial_bg_removal_params or {}).get("alpha_edge_feather", 0))
        )
        self.edge_feather_spin.valueChanged.connect(self._on_spinner_param_changed)
        self.edge_feather_spin.editingFinished.connect(self._on_spinner_param_changed)
        cutout_grid.addWidget(self.edge_feather_spin, 1, 3)

        self.alpha_matting_check = QCheckBox("Alpha Matting", self)
        self.alpha_matting_check.setChecked(
            bool((initial_bg_removal_params or {}).get("alpha_matting", False))
        )
        self.alpha_matting_check.toggled.connect(self._on_cutout_params_changed)
        cutout_grid.addWidget(self.alpha_matting_check, 2, 0, 1, 2)
        self.post_process_check = QCheckBox("Post-process Mask", self)
        self.post_process_check.setChecked(
            bool((initial_bg_removal_params or {}).get("post_process_mask", False))
        )
        self.post_process_check.toggled.connect(self._on_cutout_params_changed)
        cutout_grid.addWidget(self.post_process_check, 2, 2, 1, 2)
        side.addWidget(self.cutout_advanced_container)

        self.text_label = QLabel("Text Extraction", self)
        text_row = QHBoxLayout()
        text_row.setContentsMargins(0, 0, 0, 0)
        text_row.setSpacing(6)
        text_row.addWidget(self.text_label)
        self.text_method_combo = QComboBox(self)
        for label, value in TEXT_EXTRACTION_METHOD_OPTIONS:
            self.text_method_combo.addItem(label, value)
        init_method = normalize_text_extraction_method(
            str((initial_text_preserve_config or {}).get("method", "") or ""),
            enabled_fallback=bool((initial_text_preserve_config or {}).get("enabled", False)),
        )
        method_idx = self.text_method_combo.findData(init_method)
        if method_idx >= 0:
            self.text_method_combo.setCurrentIndex(method_idx)
        self.text_method_combo.currentIndexChanged.connect(self._on_text_preserve_changed)
        text_row.addWidget(self.text_method_combo, 1)
        side.addLayout(text_row)

        self.text_advanced_container = QWidget(self)
        text_grid = QGridLayout(self.text_advanced_container)
        text_grid.setContentsMargins(0, 0, 0, 0)
        text_grid.setHorizontalSpacing(8)
        text_grid.setVerticalSpacing(3)
        text_grid.addWidget(QLabel("Text Strength:", self), 0, 0)
        self.preserve_text_strength = QSpinBox(self)
        self.preserve_text_strength.setRange(0, 100)
        self.preserve_text_strength.setKeyboardTracking(False)
        self.preserve_text_strength.setValue(
            int((initial_text_preserve_config or {}).get("strength", 45))
        )
        self.preserve_text_strength.valueChanged.connect(self._on_spinner_param_changed)
        self.preserve_text_strength.editingFinished.connect(self._on_spinner_param_changed)
        text_grid.addWidget(self.preserve_text_strength, 0, 1)
        text_grid.addWidget(QLabel("Text Feather:", self), 0, 2)
        self.preserve_text_feather = QSpinBox(self)
        self.preserve_text_feather.setRange(0, 3)
        self.preserve_text_feather.setKeyboardTracking(False)
        self.preserve_text_feather.setValue(
            int((initial_text_preserve_config or {}).get("feather", 1))
        )
        self.preserve_text_feather.valueChanged.connect(self._on_spinner_param_changed)
        self.preserve_text_feather.editingFinished.connect(self._on_spinner_param_changed)
        text_grid.addWidget(self.preserve_text_feather, 0, 3)
        text_grid.addWidget(QLabel("Color Groups:", self), 1, 0)
        self.preserve_text_groups = QSpinBox(self)
        self.preserve_text_groups.setRange(2, 8)
        self.preserve_text_groups.setKeyboardTracking(False)
        self.preserve_text_groups.setValue(int(initial_text_cfg.color_groups))
        self.preserve_text_groups.valueChanged.connect(self._on_text_groups_changed)
        self.preserve_text_groups.valueChanged.connect(self._on_spinner_param_changed)
        self.preserve_text_groups.editingFinished.connect(self._on_spinner_param_changed)
        text_grid.addWidget(self.preserve_text_groups, 1, 1)
        text_grid.addWidget(QLabel("Glow Radius:", self), 1, 2)
        self.preserve_text_glow_radius = QSpinBox(self)
        self.preserve_text_glow_radius.setRange(0, 12)
        self.preserve_text_glow_radius.setKeyboardTracking(False)
        self.preserve_text_glow_radius.setValue(int(initial_text_cfg.glow_radius))
        self.preserve_text_glow_radius.valueChanged.connect(self._on_spinner_param_changed)
        self.preserve_text_glow_radius.editingFinished.connect(self._on_spinner_param_changed)
        text_grid.addWidget(self.preserve_text_glow_radius, 1, 3)

        self.preserve_text_outline = QCheckBox("Include Outline", self)
        self.preserve_text_outline.setChecked(bool(initial_text_cfg.include_outline))
        self.preserve_text_outline.toggled.connect(self._on_text_preserve_changed)
        text_grid.addWidget(self.preserve_text_outline, 2, 0, 1, 2)

        self.preserve_text_shadow = QCheckBox("Include Shadow", self)
        self.preserve_text_shadow.setChecked(bool(initial_text_cfg.include_shadow))
        self.preserve_text_shadow.toggled.connect(self._on_text_preserve_changed)
        text_grid.addWidget(self.preserve_text_shadow, 2, 2, 1, 2)

        text_grid.addWidget(QLabel("Glow Mode:", self), 3, 0)
        self.preserve_text_glow_mode = QComboBox(self)
        self.preserve_text_glow_mode.addItem("Disabled", "disabled")
        self.preserve_text_glow_mode.addItem("Bright", "bright")
        self.preserve_text_glow_mode.addItem("Dark", "dark")
        self.preserve_text_glow_mode.addItem("Both", "both")
        glow_idx = self.preserve_text_glow_mode.findData(str(initial_text_cfg.glow_mode))
        if glow_idx >= 0:
            self.preserve_text_glow_mode.setCurrentIndex(glow_idx)
        self.preserve_text_glow_mode.currentIndexChanged.connect(self._on_text_preserve_changed)
        text_grid.addWidget(self.preserve_text_glow_mode, 3, 1)
        text_grid.addWidget(QLabel("Glow Strength:", self), 3, 2)
        self.preserve_text_glow_strength = QSpinBox(self)
        self.preserve_text_glow_strength.setRange(0, 100)
        self.preserve_text_glow_strength.setKeyboardTracking(False)
        self.preserve_text_glow_strength.setValue(int(initial_text_cfg.glow_strength))
        self.preserve_text_glow_strength.valueChanged.connect(self._on_spinner_param_changed)
        self.preserve_text_glow_strength.editingFinished.connect(
            self._on_spinner_param_changed
        )
        text_grid.addWidget(self.preserve_text_glow_strength, 3, 3)
        text_grid.addWidget(QLabel("Seed Tol:", self), 4, 0)
        self.preserve_text_seed_tolerance = QSpinBox(self)
        self.preserve_text_seed_tolerance.setRange(4, 96)
        self.preserve_text_seed_tolerance.setKeyboardTracking(False)
        self.preserve_text_seed_tolerance.setValue(int(initial_text_cfg.seed_tolerance))
        self.preserve_text_seed_tolerance.valueChanged.connect(self._on_spinner_param_changed)
        self.preserve_text_seed_tolerance.editingFinished.connect(
            self._on_spinner_param_changed
        )
        text_grid.addWidget(self.preserve_text_seed_tolerance, 4, 1)
        side.addWidget(self.text_advanced_container)

        self.seed_controls_label = QLabel("Seed Colors (one per active group)", self)
        side.addWidget(self.seed_controls_label)
        self.seed_controls_container = QWidget(self)
        self.seed_controls_layout = QGridLayout(self.seed_controls_container)
        self.seed_controls_layout.setContentsMargins(0, 0, 0, 0)
        self.seed_controls_layout.setHorizontalSpacing(8)
        self.seed_controls_layout.setVerticalSpacing(3)
        side.addWidget(self.seed_controls_container)
        self._seed_swatch_buttons: list[SeedColorButton] = []
        self._rebuild_seed_color_controls()

        self.manual_mark_label = QLabel("Manual Text Marks", self)
        side.addWidget(self.manual_mark_label)
        self.manual_mark_controls_container = QWidget(self)
        manual_mark_row = QHBoxLayout(self.manual_mark_controls_container)
        manual_mark_row.setContentsMargins(0, 0, 0, 0)
        manual_mark_row.setSpacing(5)
        self.manual_mark_undo_btn = QPushButton("↺", self)
        self.manual_mark_redo_btn = QPushButton("↻", self)
        self.manual_mark_undo_btn.setToolTip("Undo last manual text mark change")
        self.manual_mark_redo_btn.setToolTip("Redo last undone manual text mark change")
        self.manual_mark_undo_btn.clicked.connect(self._on_manual_mark_undo)
        self.manual_mark_redo_btn.clicked.connect(self._on_manual_mark_redo)
        self.manual_mark_add_btn = QPushButton("Add", self)
        self.manual_mark_remove_btn = QPushButton("Remove", self)
        self.manual_mark_stop_btn = QPushButton("Stop", self)
        self.manual_mark_add_btn.clicked.connect(self._on_manual_mark_add_mode)
        self.manual_mark_remove_btn.clicked.connect(self._on_manual_mark_remove_mode)
        self.manual_mark_stop_btn.clicked.connect(self._on_manual_mark_stop_mode)
        manual_mark_row.addWidget(self.manual_mark_undo_btn)
        manual_mark_row.addWidget(self.manual_mark_redo_btn)
        manual_mark_row.addWidget(self.manual_mark_add_btn)
        manual_mark_row.addWidget(self.manual_mark_remove_btn)
        manual_mark_row.addWidget(self.manual_mark_stop_btn)
        side.addWidget(self.manual_mark_controls_container)
        self.manual_mark_count_label = QLabel("", self)
        side.addWidget(self.manual_mark_count_label)

        self.roi_controls_container = QWidget(self)
        roi_row = QHBoxLayout(self.roi_controls_container)
        roi_row.setContentsMargins(0, 0, 0, 0)
        roi_row.setSpacing(5)
        self.roi_draw_btn = QPushButton("Draw ROI", self)
        self.roi_draw_btn.setCheckable(True)
        self.roi_draw_btn.toggled.connect(self._on_roi_draw_toggled)
        self.roi_clear_btn = QPushButton("Clear ROI", self)
        self.roi_clear_btn.clicked.connect(self._on_clear_roi)
        self.roi_value_label = QLabel("", self)
        self.roi_value_label.setWordWrap(True)
        roi_row.addWidget(self.roi_draw_btn)
        roi_row.addWidget(self.roi_clear_btn)
        roi_row.addWidget(self.roi_value_label, 1)
        side.addWidget(self.roi_controls_container)

        self.debug_text_alpha_check = QCheckBox("Debug Text/Glow Alpha Mask", self)
        self.debug_text_alpha_check.setChecked(False)
        self.debug_text_alpha_check.toggled.connect(self._on_debug_text_alpha_toggled)
        side.addWidget(self.debug_text_alpha_check)

        side.addWidget(QLabel("Layer Visibility", self))
        layer_buttons_row = QHBoxLayout()
        self.layer_all_btn = QPushButton("All", self)
        self.layer_none_btn = QPushButton("None", self)
        self.layer_all_btn.clicked.connect(self._on_layers_all)
        self.layer_none_btn.clicked.connect(self._on_layers_none)
        layer_buttons_row.addWidget(self.layer_all_btn)
        layer_buttons_row.addWidget(self.layer_none_btn)
        layer_buttons_row.addStretch(1)
        side.addLayout(layer_buttons_row)

        self.layer_background_check = QCheckBox("Background", self)
        self.layer_template_check = QCheckBox("Template", self)
        self.layer_cutout_check = QCheckBox("Cutout", self)
        self.layer_text_check = QCheckBox("Text", self)
        layer_grid_widget = QWidget(self)
        layer_grid = QGridLayout(layer_grid_widget)
        layer_grid.setContentsMargins(0, 0, 0, 0)
        layer_grid.setHorizontalSpacing(8)
        layer_grid.setVerticalSpacing(3)
        for checkbox in (
            self.layer_background_check,
            self.layer_template_check,
            self.layer_cutout_check,
            self.layer_text_check,
        ):
            checkbox.setChecked(True)
            checkbox.toggled.connect(self._on_layer_visibility_changed)
        layer_grid.addWidget(self.layer_background_check, 0, 0)
        layer_grid.addWidget(self.layer_template_check, 0, 1)
        layer_grid.addWidget(self.layer_cutout_check, 1, 0)
        layer_grid.addWidget(self.layer_text_check, 1, 1)
        side.addWidget(layer_grid_widget)

        self.cutout_status_label = QLabel("", self)
        side.addWidget(self.cutout_status_label)
        side.addStretch(1)
        self._side_scroll = QScrollArea(self)
        self._side_scroll.setWidgetResizable(True)
        self._side_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._side_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self._side_scroll.setWidget(self._side_container)
        self._side_scroll.setMinimumWidth(380)
        content.addWidget(self._side_scroll, 0)
        layout.addLayout(content, 1)

        self._status_bar = QFrame(self)
        self._status_bar.setFrameShape(QFrame.Shape.StyledPanel)
        status_row = QHBoxLayout(self._status_bar)
        status_row.setContentsMargins(8, 4, 8, 4)
        status_row.setSpacing(6)
        self.processing_status_label = QLabel("Ready.", self._status_bar)
        self.processing_status_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        status_row.addWidget(self.processing_status_label, 1)
        status_row.addStretch(0)

        self._sync_template_dependents()
        self._set_manual_mark_mode("none")
        self._refresh_manual_mark_count_label()
        self._update_manual_history_buttons()
        self._apply_processing_settings()
        self._apply_layer_visibility_to_canvas()
        self._refresh_cutout_status()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_apply)
        buttons.rejected.connect(self.reject)
        self._button_bar_container = QWidget(self)
        self._button_bar_container.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Preferred,
        )
        button_row = QHBoxLayout(self._button_bar_container)
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.addStretch(1)
        button_row.addWidget(buttons)

        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(8)
        bottom_row.addWidget(self._status_bar, 1)
        bottom_row.addWidget(self._button_bar_container, 0)
        layout.addLayout(bottom_row, 0)
        QTimer.singleShot(0, self._sync_bottom_row_widths)
        self._apply_available_screen_bounds()
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def _sync_bottom_row_widths(self) -> None:
        side_width = max(300, int(self._side_scroll.width()))
        self._button_bar_container.setFixedWidth(side_width)

    def _apply_available_screen_bounds(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        self.setMaximumSize(available.width(), available.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_bottom_row_widths()

    def _set_zoom(self, zoom: float) -> None:
        self._canvas.set_zoom(zoom)
        zoom_pct = self._canvas.zoom() * 100.0
        self.zoom_label.setText(f"Zoom: {zoom_pct:.1f}%")
        blocked = self.zoom_spin.blockSignals(True)
        self.zoom_spin.setValue(zoom_pct)
        self.zoom_spin.blockSignals(blocked)

    def _on_zoom_spin_changed(self, value: float) -> None:
        self._set_zoom(float(value) / 100.0)

    def _on_border_changed(self) -> None:
        style = str(self.border_combo.currentData() or "none")
        self._canvas.set_border_style(style)
        self._sync_template_dependents()
        self._apply_processing_settings()

    def _on_border_shader_changed(self, config: dict[str, object]) -> None:
        self._canvas.set_border_shader(config)

    def _on_upscale_method_changed(self) -> None:
        self._canvas.set_upscale_method(str(self.upscale_method_combo.currentData() or "qt_smooth"))

    def _on_bg_engine_changed(self) -> None:
        self._sync_template_dependents()
        self._apply_processing_settings()

    def _on_cutout_params_changed(self, *_args) -> None:
        self._apply_processing_settings()

    def _on_text_preserve_changed(self, *_args) -> None:
        self._sync_template_dependents()
        self._apply_processing_settings()

    def _sync_seed_color_count(self) -> None:
        group_count = max(2, min(8, int(self.preserve_text_groups.value())))
        if len(self._seed_colors) < group_count:
            self._seed_colors.extend([None] * (group_count - len(self._seed_colors)))
        elif len(self._seed_colors) > group_count:
            self._seed_colors = self._seed_colors[:group_count]
        if (
            self._active_seed_pick_index is not None
            and self._active_seed_pick_index >= group_count
        ):
            self._active_seed_pick_index = None
            self._canvas.set_seed_pick_mode(False)

    def _rebuild_seed_color_controls(self) -> None:
        self._sync_seed_color_count()
        while self.seed_controls_layout.count() > 0:
            item = self.seed_controls_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._seed_swatch_buttons = []

        for idx in range(len(self._seed_colors)):
            row = idx // 2
            col = idx % 2
            swatch = SeedColorButton("", self.seed_controls_container)
            swatch.setMinimumHeight(24)
            swatch.singleClicked.connect(
                lambda group_index=idx: self._on_seed_swatch_clicked(group_index)
            )
            swatch.doubleClicked.connect(
                lambda group_index=idx: self._on_seed_swatch_double_clicked(group_index)
            )
            self._seed_swatch_buttons.append(swatch)
            self.seed_controls_layout.addWidget(swatch, row, col)
        self._refresh_seed_color_controls()

    def _refresh_seed_color_controls(self) -> None:
        for idx, swatch in enumerate(self._seed_swatch_buttons):
            color = self._seed_colors[idx] if idx < len(self._seed_colors) else None
            if color is None:
                swatch.setText(f"Group {idx + 1}")
                swatch.setStyleSheet(
                    "QPushButton { background: #2e2e2e; color: #cfcfcf; border: 1px solid #5b5b5b; }"
                )
                swatch.setToolTip(
                    f"Group {idx + 1}: click to choose color, double-click to clear."
                )
            else:
                red, green, blue = color
                lightness = (red * 0.299) + (green * 0.587) + (blue * 0.114)
                text_color = "#000000" if lightness >= 140.0 else "#ffffff"
                swatch.setText(f"#{red:02X}{green:02X}{blue:02X}")
                swatch.setStyleSheet(
                    "QPushButton { "
                    f"background: rgb({red},{green},{blue}); color: {text_color}; "
                    "border: 1px solid #5b5b5b; }"
                )
                swatch.setToolTip(f"Seed color {idx + 1}: RGB({red}, {green}, {blue})")
            if self._active_seed_pick_index == idx and self._canvas.seed_pick_mode():
                swatch.setStyleSheet(
                    swatch.styleSheet() + " QPushButton { border: 2px solid #f0c14b; }"
                )
            swatch.setEnabled(self._text_method_enabled() and not self._processing_in_progress)

    def _on_text_groups_changed(self, *_args) -> None:
        self._rebuild_seed_color_controls()
        self._sync_template_dependents()
        self._refresh_cutout_status()

    def _on_seed_swatch_clicked(self, index: int) -> None:
        if index < 0 or index >= len(self._seed_colors):
            return
        if self._active_seed_pick_index == index and self._canvas.seed_pick_mode():
            self._active_seed_pick_index = None
            self._canvas.set_seed_pick_mode(False)
            self._refresh_seed_color_controls()
            self._refresh_cutout_status()
            return
        self._active_seed_pick_index = index
        self._canvas.set_seed_pick_mode(True)
        self._refresh_seed_color_controls()
        self._refresh_cutout_status()

    def _on_seed_swatch_double_clicked(self, index: int) -> None:
        if index < 0 or index >= len(self._seed_colors):
            return
        self._seed_colors[index] = None
        if self._active_seed_pick_index == index:
            self._active_seed_pick_index = None
            self._canvas.set_seed_pick_mode(False)
        self._refresh_seed_color_controls()
        self._apply_processing_settings()

    def _on_canvas_seed_color_picked(self, value: object) -> None:
        if self._active_seed_pick_index is None:
            return
        try:
            red = int(value[0])  # type: ignore[index]
            green = int(value[1])  # type: ignore[index]
            blue = int(value[2])  # type: ignore[index]
        except Exception:
            self._active_seed_pick_index = None
            self._canvas.set_seed_pick_mode(False)
            self._refresh_seed_color_controls()
            self._refresh_cutout_status()
            return
        idx = self._active_seed_pick_index
        if 0 <= idx < len(self._seed_colors):
            self._seed_colors[idx] = (
                max(0, min(255, red)),
                max(0, min(255, green)),
                max(0, min(255, blue)),
            )
        self._active_seed_pick_index = None
        self._canvas.set_seed_pick_mode(False)
        self._refresh_seed_color_controls()
        self._apply_processing_settings()

    def _set_manual_mark_mode(self, mode: str) -> None:
        normalized = str(mode or "none").strip().casefold()
        if normalized not in {"none", "add", "remove"}:
            normalized = "none"
        self._manual_text_mark_mode = normalized
        self._canvas.set_text_mark_mode(normalized)
        self.manual_mark_add_btn.setText("Adding..." if normalized == "add" else "Add")
        self.manual_mark_remove_btn.setText("Removing..." if normalized == "remove" else "Remove")
        self._refresh_cutout_status()

    def _on_manual_mark_add_mode(self) -> None:
        self._set_manual_mark_mode("none" if self._manual_text_mark_mode == "add" else "add")

    def _on_manual_mark_remove_mode(self) -> None:
        self._set_manual_mark_mode(
            "none" if self._manual_text_mark_mode == "remove" else "remove"
        )

    def _on_manual_mark_stop_mode(self) -> None:
        self._set_manual_mark_mode("none")

    def _manual_points_snapshot(
        self,
    ) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
        return (tuple(self._manual_add_points), tuple(self._manual_remove_points))

    def _push_manual_undo_snapshot(self) -> None:
        self._manual_undo_stack.append(self._manual_points_snapshot())
        if len(self._manual_undo_stack) > self._manual_history_limit:
            del self._manual_undo_stack[0]
        self._manual_redo_stack.clear()
        self._update_manual_history_buttons()

    def _restore_manual_points(
        self,
        snapshot: tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]],
        *,
        apply_processing: bool,
    ) -> None:
        add_points, remove_points = snapshot
        self._manual_add_points = list(add_points)
        self._manual_remove_points = list(remove_points)
        self._canvas.set_manual_text_points(self._manual_add_points, self._manual_remove_points)
        self._refresh_manual_mark_count_label()
        if apply_processing:
            self._apply_processing_settings()

    def _update_manual_history_buttons(self) -> None:
        can_edit = self._text_method_enabled() and not self._processing_in_progress
        self.manual_mark_undo_btn.setEnabled(can_edit and bool(self._manual_undo_stack))
        self.manual_mark_redo_btn.setEnabled(can_edit and bool(self._manual_redo_stack))

    def _on_manual_mark_undo(self) -> None:
        if not self._manual_undo_stack:
            return
        current = self._manual_points_snapshot()
        snapshot = self._manual_undo_stack.pop()
        self._manual_redo_stack.append(current)
        if len(self._manual_redo_stack) > self._manual_history_limit:
            del self._manual_redo_stack[0]
        self._restore_manual_points(snapshot, apply_processing=True)
        self._update_manual_history_buttons()

    def _on_manual_mark_redo(self) -> None:
        if not self._manual_redo_stack:
            return
        current = self._manual_points_snapshot()
        snapshot = self._manual_redo_stack.pop()
        self._manual_undo_stack.append(current)
        if len(self._manual_undo_stack) > self._manual_history_limit:
            del self._manual_undo_stack[0]
        self._restore_manual_points(snapshot, apply_processing=True)
        self._update_manual_history_buttons()

    def _upsert_manual_mark_point(
        self,
        points: list[tuple[float, float]],
        point: tuple[float, float],
    ) -> None:
        x_new, y_new = point
        for idx, (x_old, y_old) in enumerate(points):
            if abs(x_old - x_new) <= 0.002 and abs(y_old - y_new) <= 0.002:
                points[idx] = point
                return
        points.append(point)
        if len(points) > 512:
            del points[0]

    def _on_canvas_manual_text_mark_point(self, value: object) -> None:
        if self._manual_text_mark_mode not in {"add", "remove"}:
            return
        try:
            x_val = float(value[0])  # type: ignore[index]
            y_val = float(value[1])  # type: ignore[index]
        except Exception:
            return
        point = (max(0.0, min(1.0, x_val)), max(0.0, min(1.0, y_val)))
        before = self._manual_points_snapshot()
        if self._manual_text_mark_mode == "add":
            self._upsert_manual_mark_point(self._manual_add_points, point)
        else:
            self._upsert_manual_mark_point(self._manual_remove_points, point)
        after = self._manual_points_snapshot()
        if after != before:
            self._manual_undo_stack.append(before)
            if len(self._manual_undo_stack) > self._manual_history_limit:
                del self._manual_undo_stack[0]
            self._manual_redo_stack.clear()
            self._update_manual_history_buttons()
        self._canvas.set_manual_text_points(self._manual_add_points, self._manual_remove_points)
        self._refresh_manual_mark_count_label()
        self._apply_processing_settings()

    def _refresh_manual_mark_count_label(self) -> None:
        self.manual_mark_count_label.setText(
            f"Add: {len(self._manual_add_points)}   Remove: {len(self._manual_remove_points)}"
        )
        self._update_manual_history_buttons()

    def _on_roi_draw_toggled(self, enabled: bool) -> None:
        self._canvas.set_roi_draw_mode(bool(enabled))
        if enabled:
            self.roi_draw_btn.setText("Drawing ROI...")
        else:
            self.roi_draw_btn.setText("Draw ROI")
        self._refresh_cutout_status()

    def _on_clear_roi(self) -> None:
        self._text_roi = None
        self._canvas.set_text_roi(None)
        if self.roi_draw_btn.isChecked():
            self.roi_draw_btn.setChecked(False)
        self._apply_processing_settings()
        self._update_roi_label()

    def _on_canvas_roi_changed(self, roi_value: object) -> None:
        roi: list[float] | None = None
        if isinstance(roi_value, (list, tuple)) and len(roi_value) >= 4:
            try:
                roi = [
                    float(roi_value[0]),
                    float(roi_value[1]),
                    float(roi_value[2]),
                    float(roi_value[3]),
                ]
            except (TypeError, ValueError):
                roi = None
        self._text_roi = roi
        self._update_roi_label()
        self._apply_processing_settings()

    def _update_roi_label(self) -> None:
        if not self._text_roi:
            self.roi_value_label.setText("ROI: full image")
            return
        x, y, w, h = self._text_roi
        self.roi_value_label.setText(
            f"ROI: x={x:.2f} y={y:.2f} w={w:.2f} h={h:.2f}"
        )

    def _on_debug_text_alpha_toggled(self, enabled: bool) -> None:
        self._canvas.set_debug_text_alpha_only(bool(enabled))
        self._refresh_cutout_status()

    def _on_spinner_param_changed(self, *_args) -> None:
        self._spinner_apply_timer.start()
        self._refresh_cutout_status()

    def _set_processing_status(self, text: str) -> None:
        self.processing_status_label.setText(text.strip() or "Ready.")

    def _set_processing_controls_busy(self, busy: bool) -> None:
        self.border_combo.setEnabled(not busy)
        self.zoom_spin.setEnabled(not busy)
        self.zoom_out_btn.setEnabled(not busy)
        self.zoom_in_btn.setEnabled(not busy)
        self.reset_btn.setEnabled(not busy)
        self.upscale_method_combo.setEnabled(not busy)
        self.bg_removal_combo.setEnabled(not busy and self._template_enabled())
        self.text_method_combo.setEnabled(not busy and self._template_enabled())
        self.layer_all_btn.setEnabled(not busy)
        self.layer_none_btn.setEnabled(not busy)
        self.roi_draw_btn.setEnabled(not busy and self._roi_method_enabled())
        self.roi_clear_btn.setEnabled(not busy and self._roi_method_enabled())
        for button in self._seed_swatch_buttons:
            button.setEnabled(not busy and self._text_method_enabled())
        self.manual_mark_undo_btn.setEnabled(False)
        self.manual_mark_redo_btn.setEnabled(False)
        self.manual_mark_add_btn.setEnabled(not busy and self._text_method_enabled())
        self.manual_mark_remove_btn.setEnabled(not busy and self._text_method_enabled())
        self.manual_mark_stop_btn.setEnabled(not busy and self._text_method_enabled())
        if not busy:
            self._update_manual_history_buttons()

    def _start_processing_worker(
        self,
        bg_engine: str,
        bg_params: dict[str, object],
        text_cfg: dict[str, object],
        *,
        text_debug_alpha: bool,
    ) -> None:
        include_cutout = bg_engine != "none"
        include_text = bool(text_cfg.get("enabled", False)) and (
            str(text_cfg.get("method", "none")) != "none"
        )
        if self._processing_in_progress:
            self._pending_processing = True
            self._set_processing_status("Queued settings update...")
            return
        if not include_cutout and not include_text:
            self._canvas.set_bg_removal_engine(bg_engine)
            self._canvas.set_bg_removal_params(bg_params)
            self._canvas.set_text_preserve_config(text_cfg)
            self._refresh_cutout_status()
            self._set_processing_status("Ready.")
            return

        self._processing_in_progress = True
        self._pending_processing = False
        self._canvas.set_async_processing_busy(True)
        self._set_processing_controls_busy(True)
        self._set_processing_status("Preparing layers...")
        thread = QThread(self)
        worker = FramingProcessingWorker(
            source_image_bytes=self._canvas.source_image_bytes(),
            bg_engine=bg_engine,
            bg_params=bg_params,
            text_config=text_cfg,
            include_cutout=include_cutout,
            include_text_overlay=include_text,
            include_text_alpha=include_text and text_debug_alpha,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._on_processing_progress)
        worker.completed.connect(
            lambda payload, be=bg_engine, bp=bg_params, tc=text_cfg: self._on_processing_completed(
                payload, be, bp, tc
            )
        )
        worker.failed.connect(self._on_processing_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_processing_finished)
        self._processing_thread = thread
        self._processing_worker = worker
        thread.start()

    def _on_processing_progress(self, stage: str, current: int, total: int) -> None:
        current_i = max(0, int(current))
        total_i = max(0, int(total))
        if total_i > 0:
            current_i = min(current_i, total_i)
            pct = int(round((current_i / total_i) * 100.0))
            self._set_processing_status(f"{stage}: {current_i}/{total_i} ({pct}%)")
            return
        self._set_processing_status(stage)

    def _on_processing_completed(
        self,
        payload_obj: object,
        bg_engine: str,
        bg_params: dict[str, object],
        text_cfg: dict[str, object],
    ) -> None:
        payload = payload_obj if isinstance(payload_obj, dict) else {}
        self._canvas.set_bg_removal_engine(bg_engine)
        self._canvas.set_bg_removal_params(bg_params)
        self._canvas.set_text_preserve_config(text_cfg)

        cutout_key = self._canvas.build_cutout_cache_key(bg_engine, bg_params)
        self._canvas.store_cutout_payload(
            cutout_key,
            payload.get("cutout_bytes") if isinstance(payload.get("cutout_bytes"), (bytes, bytearray)) else None,
            error=str(payload.get("cutout_error") or "").strip() or None,
        )

        text_key = self._canvas.build_text_overlay_cache_key(bg_engine, bg_params, text_cfg)
        text_payload = payload.get("text_overlay_bytes")
        self._canvas.store_text_overlay_payload(
            text_key,
            bytes(text_payload) if isinstance(text_payload, (bytes, bytearray)) else None,
        )

        text_alpha_key = self._canvas.build_text_alpha_cache_key(bg_engine, bg_params, text_cfg)
        text_alpha_payload = payload.get("text_alpha_bytes")
        self._canvas.store_text_alpha_payload(
            text_alpha_key,
            bytes(text_alpha_payload) if isinstance(text_alpha_payload, (bytes, bytearray)) else None,
        )
        self._canvas.update()
        self._refresh_cutout_status()
        self._set_processing_status("Ready.")

    def _on_processing_failed(self, message: str) -> None:
        self._canvas.set_bg_removal_engine(self.selected_bg_removal_engine())
        self._canvas.set_bg_removal_params(self.selected_bg_removal_params())
        self._canvas.set_text_preserve_config(self.selected_text_preserve_config())
        self._canvas.update()
        self._set_processing_status(f"Processing failed: {message.strip() or 'Unknown error'}")
        self._refresh_cutout_status()

    def _on_processing_finished(self) -> None:
        self._processing_thread = None
        self._processing_worker = None
        self._processing_in_progress = False
        self._canvas.set_async_processing_busy(False)
        self._set_processing_controls_busy(False)
        if self._pending_processing:
            self._pending_processing = False
            self._apply_processing_settings()
            return
        if self._apply_after_processing:
            self._apply_after_processing = False
            QTimer.singleShot(0, self._on_apply)

    def _apply_processing_settings(self) -> None:
        if self._spinner_apply_timer.isActive():
            self._spinner_apply_timer.stop()
        self._start_processing_worker(
            self.selected_bg_removal_engine(),
            self.selected_bg_removal_params(),
            self.selected_text_preserve_config(),
            text_debug_alpha=self.debug_text_alpha_check.isChecked(),
        )

    def _on_layer_visibility_changed(self, _enabled: bool) -> None:
        self._apply_layer_visibility_to_canvas()
        self._refresh_cutout_status()

    def _on_layers_all(self) -> None:
        for checkbox in (
            self.layer_background_check,
            self.layer_template_check,
            self.layer_cutout_check,
            self.layer_text_check,
        ):
            checkbox.setChecked(True)
        self._apply_layer_visibility_to_canvas()
        self._refresh_cutout_status()

    def _on_layers_none(self) -> None:
        for checkbox in (
            self.layer_background_check,
            self.layer_template_check,
            self.layer_cutout_check,
            self.layer_text_check,
        ):
            checkbox.setChecked(False)
        self._apply_layer_visibility_to_canvas()
        self._refresh_cutout_status()

    def _apply_layer_visibility_to_canvas(self) -> None:
        self._canvas.set_layer_visibility("base", self.layer_background_check.isChecked())
        self._canvas.set_layer_visibility("template", self.layer_template_check.isChecked())
        self._canvas.set_layer_visibility("cutout", self.layer_cutout_check.isChecked())
        self._canvas.set_layer_visibility("text", self.layer_text_check.isChecked())

    def _refresh_cutout_status(self) -> None:
        if self._processing_in_progress:
            self.cutout_status_label.setText("Processing preview layers in background...")
            return
        if self._spinner_apply_timer.isActive():
            self.cutout_status_label.setText("Spinner changes pending (auto-apply in 1.5s).")
            return
        if self._canvas.seed_pick_mode():
            if self._active_seed_pick_index is not None:
                self.cutout_status_label.setText(
                    f"Click image to pick seed color for group {self._active_seed_pick_index + 1}."
                )
            else:
                self.cutout_status_label.setText("Click image to pick seed color.")
            return
        if self._manual_text_mark_mode == "add":
            self.cutout_status_label.setText("Manual Add mode: click image to add text zones.")
            return
        if self._manual_text_mark_mode == "remove":
            self.cutout_status_label.setText("Manual Remove mode: click image to remove text zones.")
            return
        if not self._template_enabled():
            self.cutout_status_label.setText("Template is Disabled; cutout/text layers are disabled.")
            return
        show_cutout = self.layer_cutout_check.isChecked()
        show_text = self.layer_text_check.isChecked()
        if not show_cutout and not show_text:
            self.cutout_status_label.setText("")
            return
        engine = self.selected_bg_removal_engine()
        text_method = self.selected_text_extraction_method()
        error = self._canvas.cutout_error()
        if error:
            self.cutout_status_label.setText(f"Cutout preview warning: {error}")
            return
        if show_cutout and engine == "none":
            self.cutout_status_label.setText("Cutout layer visible but cutout method is Disabled.")
            return
        if show_text and text_method == "none":
            self.cutout_status_label.setText(
                "Text layer visible but text extraction method is Disabled."
            )
            return
        if text_method == "roi_guided":
            roi_note = "ROI set" if self._text_roi else "ROI not set (using full image)"
            if show_text and self.debug_text_alpha_check.isChecked():
                self.cutout_status_label.setText(f"Debug preview: text/glow alpha mask only. {roi_note}.")
                return
            if show_text:
                self.cutout_status_label.setText(f"Text layer preview active. {roi_note}.")
                return
        if show_text and self.debug_text_alpha_check.isChecked():
            self.cutout_status_label.setText("Debug preview: text/glow alpha mask only.")
            return
        if show_cutout and show_text:
            self.cutout_status_label.setText("Cutout + text layer preview active.")
            return
        if show_cutout:
            self.cutout_status_label.setText("Cutout layer preview active.")
            return
        self.cutout_status_label.setText("Text layer preview active.")

    def _on_reset(self) -> None:
        self._canvas.reset_view()
        self._set_zoom(1.0)

    def _on_apply(self) -> None:
        if self._processing_in_progress:
            self._apply_after_processing = True
            self._set_processing_status("Wait for background processing to finish.")
            return
        if self._spinner_apply_timer.isActive():
            self._spinner_apply_timer.stop()
            self._apply_after_processing = True
            self._apply_processing_settings()
            if self._processing_in_progress:
                self._set_processing_status("Processing layers before final render...")
                return
        self._result_bytes = self._canvas.export_composited_png_bytes(512)
        self.accept()

    def reject(self) -> None:
        if self._processing_in_progress:
            self._set_processing_status("Wait for background processing to finish before closing.")
            return
        super().reject()

    def framed_image_bytes(self) -> bytes | None:
        return self._result_bytes

    def framed_image_is_final_composite(self) -> bool:
        return True

    def selected_style(self) -> str:
        return self._canvas.border_style()

    def _template_enabled(self) -> bool:
        return self.selected_style() != "none"

    def _text_method_enabled(self) -> bool:
        return self._template_enabled() and self.selected_text_extraction_method() != "none"

    def _cutout_method_enabled(self) -> bool:
        return self._template_enabled() and self.selected_bg_removal_engine() != "none"

    def _roi_method_enabled(self) -> bool:
        return self._text_method_enabled() and self.selected_text_extraction_method() == "roi_guided"

    def _sync_template_dependents(self) -> None:
        template_enabled = self._template_enabled()
        text_method = self.selected_text_extraction_method() if template_enabled else "none"
        text_enabled = template_enabled and text_method != "none"
        cutout_enabled = template_enabled and self.selected_bg_removal_engine() != "none"
        roi_enabled = text_enabled and text_method == "roi_guided"

        # Keep layer visibility synchronized with mode selectors.
        if self.layer_template_check.isChecked() != template_enabled:
            self.layer_template_check.setChecked(template_enabled)
        if self.layer_cutout_check.isChecked() != cutout_enabled:
            self.layer_cutout_check.setChecked(cutout_enabled)
        if self.layer_text_check.isChecked() != text_enabled:
            self.layer_text_check.setChecked(text_enabled)

        if not template_enabled:
            bg_idx = self.bg_removal_combo.findData("none")
            if bg_idx >= 0 and self.bg_removal_combo.currentIndex() != bg_idx:
                blocked = self.bg_removal_combo.blockSignals(True)
                self.bg_removal_combo.setCurrentIndex(bg_idx)
                self.bg_removal_combo.blockSignals(blocked)
            text_idx = self.text_method_combo.findData("none")
            if text_idx >= 0 and self.text_method_combo.currentIndex() != text_idx:
                blocked = self.text_method_combo.blockSignals(True)
                self.text_method_combo.setCurrentIndex(text_idx)
                self.text_method_combo.blockSignals(blocked)

        self.bg_removal_combo.setEnabled(template_enabled)
        self.alpha_matting_check.setEnabled(cutout_enabled)
        self.fg_threshold_spin.setEnabled(cutout_enabled)
        self.bg_threshold_spin.setEnabled(cutout_enabled)
        self.erode_spin.setEnabled(cutout_enabled)
        self.edge_feather_spin.setEnabled(cutout_enabled)
        self.post_process_check.setEnabled(cutout_enabled)

        self.text_method_combo.setEnabled(template_enabled)
        self.preserve_text_strength.setEnabled(text_enabled)
        self.preserve_text_feather.setEnabled(text_enabled)
        self.preserve_text_groups.setEnabled(text_enabled)
        self.preserve_text_outline.setEnabled(text_enabled)
        self.preserve_text_shadow.setEnabled(text_enabled)
        self.preserve_text_glow_mode.setEnabled(text_enabled)
        self.preserve_text_glow_radius.setEnabled(text_enabled)
        self.preserve_text_glow_strength.setEnabled(text_enabled)
        self.preserve_text_seed_tolerance.setEnabled(text_enabled)
        self.debug_text_alpha_check.setEnabled(text_enabled)
        if not text_enabled and self.debug_text_alpha_check.isChecked():
            self.debug_text_alpha_check.setChecked(False)
        if not roi_enabled and self.roi_draw_btn.isChecked():
            self.roi_draw_btn.setChecked(False)
        if not text_enabled:
            self._active_seed_pick_index = None
            self._canvas.set_seed_pick_mode(False)
            self._set_manual_mark_mode("none")

        self.shader_controls.setVisible(template_enabled)
        self.cutout_label.setVisible(template_enabled)
        self.bg_removal_combo.setVisible(template_enabled)
        self.cutout_advanced_container.setVisible(cutout_enabled)
        self.text_label.setVisible(template_enabled)
        self.text_method_combo.setVisible(template_enabled)
        self.text_advanced_container.setVisible(text_enabled)
        self.seed_controls_label.setVisible(text_enabled)
        self.seed_controls_container.setVisible(text_enabled)
        self.manual_mark_label.setVisible(text_enabled)
        self.manual_mark_controls_container.setVisible(text_enabled)
        self.manual_mark_count_label.setVisible(text_enabled)
        self.roi_controls_container.setVisible(roi_enabled)
        self.debug_text_alpha_check.setVisible(text_enabled)

        self.layer_cutout_check.setEnabled(template_enabled)
        self.layer_text_check.setEnabled(template_enabled)
        self.shader_controls.setEnabled(template_enabled)
        self.layer_cutout_check.setVisible(template_enabled)
        self.layer_text_check.setVisible(template_enabled)
        self._refresh_seed_color_controls()
        self._refresh_manual_mark_count_label()
        self._update_roi_label()
        self._update_manual_history_buttons()

    def selected_bg_removal_engine(self) -> str:
        if not self._template_enabled():
            return "none"
        return str(self.bg_removal_combo.currentData() or "none")

    def selected_bg_removal_params(self) -> dict[str, object]:
        return normalize_background_removal_params(
            {
                "alpha_matting": self.alpha_matting_check.isChecked(),
                "alpha_matting_foreground_threshold": int(self.fg_threshold_spin.value()),
                "alpha_matting_background_threshold": int(self.bg_threshold_spin.value()),
                "alpha_matting_erode_size": int(self.erode_spin.value()),
                "alpha_edge_feather": int(self.edge_feather_spin.value()),
                "post_process_mask": self.post_process_check.isChecked(),
            }
        )

    def selected_text_preserve_config(self) -> dict[str, object]:
        method = self.selected_text_extraction_method()
        manual_add_seeds = [[float(x), float(y)] for x, y in self._manual_add_points]
        manual_remove_seeds = [[float(x), float(y)] for x, y in self._manual_remove_points]
        if method == "none":
            manual_add_seeds = []
            manual_remove_seeds = []
        seed_colors = [
            [int(color[0]), int(color[1]), int(color[2])]
            for color in self._seed_colors
            if color is not None
        ]
        return text_preserve_to_dict(
            {
                "enabled": method != "none",
                "method": method,
                "strength": int(self.preserve_text_strength.value()),
                "feather": int(self.preserve_text_feather.value()),
                "color_groups": int(self.preserve_text_groups.value()),
                "seed_colors": seed_colors,
                "seed_tolerance": int(self.preserve_text_seed_tolerance.value()),
                "manual_add_seeds": manual_add_seeds,
                "manual_remove_seeds": manual_remove_seeds,
                "include_outline": self.preserve_text_outline.isChecked(),
                "include_shadow": self.preserve_text_shadow.isChecked(),
                "glow_mode": str(self.preserve_text_glow_mode.currentData() or "disabled"),
                "glow_radius": int(self.preserve_text_glow_radius.value()),
                "glow_strength": int(self.preserve_text_glow_strength.value()),
                "roi": self._text_roi,
            }
        )

    def selected_text_extraction_method(self) -> str:
        if not self._template_enabled():
            return "none"
        return str(self.text_method_combo.currentData() or "none")

    def selected_border_shader(self) -> dict[str, object]:
        return self._canvas.border_shader()


class IconConverterDialog(QDialog):
    def __init__(
        self,
        initial_icon_style: str = "none",
        icon_style_saver: Callable[[str], None] | None = None,
        initial_bg_removal_engine: str = "none",
        bg_removal_engine_saver: Callable[[str], None] | None = None,
        initial_border_shader: dict[str, object] | None = None,
        border_shader_saver: Callable[[dict[str, object]], None] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Image to Icon Converter")
        self._prepared_image_bytes: bytes | None = None
        self._prepared_is_final_composite = False
        self._icon_style_saver = icon_style_saver
        self._bg_removal_engine_saver = bg_removal_engine_saver
        self._border_shader_saver = border_shader_saver
        self._border_shader = border_shader_to_dict(initial_border_shader)
        self._bg_removal_params = normalize_background_removal_params(
            dict(DEFAULT_BG_REMOVAL_PARAMS)
        )
        self._text_preserve_config = text_preserve_to_dict(None)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Convert any image (PNG/JPEG/WebP/BMP) to a multi-size Windows ICO "
                "using the same icon pipeline."
            )
        )

        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Source image:", self))
        self.source_edit = QLineEdit(self)
        self.source_edit.setPlaceholderText("Choose an image file...")
        source_row.addWidget(self.source_edit, 1)
        self.source_browse_btn = QPushButton("Browse...", self)
        self.source_browse_btn.clicked.connect(self._on_browse_source)
        source_row.addWidget(self.source_browse_btn)
        layout.addLayout(source_row)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output ICO:", self))
        self.output_edit = QLineEdit(self)
        self.output_edit.setPlaceholderText("Choose output .ico path...")
        output_row.addWidget(self.output_edit, 1)
        self.output_browse_btn = QPushButton("Save As...", self)
        self.output_browse_btn.clicked.connect(self._on_browse_output)
        output_row.addWidget(self.output_browse_btn)
        layout.addLayout(output_row)

        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Icon type:", self))
        self.icon_style_combo = QComboBox(self)
        for label, value in icon_style_options():
            self.icon_style_combo.addItem(label, value)
        default_style = normalize_icon_style(initial_icon_style, circular_ring=False)
        idx = self.icon_style_combo.findData(default_style)
        if idx >= 0:
            self.icon_style_combo.setCurrentIndex(idx)
        self.icon_style_combo.currentIndexChanged.connect(self._on_icon_style_changed)
        style_row.addWidget(self.icon_style_combo)
        self.border_shader_btn = QPushButton("", self)
        self.border_shader_btn.setToolTip("Border Shader Controls")
        self.border_shader_btn.clicked.connect(self._on_open_border_shader)
        style_row.addWidget(self.border_shader_btn)
        style_row.addWidget(QLabel("Background cutout:", self))
        self.bg_removal_combo = QComboBox(self)
        for label, value in BACKGROUND_REMOVAL_OPTIONS:
            self.bg_removal_combo.addItem(label, value)
        default_bg = normalize_background_removal_engine(initial_bg_removal_engine)
        bg_idx = self.bg_removal_combo.findData(default_bg)
        if bg_idx >= 0:
            self.bg_removal_combo.setCurrentIndex(bg_idx)
        self.bg_removal_combo.currentIndexChanged.connect(self._on_bg_engine_changed)
        style_row.addWidget(self.bg_removal_combo)
        style_row.addWidget(QLabel("Text:", self))
        self.text_extract_combo = QComboBox(self)
        for label, value in TEXT_EXTRACTION_METHOD_OPTIONS:
            self.text_extract_combo.addItem(label, value)
        text_idx = self.text_extract_combo.findData(
            normalize_text_extraction_method(
                str(self._text_preserve_config.get("method", "") or ""),
                enabled_fallback=bool(self._text_preserve_config.get("enabled", False)),
            )
        )
        if text_idx >= 0:
            self.text_extract_combo.setCurrentIndex(text_idx)
        self.text_extract_combo.currentIndexChanged.connect(self._on_text_extract_method_changed)
        style_row.addWidget(self.text_extract_combo)
        style_row.addStretch(1)
        self.adjust_btn = QPushButton("Adjust Framing...", self)
        self.adjust_btn.clicked.connect(self._on_adjust_framing)
        style_row.addWidget(self.adjust_btn)
        layout.addLayout(style_row)
        self._refresh_border_shader_button()

        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(self)
        self.convert_btn = QPushButton("Convert")
        self.close_btn = QPushButton("Close")
        buttons.addButton(self.convert_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.convert_btn.clicked.connect(self._on_convert)
        self.close_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(900, 240)
        self._sync_template_dependents()

    def _current_icon_style(self) -> str:
        return str(self.icon_style_combo.currentData() or "none")

    def _template_enabled(self) -> bool:
        return self._current_icon_style() != "none"

    def _sync_template_dependents(self) -> None:
        template_enabled = self._template_enabled()
        if not template_enabled:
            bg_idx = self.bg_removal_combo.findData("none")
            if bg_idx >= 0 and self.bg_removal_combo.currentIndex() != bg_idx:
                blocked = self.bg_removal_combo.blockSignals(True)
                self.bg_removal_combo.setCurrentIndex(bg_idx)
                self.bg_removal_combo.blockSignals(blocked)
            text_idx = self.text_extract_combo.findData("none")
            if text_idx >= 0 and self.text_extract_combo.currentIndex() != text_idx:
                blocked = self.text_extract_combo.blockSignals(True)
                self.text_extract_combo.setCurrentIndex(text_idx)
                self.text_extract_combo.blockSignals(blocked)
            cfg = dict(self._text_preserve_config)
            cfg.update({"enabled": False, "method": "none"})
            self._text_preserve_config = text_preserve_to_dict(cfg)
        self.bg_removal_combo.setEnabled(template_enabled)
        self.text_extract_combo.setEnabled(template_enabled)
        self.border_shader_btn.setEnabled(template_enabled)

    def _current_bg_removal_engine(self) -> str:
        if not self._template_enabled():
            return "none"
        return str(self.bg_removal_combo.currentData() or "none")

    def _border_shader_config(self) -> dict[str, object]:
        return dict(self._border_shader)

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text.strip())

    def _autofill_output_path(self, source_path: str) -> None:
        if not source_path:
            return
        source = Path(source_path)
        default_ico = source.with_suffix(".ico")
        current = self.output_edit.text().strip()
        if not current:
            self.output_edit.setText(str(default_ico))

    def _on_browse_source(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Image",
            "",
            "Images (*.png *.jpg *.jpe *.jpeg *.jfif *.avif *.webp *.bmp);;All Files (*)",
        )
        if not selected:
            return
        self.source_edit.setText(selected)
        self._prepared_image_bytes = None
        self._prepared_is_final_composite = False
        self._autofill_output_path(selected)
        self._set_status("")

    def _on_browse_output(self) -> None:
        start = self.output_edit.text().strip()
        if not start:
            source = self.source_edit.text().strip()
            if source:
                start = str(Path(source).with_suffix(".ico"))
        selected, _ = QFileDialog.getSaveFileName(
            self,
            "Save Icon As",
            start,
            "Icon Files (*.ico)",
        )
        if not selected:
            return
        path = Path(selected)
        if path.suffix.lower() != ".ico":
            path = path.with_suffix(".ico")
        self.output_edit.setText(str(path))

    def _on_icon_style_changed(self, *_args) -> None:
        self._sync_template_dependents()
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
            self._set_status("Settings changed after framing. Re-run Adjust Framing.")
        if self._icon_style_saver is not None:
            try:
                self._icon_style_saver(self._current_icon_style())
            except Exception:
                pass

    def _on_bg_engine_changed(self, *_args) -> None:
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
            self._set_status("Settings changed after framing. Re-run Adjust Framing.")
        if self._bg_removal_engine_saver is not None:
            try:
                self._bg_removal_engine_saver(self._current_bg_removal_engine())
            except Exception:
                pass

    def _on_text_extract_method_changed(self, _index: int) -> None:
        if not self._template_enabled():
            method = "none"
        else:
            method = str(self.text_extract_combo.currentData() or "none")
        cfg = dict(self._text_preserve_config)
        cfg.update({"enabled": method != "none", "method": method})
        self._text_preserve_config = text_preserve_to_dict(cfg)
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
            self._set_status("Settings changed after framing. Re-run Adjust Framing.")

    def _refresh_border_shader_button(self) -> None:
        cfg = normalize_border_shader_config(self._border_shader)
        color = QColor()
        if cfg.mode == "hsl":
            color.setHsl(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        else:
            color.setHsv(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        self.border_shader_btn.setStyleSheet(
            _shader_swatch_css((color.red(), color.green(), color.blue()))
        )

    def _on_open_border_shader(self) -> None:
        dialog = BorderShaderDialog(
            icon_style=self._current_icon_style(),
            initial_config=self._border_shader,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._border_shader = dialog.result_config()
        self._refresh_border_shader_button()
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
            self._set_status("Settings changed after framing. Re-run Adjust Framing.")
        if self._border_shader_saver is not None:
            try:
                self._border_shader_saver(dict(self._border_shader))
            except Exception:
                pass

    def _load_source_bytes(self) -> bytes | None:
        source_path = self.source_edit.text().strip()
        if not source_path:
            QMessageBox.warning(self, "Missing Source", "Select a source image first.")
            return None
        source = Path(source_path)
        if not source.exists() or not source.is_file():
            QMessageBox.warning(self, "Missing Source", f"Image not found:\n{source_path}")
            return None
        try:
            return source.read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "Read Failed", f"Could not read image:\n{exc}")
            return None

    def _on_adjust_framing(self) -> None:
        image_bytes = self._load_source_bytes()
        if image_bytes is None:
            return
        dialog = IconFramingDialog(
            image_bytes,
            border_style=self._current_icon_style(),
            initial_bg_removal_engine=self._current_bg_removal_engine(),
            initial_bg_removal_params=dict(self._bg_removal_params),
            initial_text_preserve_config=dict(self._text_preserve_config),
            border_shader=self._border_shader,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        style_idx = self.icon_style_combo.findData(dialog.selected_style())
        if style_idx >= 0:
            self.icon_style_combo.setCurrentIndex(style_idx)
        bg_idx = self.bg_removal_combo.findData(dialog.selected_bg_removal_engine())
        if bg_idx >= 0:
            self.bg_removal_combo.setCurrentIndex(bg_idx)
        self._bg_removal_params = dialog.selected_bg_removal_params()
        self._text_preserve_config = dialog.selected_text_preserve_config()
        text_idx = self.text_extract_combo.findData(
            str(self._text_preserve_config.get("method", "none") or "none")
        )
        if text_idx >= 0:
            self.text_extract_combo.setCurrentIndex(text_idx)
        self._border_shader = dialog.selected_border_shader()
        self._refresh_border_shader_button()
        if self._border_shader_saver is not None:
            try:
                self._border_shader_saver(dict(self._border_shader))
            except Exception:
                pass
        framed = dialog.framed_image_bytes()
        if not framed:
            return
        self._prepared_image_bytes = framed
        self._prepared_is_final_composite = dialog.framed_image_is_final_composite()
        self._set_status(
            "Framing applied. Conversion will reuse cached composited layers (no recompute)."
        )

    def _on_convert(self) -> None:
        output_path_raw = self.output_edit.text().strip()
        if not output_path_raw:
            QMessageBox.warning(self, "Missing Output", "Choose an output .ico file.")
            return
        output_path = Path(output_path_raw)
        if output_path.suffix.lower() != ".ico":
            output_path = output_path.with_suffix(".ico")
            self.output_edit.setText(str(output_path))

        source_bytes = self._prepared_image_bytes
        if source_bytes is None:
            source_bytes = self._load_source_bytes()
            if source_bytes is None:
                return

        try:
            if self._prepared_is_final_composite and self._prepared_image_bytes is not None:
                ico_payload = build_multi_size_ico(source_bytes)
            else:
                ico_payload = build_multi_size_ico(
                    source_bytes,
                    icon_style=self._current_icon_style(),
                    bg_removal_engine=self._current_bg_removal_engine(),
                    bg_removal_params=self._bg_removal_params,
                    text_preserve_config=self._text_preserve_config,
                    border_shader=self._border_shader_config(),
                )
        except Exception as exc:
            QMessageBox.warning(self, "Conversion Failed", f"Could not build icon:\n{exc}")
            return

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(ico_payload)
        except OSError as exc:
            QMessageBox.warning(self, "Write Failed", f"Could not save icon:\n{exc}")
            return

        self._set_status(f"Saved: {output_path}")
        QMessageBox.information(self, "Conversion Complete", f"Icon saved:\n{output_path}")


class SGDBResourcePriorityDialog(QDialog):
    def __init__(
        self,
        resource_order: list[str],
        enabled_resources: set[str],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("SteamGridDB Resource Priority")
        self._resource_labels = {value: label for label, value in SGDB_RESOURCE_OPTIONS}
        self._default_order = [value for _, value in SGDB_RESOURCE_OPTIONS]
        initial_order = [value for value in resource_order if value in self._resource_labels]
        for value in self._default_order:
            if value not in initial_order:
                initial_order.append(value)
        initial_enabled = {
            value for value in enabled_resources if value in self._resource_labels
        }
        if not initial_enabled:
            initial_enabled = {"icons", "logos"}

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Drag and drop to reorder request priority. "
                "Check items to enable them for search."
            )
        )
        self.list_widget = QListWidget(self)
        self.list_widget.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        for value in initial_order:
            item = QListWidgetItem(self._resource_labels[value], self.list_widget)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            item.setCheckState(
                Qt.CheckState.Checked
                if value in initial_enabled
                else Qt.CheckState.Unchecked
            )
        layout.addWidget(self.list_widget)

        actions = QHBoxLayout()
        self.reset_btn = QPushButton("Reset Defaults", self)
        self.reset_btn.clicked.connect(self._on_reset_defaults)
        actions.addWidget(self.reset_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_then_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(440, 420)

    def _on_reset_defaults(self) -> None:
        self.list_widget.clear()
        for label, value in SGDB_RESOURCE_OPTIONS:
            item = QListWidgetItem(label, self.list_widget)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(
                item.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsDragEnabled
                | Qt.ItemFlag.ItemIsDropEnabled
            )
            default_checked = value in {"icons", "logos"}
            item.setCheckState(
                Qt.CheckState.Checked
                if default_checked
                else Qt.CheckState.Unchecked
            )

    def _validate_then_accept(self) -> None:
        if not self.enabled_resources():
            QMessageBox.warning(
                self,
                "No Resource Enabled",
                "Enable at least one resource type.",
            )
            return
        self.accept()

    def ordered_resources(self) -> list[str]:
        ordered: list[str] = []
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item is None:
                continue
            value = str(item.data(Qt.ItemDataRole.UserRole) or "").strip().casefold()
            if not value or value in ordered:
                continue
            ordered.append(value)
        return ordered

    def enabled_resources(self) -> set[str]:
        enabled: set[str] = set()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            value = str(item.data(Qt.ItemDataRole.UserRole) or "").strip().casefold()
            if value:
                enabled.add(value)
        return enabled


class IconPickerDialog(QDialog):
    def __init__(
        self,
        folder_name: str,
        candidates: list[IconCandidate],
        preview_loader,
        image_loader,
        search_callback=None,
        initial_resource_order: list[str] | None = None,
        initial_enabled_resources: set[str] | None = None,
        resource_prefs_saver=None,
        show_cancel_all: bool = False,
        initial_icon_style: str = "none",
        icon_style_saver=None,
        initial_bg_removal_engine: str = "none",
        bg_removal_engine_saver=None,
        initial_border_shader: dict[str, object] | None = None,
        border_shader_saver=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        _cleanup_web_capture_session()
        self.setWindowTitle(f"Assign Folder Icon - {folder_name}")
        self.folder_name = folder_name
        self.candidates = candidates
        self._preview_loader = preview_loader
        self._image_loader = image_loader
        self._search_callback = search_callback
        self._resource_prefs_saver = resource_prefs_saver
        self._icon_style_saver = icon_style_saver
        self._bg_removal_engine_saver = bg_removal_engine_saver
        self._border_shader_saver = border_shader_saver
        self.cancel_all_requested = False
        self._web_capture_dialog: WebDownloadCaptureDialog | None = None
        self._local_image_path: str | None = None
        self._source_image_bytes: bytes | None = None
        self._prepared_image_bytes: bytes | None = None
        self._prepared_is_final_composite = False
        self._local_source_label = "Local File"
        self._local_source_row: int | None = None
        self._preview_pix_cache: dict[tuple[int, int, str], QPixmap] = {}
        self._hover_row: int | None = None
        self._border_shader = border_shader_to_dict(initial_border_shader)
        self._bg_removal_params = normalize_background_removal_params(
            dict(DEFAULT_BG_REMOVAL_PARAMS)
        )
        self._text_preserve_config = text_preserve_to_dict(None)
        default_order = [value for _, value in SGDB_RESOURCE_OPTIONS]
        self._resource_labels = {value: label for label, value in SGDB_RESOURCE_OPTIONS}
        self._resource_order = [
            value
            for value in (initial_resource_order or default_order)
            if value in self._resource_labels
        ]
        for value in default_order:
            if value not in self._resource_order:
                self._resource_order.append(value)
        initial_enabled = {
            value
            for value in (initial_enabled_resources or {"icons", "logos"})
            if value in self._resource_labels
        }
        if not initial_enabled:
            initial_enabled = {"icons", "logos"}
        self._enabled_resources = initial_enabled
        self._last_requested_resources = self._current_requested_resources()
        self._hover_popup = QLabel(
            None,
            Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint,
        )
        self._hover_popup.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._hover_popup.setFrameStyle(QFrame.Shape.Panel | QFrame.Shadow.Plain)
        self._hover_popup.setLineWidth(1)
        self._hover_popup.setStyleSheet("background-color: #1c1c1c; padding: 4px;")

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Pick one candidate icon, or choose a local image. "
                "Template is disabled by default."
            )
        )

        resources_row = QHBoxLayout()
        resources_row.addWidget(QLabel("Resources:", self))
        self.resource_summary = QLabel(self)
        resources_row.addWidget(self.resource_summary, 1)
        self.resource_priority_btn = QPushButton("Priority...", self)
        self.resource_priority_btn.clicked.connect(self._on_manage_resource_priority)
        resources_row.addWidget(self.resource_priority_btn)
        self.refresh_candidates_btn = QPushButton("Refresh Results", self)
        self.refresh_candidates_btn.clicked.connect(self._on_refresh_candidates)
        resources_row.addWidget(self.refresh_candidates_btn)
        layout.addLayout(resources_row)

        self.table = QTableWidget(len(candidates), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Preview", "Title", "Provider", "Size", "Source"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setIconSize(QSize(64, 64))
        self.table.setMouseTracking(True)
        self.table.viewport().setMouseTracking(True)
        self.table.viewport().installEventFilter(self)
        self._rebuild_candidates_table(select_first_row=True)
        self.table.setColumnWidth(0, 84)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._on_source_selection_changed)
        layout.addWidget(self.table)

        options_row = QHBoxLayout()
        options_row.addWidget(QLabel("Icon Type:", self))
        self.icon_style_combo = QComboBox(self)
        for label, value in icon_style_options():
            self.icon_style_combo.addItem(label, value)
        default_style = normalize_icon_style(initial_icon_style, circular_ring=False)
        default_idx = self.icon_style_combo.findData(default_style)
        if default_idx >= 0:
            self.icon_style_combo.setCurrentIndex(default_idx)
        self.icon_style_combo.currentIndexChanged.connect(self._on_icon_style_changed)
        options_row.addWidget(self.icon_style_combo)
        self.border_shader_btn = QPushButton("", self)
        self.border_shader_btn.setToolTip("Border Shader Controls")
        self.border_shader_btn.clicked.connect(self._on_open_border_shader)
        options_row.addWidget(self.border_shader_btn)
        options_row.addWidget(QLabel("Cutout:", self))
        self.bg_removal_combo = QComboBox(self)
        for label, value in BACKGROUND_REMOVAL_OPTIONS:
            self.bg_removal_combo.addItem(label, value)
        default_bg = normalize_background_removal_engine(initial_bg_removal_engine)
        bg_idx = self.bg_removal_combo.findData(default_bg)
        if bg_idx >= 0:
            self.bg_removal_combo.setCurrentIndex(bg_idx)
        self.bg_removal_combo.currentIndexChanged.connect(self._on_bg_engine_changed)
        options_row.addWidget(self.bg_removal_combo)
        options_row.addWidget(QLabel("Text:", self))
        self.text_extract_combo = QComboBox(self)
        for label, value in TEXT_EXTRACTION_METHOD_OPTIONS:
            self.text_extract_combo.addItem(label, value)
        text_idx = self.text_extract_combo.findData(
            normalize_text_extraction_method(
                str(self._text_preserve_config.get("method", "") or ""),
                enabled_fallback=bool(self._text_preserve_config.get("enabled", False)),
            )
        )
        if text_idx >= 0:
            self.text_extract_combo.setCurrentIndex(text_idx)
        self.text_extract_combo.currentIndexChanged.connect(self._on_text_extract_method_changed)
        options_row.addWidget(self.text_extract_combo)
        options_row.addStretch(1)
        self.local_btn = QPushButton("Use Local Image...")
        self.local_btn.clicked.connect(self._on_pick_local)
        options_row.addWidget(self.local_btn)
        self.web_btn = QPushButton("Web Capture...")
        self.web_btn.clicked.connect(self._on_pick_web_capture)
        options_row.addWidget(self.web_btn)
        self.frame_btn = QPushButton("Adjust Framing...")
        self.frame_btn.clicked.connect(self._on_adjust_framing)
        options_row.addWidget(self.frame_btn)
        layout.addLayout(options_row)

        layout.addWidget(QLabel("InfoTip (optional):"))
        self.info_tip_edit = QPlainTextEdit(self)
        self.info_tip_edit.setPlaceholderText("Optional folder tooltip text")
        self.info_tip_edit.setFixedHeight(90)
        layout.addWidget(self.info_tip_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_then_accept)
        buttons.rejected.connect(self.reject)
        if show_cancel_all:
            self.cancel_all_btn = QPushButton("Cancel All", self)
            self.cancel_all_btn.clicked.connect(self._on_cancel_all)
            buttons.addButton(self.cancel_all_btn, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(buttons)
        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)
        self._refresh_border_shader_button()
        self._sync_template_dependents()
        self._update_resource_summary()
        self._update_refresh_candidates_button()
        if self._search_callback is None:
            self.resource_priority_btn.setEnabled(False)
            self.refresh_candidates_btn.setEnabled(False)
            self.refresh_candidates_btn.setToolTip("Refreshing sources is unavailable.")

    def _on_cancel_all(self) -> None:
        self.cancel_all_requested = True
        self.reject()

    def _rebuild_candidates_table(self, select_first_row: bool = False) -> None:
        selected_row = self.table.currentRow()
        self._hide_hover_preview()
        self._preview_pix_cache.clear()
        self.table.clearContents()
        self.table.setRowCount(len(self.candidates))
        for row, candidate in enumerate(self.candidates):
            preview_item = QTableWidgetItem("")
            preview_item.setIcon(self._preview_icon(row, 64))
            self.table.setItem(row, 0, preview_item)
            self.table.setItem(row, 1, QTableWidgetItem(candidate.title))
            self.table.setItem(row, 2, QTableWidgetItem(candidate.provider))
            self.table.setItem(
                row, 3, QTableWidgetItem(f"{candidate.width}x{candidate.height}")
            )
            source_item = QTableWidgetItem(candidate.source_url)
            source_item.setToolTip(candidate.source_url)
            self.table.setItem(row, 4, source_item)
            self.table.setRowHeight(row, 72)
        self._local_source_row = None
        if self._source_image_bytes:
            local_path = self._local_image_path or ""
            self._upsert_local_source_row(
                local_path,
                self._source_image_bytes,
                self._local_source_label,
            )
        self.table.setColumnWidth(0, 84)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        target_row = selected_row
        if select_first_row and self.table.rowCount() > 0:
            target_row = 0
        if target_row < 0 and self.table.rowCount() > 0:
            target_row = 0
        if 0 <= target_row < self.table.rowCount():
            blocked = self.table.blockSignals(True)
            self.table.clearSelection()
            self.table.selectRow(target_row)
            self.table.blockSignals(blocked)

    def _current_requested_resources(self) -> list[str]:
        return [
            value for value in self._resource_order if value in self._enabled_resources
        ]

    def _update_resource_summary(self) -> None:
        enabled = self._current_requested_resources()
        if enabled:
            labels = [self._resource_labels.get(value, value.title()) for value in enabled]
            summary = " > ".join(labels)
        else:
            summary = "(none enabled)"
        self.resource_summary.setText(summary)

    def _update_refresh_candidates_button(self) -> None:
        changed = self._current_requested_resources() != self._last_requested_resources
        if changed:
            self.refresh_candidates_btn.setStyleSheet(
                "QPushButton { background-color: #6b1d1d; color: #ffffff; font-weight: 600; }"
            )
            self.refresh_candidates_btn.setToolTip(
                "Resource selection changed. Refresh to request updated results."
            )
            return
        self.refresh_candidates_btn.setStyleSheet("")
        self.refresh_candidates_btn.setToolTip("Refresh search with selected resources.")

    def _on_manage_resource_priority(self) -> None:
        dialog = SGDBResourcePriorityDialog(
            resource_order=self._resource_order,
            enabled_resources=self._enabled_resources,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._resource_order = dialog.ordered_resources()
        self._enabled_resources = dialog.enabled_resources()
        self._update_resource_summary()
        if self._resource_prefs_saver is not None:
            try:
                saved = self._resource_prefs_saver(
                    list(self._resource_order), set(self._enabled_resources)
                )
                if isinstance(saved, tuple) and len(saved) == 2:
                    saved_order, saved_enabled = saved
                    if isinstance(saved_order, list):
                        self._resource_order = [
                            value
                            for value in saved_order
                            if value in self._resource_labels
                        ]
                    if isinstance(saved_enabled, set):
                        self._enabled_resources = {
                            value
                            for value in saved_enabled
                            if value in self._resource_labels
                        }
            except Exception as exc:
                QMessageBox.warning(
                    self,
                    "Save Preferences Failed",
                    f"Could not persist resource preferences:\n{exc}",
                )
        self._update_refresh_candidates_button()

    def _on_refresh_candidates(self) -> None:
        if self._search_callback is None:
            return
        resources = self._current_requested_resources()
        if not resources:
            QMessageBox.warning(
                self,
                "No Resources Selected",
                "Select at least one resource type before refreshing.",
            )
            return
        try:
            refreshed = self._search_callback(resources)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Refresh Failed",
                f"Could not refresh icon candidates:\n{exc}",
            )
            return
        self.candidates = refreshed
        self._last_requested_resources = list(resources)
        self._rebuild_candidates_table(select_first_row=True)
        self._update_resource_summary()
        self._update_refresh_candidates_button()
        if not refreshed:
            QMessageBox.information(
                self,
                "No Results",
                "No candidates found for the selected resource types.",
            )

    def eventFilter(self, watched, event):  # type: ignore[override]
        if watched is self.table.viewport():
            if event.type() == QEvent.Type.MouseMove:
                index = self.table.indexAt(event.pos())
                if index.isValid() and index.column() == 0:
                    cell_rect = self.table.visualRect(index)
                    icon_hit = cell_rect.adjusted(0, 0, -(cell_rect.width() - 74), 0)
                    if icon_hit.contains(event.pos()):
                        self._show_hover_preview(
                            index.row(),
                            self.table.viewport().mapToGlobal(event.pos()),
                        )
                        return False
                self._hide_hover_preview()
            elif event.type() in (
                QEvent.Type.Leave,
                QEvent.Type.MouseButtonPress,
                QEvent.Type.Wheel,
            ):
                self._hide_hover_preview()
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._hide_hover_preview()
        self._close_web_capture_dialog_async()
        _cleanup_web_capture_session()
        super().closeEvent(event)

    def _close_web_capture_dialog_async(self) -> None:
        dialog = self._web_capture_dialog
        if dialog is None:
            return
        self._web_capture_dialog = None
        try:
            dialog.shutdown_session()
        except Exception:
            try:
                dialog.close()
            except Exception:
                pass

    def _current_icon_style(self) -> str:
        if not hasattr(self, "icon_style_combo"):
            return "none"
        return str(self.icon_style_combo.currentData() or "none")

    def _template_enabled(self) -> bool:
        return self._current_icon_style() != "none"

    def _sync_template_dependents(self) -> None:
        template_enabled = self._template_enabled()
        if not template_enabled:
            bg_idx = self.bg_removal_combo.findData("none")
            if bg_idx >= 0 and self.bg_removal_combo.currentIndex() != bg_idx:
                blocked = self.bg_removal_combo.blockSignals(True)
                self.bg_removal_combo.setCurrentIndex(bg_idx)
                self.bg_removal_combo.blockSignals(blocked)
            text_idx = self.text_extract_combo.findData("none")
            if text_idx >= 0 and self.text_extract_combo.currentIndex() != text_idx:
                blocked = self.text_extract_combo.blockSignals(True)
                self.text_extract_combo.setCurrentIndex(text_idx)
                self.text_extract_combo.blockSignals(blocked)
            cfg = dict(self._text_preserve_config)
            cfg.update({"enabled": False, "method": "none"})
            self._text_preserve_config = text_preserve_to_dict(cfg)
        self.bg_removal_combo.setEnabled(template_enabled)
        self.text_extract_combo.setEnabled(template_enabled)
        self.border_shader_btn.setEnabled(template_enabled)

    def _current_bg_removal_engine(self) -> str:
        if not hasattr(self, "bg_removal_combo"):
            return "none"
        if not self._template_enabled():
            return "none"
        return str(self.bg_removal_combo.currentData() or "none")

    def _is_heavy_bg_engine(self) -> bool:
        return self._current_bg_removal_engine() in {"rembg", "bria_rmbg"}

    def _preview_bg_engine(self) -> str:
        # Keep UI responsive: heavy cutout engines are only applied at final icon build time.
        if self._is_heavy_bg_engine():
            return "none"
        return self._current_bg_removal_engine()

    def _border_shader_config(self) -> dict[str, object]:
        return dict(self._border_shader)

    def _on_icon_style_changed(self, *_args) -> None:
        self._sync_template_dependents()
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._refresh_preview_icons()
        if self._icon_style_saver is not None:
            try:
                self._icon_style_saver(self._current_icon_style())
            except Exception:
                pass

    def _on_bg_engine_changed(self, *_args) -> None:
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._preview_pix_cache.clear()
        self._refresh_preview_icons()
        if self._bg_removal_engine_saver is not None:
            try:
                self._bg_removal_engine_saver(self._current_bg_removal_engine())
            except Exception:
                pass

    def _on_text_extract_method_changed(self, _index: int) -> None:
        if not self._template_enabled():
            method = "none"
        else:
            method = str(self.text_extract_combo.currentData() or "none")
        cfg = dict(self._text_preserve_config)
        cfg.update({"enabled": method != "none", "method": method})
        self._text_preserve_config = text_preserve_to_dict(cfg)
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._preview_pix_cache.clear()
        self._refresh_preview_icons()

    def _refresh_border_shader_button(self) -> None:
        cfg = normalize_border_shader_config(self._border_shader)
        color = QColor()
        if cfg.mode == "hsl":
            color.setHsl(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        else:
            color.setHsv(
                cfg.hue,
                int(round(cfg.saturation * 255 / 100)),
                int(round(cfg.tone * 255 / 100)),
            )
        self.border_shader_btn.setStyleSheet(
            _shader_swatch_css((color.red(), color.green(), color.blue()))
        )

    def _on_open_border_shader(self) -> None:
        dialog = BorderShaderDialog(
            icon_style=self._current_icon_style(),
            initial_config=self._border_shader,
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._border_shader = dialog.result_config()
        self._refresh_border_shader_button()
        if self._prepared_is_final_composite:
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False
        self._preview_pix_cache.clear()
        self._refresh_preview_icons()
        if self._border_shader_saver is not None:
            try:
                self._border_shader_saver(dict(self._border_shader))
            except Exception:
                pass

    def _preview_icon(self, row: int, size: int) -> QIcon:
        pix = self._preview_pixmap(row, size)
        if pix is None or pix.isNull():
            return QIcon()
        return QIcon(pix)

    def _preview_pixmap(self, row: int, size: int) -> QPixmap | None:
        if row < 0:
            return None
        if self._local_source_row is not None and row == self._local_source_row:
            return self._styled_local_preview_pixmap(size)
        if row >= len(self.candidates):
            return None
        icon_style = self._current_icon_style()
        bg_engine = self._preview_bg_engine()
        shader_key = json.dumps(self._border_shader_config(), sort_keys=True)
        cache_key = (row, size, f"{icon_style}|{bg_engine}|{shader_key}")
        cached = self._preview_pix_cache.get(cache_key)
        if cached is not None:
            return cached
        candidate = self.candidates[row]
        try:
            preview_png = self._preview_loader(
                candidate,
                icon_style,
                size,
                bg_engine,
                self._border_shader_config(),
            )
            pix = QPixmap()
            if not pix.loadFromData(preview_png):
                return None
            composed = composite_on_checkerboard(
                pix,
                width=size,
                height=size,
                keep_aspect=True,
            )
            self._preview_pix_cache[cache_key] = composed
            return composed
        except Exception:
            return None

    def _styled_local_preview_pixmap(self, size: int) -> QPixmap | None:
        local_bytes = self._prepared_image_bytes or self._source_image_bytes
        if not local_bytes:
            return None
        if self._prepared_is_final_composite and self._prepared_image_bytes:
            pix = QPixmap()
            if not pix.loadFromData(self._prepared_image_bytes):
                return None
            return composite_on_checkerboard(
                pix,
                width=size,
                height=size,
                keep_aspect=True,
            )
        try:
            preview_png = build_preview_png(
                local_bytes,
                size=size,
                icon_style=self._current_icon_style(),
                bg_removal_engine=self._preview_bg_engine(),
                text_preserve_config=self._text_preserve_config,
                border_shader=self._border_shader_config(),
            )
            pix = QPixmap()
            if pix.loadFromData(preview_png):
                return composite_on_checkerboard(
                    pix,
                    width=size,
                    height=size,
                    keep_aspect=True,
                )
        except Exception:
            pass
        pix = QPixmap()
        if not pix.loadFromData(local_bytes):
            return None
        return composite_on_checkerboard(
            pix,
            width=size,
            height=size,
            keep_aspect=True,
        )

    def _refresh_preview_icon_row(self, row: int) -> None:
        if row < 0 or row >= self.table.rowCount():
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        item.setIcon(self._preview_icon(row, 64))

    def _refresh_preview_icons(self, *, lazy: bool = False) -> None:
        self._hide_hover_preview()
        if lazy:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is None:
                    continue
                item.setIcon(QIcon())
            current = self.table.currentRow()
            if current >= 0:
                self._refresh_preview_icon_row(current)
            elif self.table.rowCount() > 0:
                self._refresh_preview_icon_row(0)
            if self._local_source_row is not None:
                self._refresh_preview_icon_row(self._local_source_row)
            return
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is None:
                continue
            item.setIcon(self._preview_icon(row, 64))

    def _upsert_local_source_row(self, path: str, bytes_data: bytes, source_label: str) -> None:
        if self._local_source_row is None:
            self._local_source_row = self.table.rowCount()
            self.table.insertRow(self._local_source_row)
            self.table.setRowHeight(self._local_source_row, 72)

        row = self._local_source_row
        pix = self._styled_local_preview_pixmap(64) or QPixmap()
        preview_item = QTableWidgetItem("")
        if not pix.isNull():
            preview_item.setIcon(QIcon(pix))
        self.table.setItem(row, 0, preview_item)
        self.table.setItem(row, 1, QTableWidgetItem("Selected File"))
        self.table.setItem(row, 2, QTableWidgetItem(source_label))
        if pix.isNull():
            size_text = "unknown"
        else:
            size_text = f"{pix.width()}x{pix.height()}"
        self.table.setItem(row, 3, QTableWidgetItem(size_text))
        source_item = QTableWidgetItem(path)
        source_item.setToolTip(path)
        self.table.setItem(row, 4, source_item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

        was_blocked = self.table.blockSignals(True)
        self.table.clearSelection()
        self.table.selectRow(row)
        self.table.blockSignals(was_blocked)
        self.table.scrollToItem(self.table.item(row, 0))

    def _show_hover_preview(self, row: int, global_pos: QPoint) -> None:
        pix = self._preview_pixmap(row, 256)
        if pix is None or pix.isNull():
            self._hide_hover_preview()
            return
        if self._hover_row != row:
            self._hover_popup.setPixmap(pix)
            self._hover_popup.adjustSize()
            self._hover_row = row
        self._position_hover_popup(global_pos)
        self._hover_popup.show()

    def _position_hover_popup(self, global_pos: QPoint) -> None:
        popup_size = self._hover_popup.sizeHint()
        x = global_pos.x() + 20
        y = global_pos.y() + 20
        screen = QApplication.primaryScreen()
        if screen is not None:
            rect = screen.availableGeometry()
            x = min(max(rect.left(), x), max(rect.left(), rect.right() - popup_size.width()))
            y = min(max(rect.top(), y), max(rect.top(), rect.bottom() - popup_size.height()))
        self._hover_popup.move(x, y)

    def _hide_hover_preview(self) -> None:
        self._hover_popup.hide()
        self._hover_row = None

    def _on_pick_local(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Image",
            "",
            "Images (*.png *.jpg *.jpe *.jpeg *.jfif *.avif *.webp *.bmp);;All Files (*)",
        )
        if not selected:
            return
        self._set_local_source(selected, "Local File")

    def _on_pick_web_capture(self) -> None:
        if self._web_capture_dialog is not None:
            self._web_capture_dialog.show()
            self._web_capture_dialog.raise_()
            self._web_capture_dialog.activateWindow()
            return
        query = f"{self.folder_name} game icon"
        browser = WebDownloadCaptureDialog(
            query,
            self,
            selection_callback=self._on_web_capture_image_selected,
        )
        browser.setWindowModality(Qt.WindowModality.NonModal)
        browser.finished.connect(self._on_web_capture_dialog_finished)
        self._web_capture_dialog = browser
        browser.show()
        browser.raise_()
        browser.activateWindow()

    def _on_web_capture_dialog_finished(self, _result: int) -> None:
        self._web_capture_dialog = None

    def _on_web_capture_image_selected(self, path: str) -> None:
        self._set_local_source(path, "Web Download")

    def _set_local_source(self, path: str, source_label: str) -> None:
        image_bytes: bytes
        try:
            image_bytes = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.warning(
                self, "Image Read Failed", f"Could not read selected image:\n{exc}"
            )
            return
        image_bytes = _normalize_image_bytes_for_canvas(image_bytes)
        self._local_image_path = path
        self._local_source_label = source_label
        self._source_image_bytes = image_bytes
        self._prepared_image_bytes = None
        self._prepared_is_final_composite = False
        self._upsert_local_source_row(path, image_bytes, source_label)

    def _resolve_current_image_bytes(self) -> bytes | None:
        row = self.table.currentRow()
        if self._local_source_row is not None and row == self._local_source_row:
            return self._prepared_image_bytes or self._source_image_bytes
        if 0 <= row < len(self.candidates):
            try:
                return self._image_loader(self.candidates[row])
            except Exception as exc:
                QMessageBox.warning(
                    self, "Image Download Failed", f"Could not download selected image:\n{exc}"
                )
                return None
        if self._prepared_image_bytes:
            return self._prepared_image_bytes
        if self._source_image_bytes:
            return self._source_image_bytes
        if self._local_image_path:
            try:
                return Path(self._local_image_path).read_bytes()
            except OSError as exc:
                QMessageBox.warning(
                    self, "Image Read Failed", f"Could not read local image:\n{exc}"
                )
                return None
        return None

    def _on_adjust_framing(self) -> None:
        image_bytes = self._resolve_current_image_bytes()
        if image_bytes is None:
            QMessageBox.information(
                self,
                "No Image Source",
                "Select a candidate, local image, or captured web image first.",
            )
            return
        dialog = IconFramingDialog(
            image_bytes,
            border_style=self._current_icon_style(),
            initial_bg_removal_engine=self._current_bg_removal_engine(),
            initial_bg_removal_params=dict(self._bg_removal_params),
            initial_text_preserve_config=dict(self._text_preserve_config),
            border_shader=self._border_shader_config(),
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        style_idx = self.icon_style_combo.findData(dialog.selected_style())
        if style_idx >= 0:
            self.icon_style_combo.setCurrentIndex(style_idx)
        bg_idx = self.bg_removal_combo.findData(dialog.selected_bg_removal_engine())
        if bg_idx >= 0:
            self.bg_removal_combo.setCurrentIndex(bg_idx)
        self._bg_removal_params = dialog.selected_bg_removal_params()
        self._text_preserve_config = dialog.selected_text_preserve_config()
        text_idx = self.text_extract_combo.findData(
            str(self._text_preserve_config.get("method", "none") or "none")
        )
        if text_idx >= 0:
            self.text_extract_combo.setCurrentIndex(text_idx)
        self._border_shader = dialog.selected_border_shader()
        self._refresh_border_shader_button()
        if self._border_shader_saver is not None:
            try:
                self._border_shader_saver(dict(self._border_shader))
            except Exception:
                pass
        framed = dialog.framed_image_bytes()
        if not framed:
            return
        self._prepared_image_bytes = framed
        self._prepared_is_final_composite = dialog.framed_image_is_final_composite()
        self._source_image_bytes = framed
        local_path = self._local_image_path or "(framed image)"
        label = (
            f"{self._local_source_label} (Framed)"
            if self._local_source_label
            else "Framed"
        )
        self._upsert_local_source_row(local_path, framed, label)
        # Applying framing in Set Icon flow should immediately finalize selection.
        self._validate_then_accept()

    def _on_source_selection_changed(self) -> None:
        current_row = self.table.currentRow()
        if self._is_heavy_bg_engine() and current_row >= 0:
            self._refresh_preview_icon_row(current_row)
        if 0 <= current_row < len(self.candidates):
            self._local_image_path = None
            self._source_image_bytes = None
            self._prepared_image_bytes = None
            self._prepared_is_final_composite = False

    def _validate_then_accept(self) -> None:
        row = self.table.currentRow()
        has_local = self._local_image_path is not None or self._source_image_bytes is not None
        if row < 0 and not has_local and self._prepared_image_bytes is None:
            QMessageBox.warning(
                self,
                "No Selection",
                "Select one candidate row or choose a local image.",
            )
            return
        if (
            self._local_image_path
            and self._source_image_bytes is None
            and not Path(self._local_image_path).exists()
        ):
            QMessageBox.warning(
                self,
                "Missing File",
                "The selected local/captured file no longer exists.",
            )
            return
        self._close_web_capture_dialog_async()
        self.accept()

    def result_payload(self) -> IconPickerResult:
        row = self.table.currentRow()
        candidate = self.candidates[row] if 0 <= row < len(self.candidates) else None
        return IconPickerResult(
            candidate=candidate,
            local_image_path=self._local_image_path,
            source_image_bytes=self._source_image_bytes,
            prepared_image_bytes=self._prepared_image_bytes,
            prepared_is_final_composite=self._prepared_is_final_composite,
            info_tip=self.info_tip_edit.toPlainText().strip(),
            icon_style=self._current_icon_style(),
            bg_removal_engine=self._current_bg_removal_engine(),
            bg_removal_params=dict(self._bg_removal_params),
            text_preserve_config=dict(self._text_preserve_config),
            border_shader=self._border_shader_config(),
        )


class PerformanceSettingsDialog(QDialog):
    def __init__(
        self,
        initial: PerformanceSettingsResult,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Performance Settings")

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Tune scan/move responsiveness and cache behavior. "
                "Higher worker counts can improve throughput on fast storage."
            )
        )

        prewarm_row = QHBoxLayout()
        prewarm_row.addWidget(QLabel("Startup model preload:", self))
        self.prewarm_mode_combo = QComboBox(self)
        self.prewarm_mode_combo.addItem("Off", "off")
        self.prewarm_mode_combo.addItem("Minimal", "minimal")
        self.prewarm_mode_combo.addItem("Full", "full")
        mode = str(initial.startup_prewarm_mode or "minimal").strip().casefold()
        idx = self.prewarm_mode_combo.findData(mode if mode in {"off", "minimal", "full"} else "minimal")
        if idx >= 0:
            self.prewarm_mode_combo.setCurrentIndex(idx)
        prewarm_row.addWidget(self.prewarm_mode_combo)
        prewarm_row.addStretch(1)
        layout.addLayout(prewarm_row)

        workers_row = QHBoxLayout()
        workers_row.addWidget(QLabel("Folder-size workers (0 = auto):", self))
        self.workers_spin = QSpinBox(self)
        self.workers_spin.setRange(0, 64)
        self.workers_spin.setValue(max(0, min(64, int(initial.scan_size_workers))))
        workers_row.addWidget(self.workers_spin)
        workers_row.addStretch(1)
        layout.addLayout(workers_row)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("Progress update interval (ms):", self))
        self.interval_spin = QSpinBox(self)
        self.interval_spin.setRange(10, 500)
        self.interval_spin.setValue(max(10, min(500, int(initial.progress_interval_ms))))
        interval_row.addWidget(self.interval_spin)
        interval_row.addStretch(1)
        layout.addLayout(interval_row)

        cache_row = QHBoxLayout()
        self.cache_enabled = QCheckBox("Enable directory-size cache", self)
        self.cache_enabled.setChecked(bool(initial.dir_cache_enabled))
        cache_row.addWidget(self.cache_enabled)
        cache_row.addStretch(1)
        layout.addLayout(cache_row)

        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("Cache max entries:", self))
        self.cache_max_spin = QSpinBox(self)
        self.cache_max_spin.setRange(1_000, 2_000_000)
        self.cache_max_spin.setSingleStep(10_000)
        self.cache_max_spin.setValue(
            max(1_000, min(2_000_000, int(initial.dir_cache_max_entries)))
        )
        max_row.addWidget(self.cache_max_spin)
        max_row.addStretch(1)
        layout.addLayout(max_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_payload(self) -> PerformanceSettingsResult:
        return PerformanceSettingsResult(
            scan_size_workers=int(self.workers_spin.value()),
            progress_interval_ms=int(self.interval_spin.value()),
            dir_cache_enabled=self.cache_enabled.isChecked(),
            dir_cache_max_entries=int(self.cache_max_spin.value()),
            startup_prewarm_mode=str(self.prewarm_mode_combo.currentData() or "minimal"),
        )


class IconProviderSettingsDialog(QDialog):
    def __init__(
        self,
        initial: IconProviderSettingsResult,
        test_callback,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Icon Provider Settings")
        self._test_callback = test_callback

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Configure API keys/endpoints for icon sources."))

        self.steam_enabled = QCheckBox("Enable SteamGridDB")
        self.steam_enabled.setChecked(initial.steamgriddb_enabled)
        layout.addWidget(self.steam_enabled)
        self.steam_key = QLineEdit(initial.steamgriddb_api_key)
        self.steam_key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.steam_key.setPlaceholderText("SteamGridDB API Key")
        layout.addWidget(self.steam_key)
        self.steam_base = QLineEdit(initial.steamgriddb_api_base)
        self.steam_base.setPlaceholderText("SteamGridDB API Base URL")
        layout.addWidget(self.steam_base)

        self.iconfinder_enabled = QCheckBox("Enable Iconfinder")
        self.iconfinder_enabled.setChecked(initial.iconfinder_enabled)
        layout.addWidget(self.iconfinder_enabled)
        self.iconfinder_key = QLineEdit(initial.iconfinder_api_key)
        self.iconfinder_key.setEchoMode(QLineEdit.EchoMode.PasswordEchoOnEdit)
        self.iconfinder_key.setPlaceholderText("Iconfinder API Key")
        layout.addWidget(self.iconfinder_key)
        self.iconfinder_base = QLineEdit(initial.iconfinder_api_base)
        self.iconfinder_base.setPlaceholderText("Iconfinder API Base URL")
        layout.addWidget(self.iconfinder_base)

        actions = QHBoxLayout()
        self.test_btn = QPushButton("Test Credentials")
        self.test_btn.clicked.connect(self._on_test)
        actions.addWidget(self.test_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_test(self) -> None:
        try:
            msg = self._test_callback(self.result_payload())
        except Exception as exc:
            QMessageBox.warning(self, "Credentials Test", f"Test failed:\n{exc}")
            return
        QMessageBox.information(self, "Credentials Test", msg)

    def result_payload(self) -> IconProviderSettingsResult:
        return IconProviderSettingsResult(
            steamgriddb_enabled=self.steam_enabled.isChecked(),
            steamgriddb_api_key=self.steam_key.text().strip(),
            steamgriddb_api_base=self.steam_base.text().strip(),
            iconfinder_enabled=self.iconfinder_enabled.isChecked(),
            iconfinder_api_key=self.iconfinder_key.text().strip(),
            iconfinder_api_base=self.iconfinder_base.text().strip(),
        )


class DeleteGroupDialog(QDialog):
    def __init__(
        self,
        cleaned_name: str,
        rows: list[tuple[InventoryItem, str]],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Deleting {cleaned_name}")
        self.cancel_all_requested = False
        self.rows = list(
            sorted(
                rows,
                key=lambda x: x[0].modified_at,
                reverse=True,
            )
        )

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Select versions to delete. The newest version is unselected by default."
            )
        )

        self.table = QTableWidget(len(self.rows), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Delete", "Full Name", "Modified", "Size", "Source"]
        )
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)

        for row_idx, (item, source_text) in enumerate(self.rows):
            check_item = QTableWidgetItem("")
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            # Newest is first row after sort(desc), default unchecked.
            check_item.setCheckState(
                Qt.CheckState.Unchecked if row_idx == 0 else Qt.CheckState.Checked
            )
            self.table.setItem(row_idx, 0, check_item)
            self.table.setItem(row_idx, 1, QTableWidgetItem(item.full_name))
            self.table.setItem(
                row_idx, 2, QTableWidgetItem(item.modified_at.strftime("%Y-%m-%d %H:%M:%S"))
            )
            self.table.setItem(row_idx, 3, QTableWidgetItem(str(item.size_bytes)))
            self.table.setItem(row_idx, 4, QTableWidgetItem(source_text))

        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(self)
        self.ok_btn = QPushButton("Confirm")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_all_btn = QPushButton("Cancel All")
        buttons.addButton(self.ok_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.addButton(self.cancel_all_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.ok_btn.clicked.connect(self._validate_then_accept)
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_all_btn.clicked.connect(self._cancel_all)
        layout.addWidget(buttons)

        self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def selected_for_delete(self) -> list[InventoryItem]:
        selected: list[InventoryItem] = []
        for row_idx, (item, _) in enumerate(self.rows):
            check_item = self.table.item(row_idx, 0)
            if check_item and check_item.checkState() == Qt.CheckState.Checked:
                selected.append(item)
        return selected

    def _validate_then_accept(self) -> None:
        selected = self.selected_for_delete()
        if not selected:
            answer = QMessageBox.question(
                self,
                "No Selection",
                "No versions are selected for deletion. Continue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self.accept()
            return
        if len(selected) == len(self.rows):
            answer = QMessageBox.warning(
                self,
                "Delete All Versions",
                "All versions are selected. Are you certain you want to delete all versions of this game?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.accept()

    def _cancel_all(self) -> None:
        self.cancel_all_requested = True
        self.reject()
