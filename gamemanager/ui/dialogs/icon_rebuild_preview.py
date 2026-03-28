from __future__ import annotations

from collections.abc import Callable
import copy
import json
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gamemanager.models import IconRebuildEntry
from gamemanager.services.icon_pipeline import (
    ICO_SIZES,
    default_icon_size_improvements,
    normalize_icon_size_improvements,
)


@dataclass(slots=True)
class IconRebuildPreviewItem:
    label: str
    folder_path: str
    icon_path: str
    already_rebuilt: bool
    summary: str
    entry: IconRebuildEntry


class _ZoomableImagePane(QWidget):
    class _CenteredCanvas(QWidget):
        def __init__(self, parent: QWidget | None = None):
            super().__init__(parent)
            self._pixmap = QPixmap()
            self.setAutoFillBackground(False)

        def set_pixmap(self, pixmap: QPixmap) -> None:
            self._pixmap = QPixmap(pixmap)
            self.update()

        def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
            painter = QPainter(self)
            painter.fillRect(self.rect(), Qt.GlobalColor.transparent)
            if self._pixmap.isNull():
                painter.setPen(Qt.GlobalColor.lightGray)
                painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No preview available")
                return
            x = (self.width() - self._pixmap.width()) // 2
            y = (self.height() - self._pixmap.height()) // 2
            painter.drawPixmap(x, y, self._pixmap)

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._source_pixmap = QPixmap()
        self._zoom = 1.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(title_label)

        self._frame = QFrame(self)
        self._frame.setFrameShape(QFrame.Shape.StyledPanel)
        self._frame.setStyleSheet("background-color: #1b1b1b;")
        self._frame.setMinimumSize(360, 330)
        self._frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        frame_layout = QVBoxLayout(self._frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        self._canvas = self._CenteredCanvas(self._frame)
        self._canvas.setMinimumSize(300, 270)
        self._canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        frame_layout.addWidget(self._canvas, 1)
        layout.addWidget(self._frame, 1)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._source_pixmap = QPixmap(pixmap)
        self._apply_zoom()

    def set_zoom(self, zoom: float) -> None:
        self._zoom = max(0.2, min(32.0, float(zoom)))
        self._apply_zoom()

    def _apply_zoom(self) -> None:
        if self._source_pixmap.isNull():
            self._canvas.set_pixmap(QPixmap())
            return
        width = max(1, int(round(self._source_pixmap.width() * self._zoom)))
        height = max(1, int(round(self._source_pixmap.height() * self._zoom)))
        scaled = self._source_pixmap.scaled(
            width,
            height,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation,
        )
        self._canvas.set_pixmap(scaled)


class IconRebuildPreviewDialog(QDialog):
    def __init__(
        self,
        items: list[IconRebuildPreviewItem],
        frame_loader: Callable[
            [IconRebuildEntry, dict[int, dict[str, object]]],
            dict[int, tuple[bytes, bytes]],
        ],
        size_improvements: dict[int, dict[str, object]] | None = None,
        default_size_improvements: dict[int, dict[str, object]] | None = None,
        create_backups: bool = True,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Rebuild Icon Preview")
        self.resize(1480, 920)
        self._items = list(items)
        self._frame_loader = frame_loader
        self._zoom = 4.0
        self._syncing_controls = False
        self._settings_dirty = False
        self._create_backups = bool(create_backups)
        self._debounce_ms = 280
        self._refresh_debounce_timer = QTimer(self)
        self._refresh_debounce_timer.setSingleShot(True)
        self._refresh_debounce_timer.timeout.connect(self._on_refresh_debounce_timeout)

        self._size_order = tuple(int(value) for value in ICO_SIZES)
        self._size_improvements = normalize_icon_size_improvements(
            size_improvements,
            self._size_order,
        )
        self._default_size_improvements = normalize_icon_size_improvements(
            default_size_improvements,
            self._size_order,
        )
        self._frames_by_item: dict[int, dict[int, tuple[bytes, bytes]]] = {}
        self._token_by_item: dict[int, str] = {}

        self._item_combo = QComboBox(self)
        for item in self._items:
            self._item_combo.addItem(item.label)
        self._item_combo.currentIndexChanged.connect(self._on_item_changed)

        self._size_combo = QComboBox(self)
        for size in self._size_order:
            self._size_combo.addItem(f"{size} x {size}", size)
        size_index = self._size_combo.findData(32)
        self._size_combo.setCurrentIndex(size_index if size_index >= 0 else 0)
        self._size_combo.currentIndexChanged.connect(self._on_size_changed)

        self._zoom_spin = QDoubleSpinBox(self)
        self._zoom_spin.setSuffix("%")
        self._zoom_spin.setRange(20.0, 3200.0)
        self._zoom_spin.setDecimals(1)
        self._zoom_spin.setSingleStep(10.0)
        self._zoom_spin.setValue(400.0)
        self._zoom_spin.valueChanged.connect(self._on_zoom_spin_changed)
        zoom_out_btn = QPushButton("Zoom -", self)
        zoom_in_btn = QPushButton("Zoom +", self)
        zoom_reset_btn = QPushButton("100%", self)
        zoom_out_btn.clicked.connect(lambda: self._set_zoom(self._zoom / 1.15))
        zoom_in_btn.clicked.connect(lambda: self._set_zoom(self._zoom * 1.15))
        zoom_reset_btn.clicked.connect(lambda: self._set_zoom(1.0))

        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel("Item:", self))
        controls_row.addWidget(self._item_combo, 2)
        controls_row.addWidget(QLabel("Resolution:", self))
        controls_row.addWidget(self._size_combo, 0)
        controls_row.addStretch(1)
        controls_row.addWidget(self._zoom_spin, 0)
        controls_row.addWidget(zoom_out_btn, 0)
        controls_row.addWidget(zoom_in_btn, 0)
        controls_row.addWidget(zoom_reset_btn, 0)

        self._meta_label = QLabel("", self)
        self._meta_label.setWordWrap(True)
        self._settings_state_label = QLabel("", self)
        self._settings_state_label.setWordWrap(True)

        settings_group = QGroupBox("Improvements (Per Resolution)", self)
        settings_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        settings_layout = QGridLayout(settings_group)
        settings_layout.setHorizontalSpacing(14)
        settings_layout.setColumnStretch(0, 0)
        settings_layout.setColumnStretch(1, 1)
        settings_layout.setColumnStretch(2, 0)
        settings_layout.setColumnMinimumWidth(3, 14)
        settings_layout.setColumnStretch(3, 0)
        settings_layout.setColumnStretch(4, 0)
        settings_layout.setColumnStretch(5, 1)
        settings_layout.setColumnStretch(6, 0)
        left_row = 0
        right_row = 0

        def _add_settings_triplet(
            side: str,
            left_widget: QWidget,
            middle_widget: QWidget | None,
            right_widget: QWidget | None,
            *,
            left_colspan: int = 1,
        ) -> None:
            nonlocal left_row, right_row
            target_row = left_row if side == "left" else right_row
            base_col = 0 if side == "left" else 4
            settings_layout.addWidget(left_widget, target_row, base_col, 1, left_colspan)
            if middle_widget is not None:
                settings_layout.addWidget(middle_widget, target_row, base_col + 1)
            if right_widget is not None:
                settings_layout.addWidget(right_widget, target_row, base_col + 2)
            if side == "left":
                left_row += 1
            else:
                right_row += 1

        self._contrast_enabled = QCheckBox("Contrast", settings_group)
        self._contrast_spin = QDoubleSpinBox(settings_group)
        self._contrast_spin.setRange(0.5, 2.5)
        self._contrast_spin.setSingleStep(0.02)
        self._contrast_spin.setDecimals(3)
        self._contrast_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._contrast_enabled,
            self._contrast_spin,
            self._contrast_default_btn,
        )

        self._saturation_enabled = QCheckBox("Saturation", settings_group)
        self._saturation_spin = QDoubleSpinBox(settings_group)
        self._saturation_spin.setRange(0.0, 2.5)
        self._saturation_spin.setSingleStep(0.02)
        self._saturation_spin.setDecimals(3)
        self._saturation_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._saturation_enabled,
            self._saturation_spin,
            self._saturation_default_btn,
        )

        self._sharpness_enabled = QCheckBox("Sharpness", settings_group)
        self._sharpness_spin = QDoubleSpinBox(settings_group)
        self._sharpness_spin.setRange(0.0, 4.0)
        self._sharpness_spin.setSingleStep(0.05)
        self._sharpness_spin.setDecimals(3)
        self._sharpness_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._sharpness_enabled,
            self._sharpness_spin,
            self._sharpness_default_btn,
        )

        self._brightness_enabled = QCheckBox("Brightness", settings_group)
        self._brightness_spin = QDoubleSpinBox(settings_group)
        self._brightness_spin.setRange(0.5, 1.8)
        self._brightness_spin.setSingleStep(0.01)
        self._brightness_spin.setDecimals(3)
        self._brightness_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._brightness_enabled,
            self._brightness_spin,
            self._brightness_default_btn,
        )

        self._silhouette_enabled = QCheckBox("Silhouette Normalize", settings_group)
        self._silhouette_min_spin = QDoubleSpinBox(settings_group)
        self._silhouette_min_spin.setRange(0.03, 0.90)
        self._silhouette_min_spin.setSingleStep(0.01)
        self._silhouette_min_spin.setDecimals(3)
        self._silhouette_max_spin = QDoubleSpinBox(settings_group)
        self._silhouette_max_spin.setRange(0.05, 0.96)
        self._silhouette_max_spin.setSingleStep(0.01)
        self._silhouette_max_spin.setDecimals(3)
        self._silhouette_threshold_spin = QSpinBox(settings_group)
        self._silhouette_threshold_spin.setRange(1, 255)
        self._silhouette_scale_up_spin = QDoubleSpinBox(settings_group)
        self._silhouette_scale_up_spin.setRange(1.0, 4.0)
        self._silhouette_scale_up_spin.setSingleStep(0.05)
        self._silhouette_scale_up_spin.setDecimals(3)
        self._silhouette_scale_min_spin = QDoubleSpinBox(settings_group)
        self._silhouette_scale_min_spin.setRange(0.2, 1.0)
        self._silhouette_scale_min_spin.setSingleStep(0.05)
        self._silhouette_scale_min_spin.setDecimals(3)
        self._silhouette_allow_downscale = QCheckBox("Allow shrink", settings_group)
        silhouette_row_widget = QWidget(settings_group)
        silhouette_row_layout = QVBoxLayout(silhouette_row_widget)
        silhouette_row_layout.setContentsMargins(0, 0, 0, 0)
        silhouette_row_layout.setSpacing(2)
        silhouette_line1 = QHBoxLayout()
        silhouette_line1.setContentsMargins(0, 0, 0, 0)
        silhouette_line1.addWidget(QLabel("Min:", silhouette_row_widget))
        silhouette_line1.addWidget(self._silhouette_min_spin)
        silhouette_line1.addWidget(QLabel("Max:", silhouette_row_widget))
        silhouette_line1.addWidget(self._silhouette_max_spin)
        silhouette_line1.addWidget(QLabel("Thr:", silhouette_row_widget))
        silhouette_line1.addWidget(self._silhouette_threshold_spin)
        silhouette_line2 = QHBoxLayout()
        silhouette_line2.setContentsMargins(0, 0, 0, 0)
        silhouette_line2.addWidget(QLabel("Up:", silhouette_row_widget))
        silhouette_line2.addWidget(self._silhouette_scale_up_spin)
        silhouette_line2.addWidget(QLabel("MinScale:", silhouette_row_widget))
        silhouette_line2.addWidget(self._silhouette_scale_min_spin)
        silhouette_line2.addWidget(self._silhouette_allow_downscale)
        silhouette_line2.addStretch(1)
        silhouette_row_layout.addLayout(silhouette_line1)
        silhouette_row_layout.addLayout(silhouette_line2)
        self._silhouette_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._silhouette_enabled,
            silhouette_row_widget,
            self._silhouette_default_btn,
        )

        self._pre_enabled = QCheckBox("Pre-Downscale Stage", settings_group)
        self._pre_enabled_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._pre_enabled,
            None,
            self._pre_enabled_default_btn,
            left_colspan=2,
        )

        self._pre_simplify_enabled = QCheckBox("Pre Simplify", settings_group)
        self._pre_simplify_strength = QDoubleSpinBox(settings_group)
        self._pre_simplify_strength.setRange(0.0, 1.0)
        self._pre_simplify_strength.setSingleStep(0.02)
        self._pre_simplify_strength.setDecimals(3)
        self._pre_working_scale = QDoubleSpinBox(settings_group)
        self._pre_working_scale.setRange(1.0, 4.0)
        self._pre_working_scale.setSingleStep(0.05)
        self._pre_working_scale.setDecimals(3)
        pre_simplify_row = QWidget(settings_group)
        pre_simplify_layout = QVBoxLayout(pre_simplify_row)
        pre_simplify_layout.setContentsMargins(0, 0, 0, 0)
        pre_simplify_layout.setSpacing(2)
        pre_simplify_line = QHBoxLayout()
        pre_simplify_line.setContentsMargins(0, 0, 0, 0)
        pre_simplify_line.addWidget(QLabel("Strength:", pre_simplify_row))
        pre_simplify_line.addWidget(self._pre_simplify_strength)
        pre_simplify_line.addWidget(QLabel("Working scale:", pre_simplify_row))
        pre_simplify_line.addWidget(self._pre_working_scale)
        pre_simplify_line.addStretch(1)
        pre_simplify_layout.addLayout(pre_simplify_line)
        self._pre_simplify_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._pre_simplify_enabled,
            pre_simplify_row,
            self._pre_simplify_default_btn,
        )

        self._pre_prune_enabled = QCheckBox("Pre Detail Prune", settings_group)
        self._pre_prune_pixels = QSpinBox(settings_group)
        self._pre_prune_pixels.setRange(1, 128)
        self._pre_prune_alpha = QSpinBox(settings_group)
        self._pre_prune_alpha.setRange(1, 255)
        pre_prune_row = QWidget(settings_group)
        pre_prune_layout = QVBoxLayout(pre_prune_row)
        pre_prune_layout.setContentsMargins(0, 0, 0, 0)
        pre_prune_layout.setSpacing(2)
        pre_prune_line = QHBoxLayout()
        pre_prune_line.setContentsMargins(0, 0, 0, 0)
        pre_prune_line.addWidget(QLabel("Min pixels:", pre_prune_row))
        pre_prune_line.addWidget(self._pre_prune_pixels)
        pre_prune_line.addWidget(QLabel("Alpha thr:", pre_prune_row))
        pre_prune_line.addWidget(self._pre_prune_alpha)
        pre_prune_line.addStretch(1)
        pre_prune_layout.addLayout(pre_prune_line)
        self._pre_prune_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._pre_prune_enabled,
            pre_prune_row,
            self._pre_prune_default_btn,
        )

        self._pre_stroke_enabled = QCheckBox("Pre Stroke Boost", settings_group)
        self._pre_stroke_px = QSpinBox(settings_group)
        self._pre_stroke_px.setRange(0, 4)
        self._pre_stroke_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "left",
            self._pre_stroke_enabled,
            self._pre_stroke_px,
            self._pre_stroke_default_btn,
        )

        self._tiny_enabled = QCheckBox("Tiny Pass (Master Toggle)", settings_group)
        self._tiny_enabled_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "right",
            self._tiny_enabled,
            None,
            self._tiny_enabled_default_btn,
            left_colspan=2,
        )

        self._tiny_unsharp_enabled = QCheckBox("Unsharp", settings_group)
        self._tiny_unsharp_radius = QDoubleSpinBox(settings_group)
        self._tiny_unsharp_radius.setRange(0.0, 4.0)
        self._tiny_unsharp_radius.setSingleStep(0.05)
        self._tiny_unsharp_radius.setDecimals(3)
        self._tiny_unsharp_percent = QSpinBox(settings_group)
        self._tiny_unsharp_percent.setRange(0, 300)
        self._tiny_unsharp_threshold = QSpinBox(settings_group)
        self._tiny_unsharp_threshold.setRange(0, 64)
        tiny_unsharp_row = QWidget(settings_group)
        tiny_unsharp_layout = QVBoxLayout(tiny_unsharp_row)
        tiny_unsharp_layout.setContentsMargins(0, 0, 0, 0)
        tiny_unsharp_layout.setSpacing(2)
        tiny_unsharp_line1 = QHBoxLayout()
        tiny_unsharp_line1.setContentsMargins(0, 0, 0, 0)
        tiny_unsharp_line1.addWidget(QLabel("Radius:", tiny_unsharp_row))
        tiny_unsharp_line1.addWidget(self._tiny_unsharp_radius)
        tiny_unsharp_line1.addWidget(QLabel("Percent:", tiny_unsharp_row))
        tiny_unsharp_line1.addWidget(self._tiny_unsharp_percent)
        tiny_unsharp_line2 = QHBoxLayout()
        tiny_unsharp_line2.setContentsMargins(0, 0, 0, 0)
        tiny_unsharp_line2.addWidget(QLabel("Threshold:", tiny_unsharp_row))
        tiny_unsharp_line2.addWidget(self._tiny_unsharp_threshold)
        tiny_unsharp_line2.addStretch(1)
        tiny_unsharp_layout.addLayout(tiny_unsharp_line1)
        tiny_unsharp_layout.addLayout(tiny_unsharp_line2)
        self._tiny_unsharp_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "right",
            self._tiny_unsharp_enabled,
            tiny_unsharp_row,
            self._tiny_unsharp_default_btn,
        )

        self._tiny_micro_enabled = QCheckBox("Micro Contrast", settings_group)
        self._tiny_micro_spin = QDoubleSpinBox(settings_group)
        self._tiny_micro_spin.setRange(0.5, 1.8)
        self._tiny_micro_spin.setSingleStep(0.01)
        self._tiny_micro_spin.setDecimals(3)
        self._tiny_micro_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "right",
            self._tiny_micro_enabled,
            self._tiny_micro_spin,
            self._tiny_micro_default_btn,
        )

        self._tiny_alpha_enabled = QCheckBox("Alpha Cleanup", settings_group)
        self._tiny_alpha_spin = QSpinBox(settings_group)
        self._tiny_alpha_spin.setRange(0, 255)
        self._tiny_alpha_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "right",
            self._tiny_alpha_enabled,
            self._tiny_alpha_spin,
            self._tiny_alpha_default_btn,
        )

        self._tiny_prune_enabled = QCheckBox("Tiny Detail Prune", settings_group)
        self._tiny_prune_pixels_spin = QSpinBox(settings_group)
        self._tiny_prune_pixels_spin.setRange(1, 128)
        self._tiny_prune_alpha_spin = QSpinBox(settings_group)
        self._tiny_prune_alpha_spin.setRange(1, 255)
        tiny_prune_row = QWidget(settings_group)
        tiny_prune_layout = QVBoxLayout(tiny_prune_row)
        tiny_prune_layout.setContentsMargins(0, 0, 0, 0)
        tiny_prune_layout.setSpacing(2)
        tiny_prune_line = QHBoxLayout()
        tiny_prune_line.setContentsMargins(0, 0, 0, 0)
        tiny_prune_line.addWidget(QLabel("Min pixels:", tiny_prune_row))
        tiny_prune_line.addWidget(self._tiny_prune_pixels_spin)
        tiny_prune_line.addWidget(QLabel("Alpha thr:", tiny_prune_row))
        tiny_prune_line.addWidget(self._tiny_prune_alpha_spin)
        tiny_prune_line.addStretch(1)
        tiny_prune_layout.addLayout(tiny_prune_line)
        self._tiny_prune_default_btn = QPushButton("Set as Default", settings_group)
        _add_settings_triplet(
            "right",
            self._tiny_prune_enabled,
            tiny_prune_row,
            self._tiny_prune_default_btn,
        )

        settings_buttons_row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh Now", settings_group)
        self._reset_size_defaults_btn = QPushButton("Reset Size Defaults", settings_group)
        self._reset_all_defaults_btn = QPushButton("Reset All Defaults", settings_group)
        settings_buttons_row.addWidget(self._refresh_btn)
        settings_buttons_row.addWidget(self._reset_size_defaults_btn)
        settings_buttons_row.addWidget(self._reset_all_defaults_btn)
        settings_buttons_row.addStretch(1)
        settings_layout.addLayout(
            settings_buttons_row,
            max(left_row, right_row),
            0,
            1,
            7,
        )

        before_group = QGroupBox("Before (Current Icon)", self)
        after_group = QGroupBox("After (Rebuild Result)", self)
        before_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        after_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        before_layout = QVBoxLayout(before_group)
        after_layout = QVBoxLayout(after_group)
        self._before_view = _ZoomableImagePane("Current", before_group)
        self._after_view = _ZoomableImagePane("Rebuild", after_group)
        before_layout.addWidget(self._before_view)
        after_layout.addWidget(self._after_view)

        views_row = QHBoxLayout()
        views_row.setSpacing(10)
        views_row.addWidget(before_group, 1)
        views_row.addWidget(after_group, 1)

        action_panel = QWidget(self)
        action_panel.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        action_layout = QVBoxLayout(action_panel)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(8)
        self._continue_btn = QPushButton("Continue", action_panel)
        self._cancel_btn = QPushButton("Cancel", action_panel)
        self._backup_checkbox = QCheckBox("Create backups", action_panel)
        self._backup_checkbox.setChecked(self._create_backups)
        self._backup_checkbox.toggled.connect(self._on_backup_toggled)
        self._continue_btn.clicked.connect(self.accept)
        self._cancel_btn.clicked.connect(self.reject)
        action_layout.addWidget(self._backup_checkbox)
        action_layout.addWidget(self._continue_btn)
        action_layout.addWidget(self._cancel_btn)
        action_layout.addStretch(1)

        preview_and_actions_row = QHBoxLayout()
        preview_and_actions_row.setSpacing(14)
        preview_and_actions_row.addLayout(views_row, 1)
        preview_and_actions_row.addWidget(
            action_panel,
            0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
        )

        root = QVBoxLayout(self)
        root.addLayout(controls_row)
        root.addWidget(self._meta_label)
        root.addWidget(settings_group)
        root.addWidget(self._settings_state_label)
        root.addLayout(preview_and_actions_row, 1)

        self._wire_setting_controls()
        self._load_settings_controls_for_size(self._current_size())
        self._refresh_preview(force_reload=True)

    def _wire_setting_controls(self) -> None:
        checkboxes = [
            self._contrast_enabled,
            self._saturation_enabled,
            self._sharpness_enabled,
            self._brightness_enabled,
            self._silhouette_enabled,
            self._silhouette_allow_downscale,
            self._pre_enabled,
            self._pre_simplify_enabled,
            self._pre_prune_enabled,
            self._pre_stroke_enabled,
            self._tiny_enabled,
            self._tiny_unsharp_enabled,
            self._tiny_micro_enabled,
            self._tiny_alpha_enabled,
            self._tiny_prune_enabled,
        ]
        spins = [
            self._contrast_spin,
            self._saturation_spin,
            self._sharpness_spin,
            self._brightness_spin,
            self._silhouette_min_spin,
            self._silhouette_max_spin,
            self._silhouette_threshold_spin,
            self._silhouette_scale_up_spin,
            self._silhouette_scale_min_spin,
            self._pre_simplify_strength,
            self._pre_working_scale,
            self._pre_prune_pixels,
            self._pre_prune_alpha,
            self._pre_stroke_px,
            self._tiny_unsharp_radius,
            self._tiny_unsharp_percent,
            self._tiny_unsharp_threshold,
            self._tiny_micro_spin,
            self._tiny_alpha_spin,
            self._tiny_prune_pixels_spin,
            self._tiny_prune_alpha_spin,
        ]
        for checkbox in checkboxes:
            checkbox.toggled.connect(self._on_settings_edited)
        for spin in spins:
            spin.valueChanged.connect(self._on_settings_edited)

        self._refresh_btn.clicked.connect(lambda: self._refresh_preview(force_reload=True))
        self._reset_size_defaults_btn.clicked.connect(self._on_reset_size_defaults)
        self._reset_all_defaults_btn.clicked.connect(self._on_reset_all_defaults)

        self._contrast_default_btn.clicked.connect(
            lambda: self._set_single_setting_default(
                "contrast_enabled",
                bool(self._contrast_enabled.isChecked()),
                "contrast",
                float(self._contrast_spin.value()),
            )
        )
        self._saturation_default_btn.clicked.connect(
            lambda: self._set_single_setting_default(
                "saturation_enabled",
                bool(self._saturation_enabled.isChecked()),
                "saturation",
                float(self._saturation_spin.value()),
            )
        )
        self._sharpness_default_btn.clicked.connect(
            lambda: self._set_single_setting_default(
                "sharpness_enabled",
                bool(self._sharpness_enabled.isChecked()),
                "sharpness",
                float(self._sharpness_spin.value()),
            )
        )
        self._brightness_default_btn.clicked.connect(
            lambda: self._set_single_setting_default(
                "brightness_enabled",
                bool(self._brightness_enabled.isChecked()),
                "brightness",
                float(self._brightness_spin.value()),
            )
        )
        self._silhouette_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "silhouette_enabled": bool(self._silhouette_enabled.isChecked()),
                    "silhouette_target_min": float(self._silhouette_min_spin.value()),
                    "silhouette_target_max": float(self._silhouette_max_spin.value()),
                    "silhouette_alpha_threshold": int(self._silhouette_threshold_spin.value()),
                    "silhouette_max_upscale": float(self._silhouette_scale_up_spin.value()),
                    "silhouette_min_scale": float(self._silhouette_scale_min_spin.value()),
                    "silhouette_allow_downscale": bool(self._silhouette_allow_downscale.isChecked()),
                }
            )
        )
        self._pre_enabled_default_btn.clicked.connect(
            lambda: self._set_single_setting_default(
                "pre_enabled",
                bool(self._pre_enabled.isChecked()),
            )
        )
        self._pre_simplify_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "pre_simplify_enabled": bool(self._pre_simplify_enabled.isChecked()),
                    "pre_simplify_strength": float(self._pre_simplify_strength.value()),
                    "pre_working_scale": float(self._pre_working_scale.value()),
                }
            )
        )
        self._pre_prune_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "pre_prune_enabled": bool(self._pre_prune_enabled.isChecked()),
                    "pre_prune_min_pixels": int(self._pre_prune_pixels.value()),
                    "pre_prune_alpha_threshold": int(self._pre_prune_alpha.value()),
                }
            )
        )
        self._pre_stroke_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "pre_stroke_boost_enabled": bool(self._pre_stroke_enabled.isChecked()),
                    "pre_stroke_boost_px": int(self._pre_stroke_px.value()),
                }
            )
        )
        self._tiny_enabled_default_btn.clicked.connect(
            lambda: self._set_single_setting_default(
                "tiny_enabled",
                bool(self._tiny_enabled.isChecked()),
            )
        )
        self._tiny_unsharp_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "tiny_unsharp_enabled": bool(self._tiny_unsharp_enabled.isChecked()),
                    "tiny_unsharp_radius": float(self._tiny_unsharp_radius.value()),
                    "tiny_unsharp_percent": int(self._tiny_unsharp_percent.value()),
                    "tiny_unsharp_threshold": int(self._tiny_unsharp_threshold.value()),
                }
            )
        )
        self._tiny_micro_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "tiny_micro_contrast_enabled": bool(self._tiny_micro_enabled.isChecked()),
                    "tiny_micro_contrast": float(self._tiny_micro_spin.value()),
                }
            )
        )
        self._tiny_alpha_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "tiny_alpha_cleanup_enabled": bool(self._tiny_alpha_enabled.isChecked()),
                    "tiny_alpha_floor": int(self._tiny_alpha_spin.value()),
                }
            )
        )
        self._tiny_prune_default_btn.clicked.connect(
            lambda: self._set_multi_setting_default(
                {
                    "tiny_prune_enabled": bool(self._tiny_prune_enabled.isChecked()),
                    "tiny_prune_min_pixels": int(self._tiny_prune_pixels_spin.value()),
                    "tiny_prune_alpha_threshold": int(self._tiny_prune_alpha_spin.value()),
                }
            )
        )

    def _current_item(self) -> IconRebuildPreviewItem:
        idx = max(0, min(self._item_combo.currentIndex(), len(self._items) - 1))
        return self._items[idx]

    def _current_size(self) -> int:
        return int(self._size_combo.currentData() or self._size_order[-1])

    def _settings_token(self) -> str:
        return json.dumps(self._size_improvements, sort_keys=True)

    def _on_item_changed(self) -> None:
        self._refresh_preview(force_reload=False)
        if self._settings_dirty:
            self._schedule_debounced_refresh()

    def _on_size_changed(self) -> None:
        self._load_settings_controls_for_size(self._current_size())
        self._refresh_preview(force_reload=False)
        if self._settings_dirty:
            self._schedule_debounced_refresh()

    def _on_zoom_spin_changed(self, value: float) -> None:
        self._set_zoom(float(value) / 100.0)

    def _set_zoom(self, zoom: float) -> None:
        self._zoom = max(0.2, min(32.0, float(zoom)))
        blocked = self._zoom_spin.blockSignals(True)
        self._zoom_spin.setValue(self._zoom * 100.0)
        self._zoom_spin.blockSignals(blocked)
        self._before_view.set_zoom(self._zoom)
        self._after_view.set_zoom(self._zoom)

    @staticmethod
    def _pixmap_from_png(payload: bytes) -> QPixmap:
        pix = QPixmap()
        if payload:
            pix.loadFromData(payload, "PNG")
        return pix

    def _load_settings_controls_for_size(self, size: int) -> None:
        profile = self._size_improvements.get(int(size)) or default_icon_size_improvements((int(size),)).get(int(size), {})
        self._syncing_controls = True
        try:
            self._contrast_enabled.setChecked(bool(profile.get("contrast_enabled", True)))
            self._contrast_spin.setValue(float(profile.get("contrast", 1.0)))
            self._saturation_enabled.setChecked(bool(profile.get("saturation_enabled", True)))
            self._saturation_spin.setValue(float(profile.get("saturation", 1.0)))
            self._sharpness_enabled.setChecked(bool(profile.get("sharpness_enabled", True)))
            self._sharpness_spin.setValue(float(profile.get("sharpness", 1.0)))
            self._brightness_enabled.setChecked(bool(profile.get("brightness_enabled", True)))
            self._brightness_spin.setValue(float(profile.get("brightness", 1.0)))
            self._silhouette_enabled.setChecked(bool(profile.get("silhouette_enabled", False)))
            self._silhouette_min_spin.setValue(float(profile.get("silhouette_target_min", 0.12)))
            self._silhouette_max_spin.setValue(float(profile.get("silhouette_target_max", 0.70)))
            self._silhouette_threshold_spin.setValue(int(profile.get("silhouette_alpha_threshold", 8)))
            self._silhouette_scale_up_spin.setValue(float(profile.get("silhouette_max_upscale", 1.5)))
            self._silhouette_scale_min_spin.setValue(float(profile.get("silhouette_min_scale", 0.7)))
            self._silhouette_allow_downscale.setChecked(
                bool(profile.get("silhouette_allow_downscale", False))
            )
            self._pre_enabled.setChecked(bool(profile.get("pre_enabled", False)))
            self._pre_simplify_enabled.setChecked(bool(profile.get("pre_simplify_enabled", False)))
            self._pre_simplify_strength.setValue(float(profile.get("pre_simplify_strength", 0.0)))
            self._pre_working_scale.setValue(float(profile.get("pre_working_scale", 1.0)))
            self._pre_prune_enabled.setChecked(bool(profile.get("pre_prune_enabled", False)))
            self._pre_prune_pixels.setValue(int(profile.get("pre_prune_min_pixels", 1)))
            self._pre_prune_alpha.setValue(int(profile.get("pre_prune_alpha_threshold", 12)))
            self._pre_stroke_enabled.setChecked(bool(profile.get("pre_stroke_boost_enabled", False)))
            self._pre_stroke_px.setValue(int(profile.get("pre_stroke_boost_px", 0)))
            self._tiny_enabled.setChecked(bool(profile.get("tiny_enabled", False)))
            self._tiny_unsharp_enabled.setChecked(bool(profile.get("tiny_unsharp_enabled", True)))
            self._tiny_unsharp_radius.setValue(float(profile.get("tiny_unsharp_radius", 0.0)))
            self._tiny_unsharp_percent.setValue(int(profile.get("tiny_unsharp_percent", 0)))
            self._tiny_unsharp_threshold.setValue(int(profile.get("tiny_unsharp_threshold", 0)))
            self._tiny_micro_enabled.setChecked(bool(profile.get("tiny_micro_contrast_enabled", True)))
            self._tiny_micro_spin.setValue(float(profile.get("tiny_micro_contrast", 1.0)))
            self._tiny_alpha_enabled.setChecked(bool(profile.get("tiny_alpha_cleanup_enabled", True)))
            self._tiny_alpha_spin.setValue(int(profile.get("tiny_alpha_floor", 0)))
            self._tiny_prune_enabled.setChecked(bool(profile.get("tiny_prune_enabled", True)))
            self._tiny_prune_pixels_spin.setValue(int(profile.get("tiny_prune_min_pixels", 2)))
            self._tiny_prune_alpha_spin.setValue(int(profile.get("tiny_prune_alpha_threshold", 12)))
        finally:
            self._syncing_controls = False

    def _save_settings_controls_for_size(self, size: int) -> None:
        size_key = int(size)
        profile = self._size_improvements.get(size_key, {})
        profile.update(
            {
                "contrast_enabled": bool(self._contrast_enabled.isChecked()),
                "contrast": float(self._contrast_spin.value()),
                "saturation_enabled": bool(self._saturation_enabled.isChecked()),
                "saturation": float(self._saturation_spin.value()),
                "sharpness_enabled": bool(self._sharpness_enabled.isChecked()),
                "sharpness": float(self._sharpness_spin.value()),
                "brightness_enabled": bool(self._brightness_enabled.isChecked()),
                "brightness": float(self._brightness_spin.value()),
                "silhouette_enabled": bool(self._silhouette_enabled.isChecked()),
                "silhouette_target_min": float(self._silhouette_min_spin.value()),
                "silhouette_target_max": float(self._silhouette_max_spin.value()),
                "silhouette_alpha_threshold": int(self._silhouette_threshold_spin.value()),
                "silhouette_max_upscale": float(self._silhouette_scale_up_spin.value()),
                "silhouette_min_scale": float(self._silhouette_scale_min_spin.value()),
                "silhouette_allow_downscale": bool(self._silhouette_allow_downscale.isChecked()),
                "pre_enabled": bool(self._pre_enabled.isChecked()),
                "pre_simplify_enabled": bool(self._pre_simplify_enabled.isChecked()),
                "pre_simplify_strength": float(self._pre_simplify_strength.value()),
                "pre_working_scale": float(self._pre_working_scale.value()),
                "pre_prune_enabled": bool(self._pre_prune_enabled.isChecked()),
                "pre_prune_min_pixels": int(self._pre_prune_pixels.value()),
                "pre_prune_alpha_threshold": int(self._pre_prune_alpha.value()),
                "pre_stroke_boost_enabled": bool(self._pre_stroke_enabled.isChecked()),
                "pre_stroke_boost_px": int(self._pre_stroke_px.value()),
                "tiny_enabled": bool(self._tiny_enabled.isChecked()),
                "tiny_unsharp_enabled": bool(self._tiny_unsharp_enabled.isChecked()),
                "tiny_unsharp_radius": float(self._tiny_unsharp_radius.value()),
                "tiny_unsharp_percent": int(self._tiny_unsharp_percent.value()),
                "tiny_unsharp_threshold": int(self._tiny_unsharp_threshold.value()),
                "tiny_micro_contrast_enabled": bool(self._tiny_micro_enabled.isChecked()),
                "tiny_micro_contrast": float(self._tiny_micro_spin.value()),
                "tiny_alpha_cleanup_enabled": bool(self._tiny_alpha_enabled.isChecked()),
                "tiny_alpha_floor": int(self._tiny_alpha_spin.value()),
                "tiny_prune_enabled": bool(self._tiny_prune_enabled.isChecked()),
                "tiny_prune_min_pixels": int(self._tiny_prune_pixels_spin.value()),
                "tiny_prune_alpha_threshold": int(self._tiny_prune_alpha_spin.value()),
            }
        )
        self._size_improvements[size_key] = profile
        self._size_improvements = normalize_icon_size_improvements(
            self._size_improvements,
            self._size_order,
        )

    def _on_settings_edited(self) -> None:
        if self._syncing_controls:
            return
        self._save_settings_controls_for_size(self._current_size())
        self._settings_dirty = True
        self._refresh_state_label()
        self._schedule_debounced_refresh()

    def _refresh_state_label(self) -> None:
        if self._settings_dirty:
            self._settings_state_label.setText(
                f"Settings changed. Auto-refresh in {self._debounce_ms} ms."
            )
        else:
            self._settings_state_label.setText("Preview matches current settings.")

    def _schedule_debounced_refresh(self) -> None:
        self._refresh_debounce_timer.start(self._debounce_ms)

    def _on_refresh_debounce_timeout(self) -> None:
        self._refresh_preview(force_reload=True)

    def _on_backup_toggled(self, checked: bool) -> None:
        self._create_backups = bool(checked)

    def _set_single_setting_default(
        self,
        key_one: str,
        value_one: object,
        key_two: str | None = None,
        value_two: object | None = None,
    ) -> None:
        self._set_multi_setting_default(
            {k: v for k, v in ((key_one, value_one), (key_two, value_two)) if k is not None}
        )

    def _set_multi_setting_default(self, updates: dict[str, object]) -> None:
        size_key = self._current_size()
        current = dict(self._default_size_improvements.get(size_key, {}))
        current.update(updates)
        self._default_size_improvements[size_key] = current
        self._default_size_improvements = normalize_icon_size_improvements(
            self._default_size_improvements,
            self._size_order,
        )

    def _on_reset_size_defaults(self) -> None:
        size_key = self._current_size()
        defaults = self._default_size_improvements.get(size_key, {})
        self._size_improvements[size_key] = dict(defaults)
        self._size_improvements = normalize_icon_size_improvements(
            self._size_improvements,
            self._size_order,
        )
        self._load_settings_controls_for_size(size_key)
        self._settings_dirty = True
        self._refresh_state_label()

    def _on_reset_all_defaults(self) -> None:
        self._size_improvements = copy.deepcopy(self._default_size_improvements)
        self._size_improvements = normalize_icon_size_improvements(
            self._size_improvements,
            self._size_order,
        )
        self._load_settings_controls_for_size(self._current_size())
        self._settings_dirty = True
        self._refresh_state_label()

    def _refresh_preview(self, *, force_reload: bool) -> None:
        if not self._items:
            self._meta_label.setText("No preview data available.")
            self._before_view.set_pixmap(QPixmap())
            self._after_view.set_pixmap(QPixmap())
            return
        if force_reload and self._refresh_debounce_timer.isActive():
            self._refresh_debounce_timer.stop()

        if not self._syncing_controls:
            self._save_settings_controls_for_size(self._current_size())

        idx = max(0, min(self._item_combo.currentIndex(), len(self._items) - 1))
        item = self._items[idx]
        token = self._settings_token()
        need_reload = force_reload or self._token_by_item.get(idx) != token
        if need_reload:
            try:
                frames = self._frame_loader(item.entry, self._size_improvements)
            except Exception as exc:
                self._meta_label.setText(
                    f"Preview generation failed for {item.label}: {exc}"
                )
                self._before_view.set_pixmap(QPixmap())
                self._after_view.set_pixmap(QPixmap())
                return
            self._frames_by_item[idx] = frames
            self._token_by_item[idx] = token
            self._settings_dirty = False
            self._refresh_state_label()

        size = self._current_size()
        frames = self._frames_by_item.get(idx, {})
        frame = frames.get(size)
        if frame is None and frames:
            fallback = sorted(frames.keys())[0]
            frame = frames.get(fallback)
        before_payload = frame[0] if frame else b""
        after_payload = frame[1] if frame else b""
        before_pix = self._pixmap_from_png(before_payload)
        after_pix = self._pixmap_from_png(after_payload)
        self._before_view.set_pixmap(before_pix)
        self._after_view.set_pixmap(after_pix)
        self._before_view.set_zoom(self._zoom)
        self._after_view.set_zoom(self._zoom)

        rebuilt_text = "yes" if item.already_rebuilt else "no"
        self._meta_label.setText(
            f"Already rebuilt: {rebuilt_text}\n"
            f"Folder: {item.folder_path}\n"
            f"Icon: {item.icon_path}\n"
            f"{item.summary}"
        )

    def size_improvements(self) -> dict[int, dict[str, object]]:
        if not self._syncing_controls:
            self._save_settings_controls_for_size(self._current_size())
        return normalize_icon_size_improvements(self._size_improvements, self._size_order)

    def default_size_improvements(self) -> dict[int, dict[str, object]]:
        return normalize_icon_size_improvements(
            self._default_size_improvements,
            self._size_order,
        )

    def create_backups_enabled(self) -> bool:
        return bool(self._create_backups)
