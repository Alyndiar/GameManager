from __future__ import annotations

from collections.abc import Callable
import json
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QEvent, QObject, QPoint, QPointF, QRectF, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
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
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QVBoxLayout,
    QWidget,
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
    border_shader_to_dict,
    build_template_interior_mask_png,
    build_text_extraction_alpha_mask,
    build_text_extraction_overlay,
    build_multi_size_ico,
    build_template_overlay_preview,
    icon_style_options,
    normalize_border_shader_config,
    normalize_icon_style,
    normalize_text_preserve_config,
    normalize_text_extraction_method,
    resolve_icon_template,
    text_preserve_to_dict,
)
from gamemanager.ui.alpha_preview import composite_on_checkerboard, draw_checkerboard
from .settings import _bind_dialog_shortcut
from .template_management import TemplateGalleryDialog

try:
    from PIL import Image, ImageFilter, ImageOps
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageFilter = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


def _icon_style_gallery_entries() -> list[tuple[str, str, object | None]]:
    entries: list[tuple[str, str, object | None]] = [("none", "No Template", None)]
    for label, value in icon_style_options():
        key = str(value or "").strip()
        if not key or key == "none":
            continue
        spec = resolve_icon_template(key, circular_ring=False)
        entries.append((key, str(label), spec.path))
    return entries


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


def _normalize_image_bytes_for_canvas(payload: bytes) -> bytes:
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
    cutoutColorPicked = Signal(object)
    cutoutMaskMarkPoint = Signal(object)
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
        self._cutout_color_pick_mode = False
        self._cutout_mark_mode = "none"
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
        self._sync_pick_cursor()

    def seed_pick_mode(self) -> bool:
        return self._seed_pick_mode

    def set_cutout_color_pick_mode(self, enabled: bool) -> None:
        self._cutout_color_pick_mode = bool(enabled)
        self._sync_pick_cursor()

    def cutout_color_pick_mode(self) -> bool:
        return self._cutout_color_pick_mode

    def set_cutout_mark_mode(self, mode: str | None) -> None:
        normalized = str(mode or "none").strip().casefold()
        if normalized not in {"none", "add", "remove"}:
            normalized = "none"
        self._cutout_mark_mode = normalized
        self._sync_pick_cursor()

    def cutout_mark_mode(self) -> str:
        return self._cutout_mark_mode

    def set_text_mark_mode(self, mode: str | None) -> None:
        normalized = str(mode or "none").strip().casefold()
        if normalized not in {"none", "add", "remove"}:
            normalized = "none"
        self._text_mark_mode = normalized
        self._sync_pick_cursor()
        self.update()

    def text_mark_mode(self) -> str:
        return self._text_mark_mode

    def _sync_pick_cursor(self) -> None:
        if (
            self._seed_pick_mode
            or self._cutout_color_pick_mode
            or self._cutout_mark_mode != "none"
            or self._text_mark_mode != "none"
        ):
            self.setCursor(Qt.CursorShape.CrossCursor)
            return
        self.unsetCursor()

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
        if self._cutout_color_pick_mode and event.button() == Qt.MouseButton.LeftButton:
            source_point = self._canvas_point_to_source_image_point(event.position())
            image = self._pixmap.toImage()
            if not image.isNull():
                px = max(0, min(image.width() - 1, int(round(source_point.x()))))
                py = max(0, min(image.height() - 1, int(round(source_point.y()))))
                color = image.pixelColor(px, py)
                self.cutoutColorPicked.emit((color.red(), color.green(), color.blue()))
            self.set_cutout_color_pick_mode(False)
            event.accept()
            return
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
        if self._cutout_mark_mode != "none" and event.button() == Qt.MouseButton.LeftButton:
            source_point = self._canvas_point_to_source_image_point(event.position())
            nx = max(0.0, min(1.0, source_point.x() / max(1.0, float(self._pixmap.width()))))
            ny = max(0.0, min(1.0, source_point.y() / max(1.0, float(self._pixmap.height()))))
            self.cutoutMaskMarkPoint.emit((nx, ny))
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
            return

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
        self._cutout_picked_colors: list[dict[str, object]] = []
        self._cutout_pick_mode_active = False
        self._cutout_row_uid_counter = 1
        self._active_cutout_mark_row_id: int | None = None
        self._cutout_mark_mode = "none"
        self._cutout_mark_history: dict[
            int,
            dict[
                str,
                list[tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]],
            ],
        ] = {}
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
        self._canvas.cutoutColorPicked.connect(self._on_canvas_cutout_color_picked)
        self._canvas.cutoutMaskMarkPoint.connect(self._on_canvas_cutout_mark_point)
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
        self.border_gallery_btn = QPushButton("", self)
        self.border_gallery_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        )
        self.border_gallery_btn.setToolTip("Pick Template from Gallery")
        self.border_gallery_btn.clicked.connect(self._on_pick_border_template_from_gallery)
        template_row.addWidget(self.border_gallery_btn)
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

        self.cutout_pick_colors_container = QWidget(self)
        pick_colors_layout = QVBoxLayout(self.cutout_pick_colors_container)
        pick_colors_layout.setContentsMargins(0, 0, 0, 0)
        pick_colors_layout.setSpacing(4)
        pick_header = QHBoxLayout()
        pick_header.setContentsMargins(0, 0, 0, 0)
        pick_header.setSpacing(6)
        self.cutout_pick_add_btn = QPushButton("Add (eye-dropper)", self)
        self.cutout_pick_add_btn.clicked.connect(self._on_cutout_pick_add_clicked)
        pick_header.addWidget(self.cutout_pick_add_btn)
        self.cutout_pick_clear_btn = QPushButton("Clear", self)
        self.cutout_pick_clear_btn.clicked.connect(self._on_cutout_pick_clear_clicked)
        pick_header.addWidget(self.cutout_pick_clear_btn)
        pick_header.addStretch(1)
        pick_colors_layout.addLayout(pick_header)
        falloff_row = QHBoxLayout()
        falloff_row.setContentsMargins(0, 0, 0, 0)
        falloff_row.setSpacing(6)
        self.cutout_falloff_advanced_check = QCheckBox("Adv", self)
        self.cutout_falloff_advanced_check.toggled.connect(self._on_cutout_falloff_advanced_toggled)
        falloff_row.addWidget(self.cutout_falloff_advanced_check)
        self.cutout_curve_strength_label = QLabel("Curve:", self)
        falloff_row.addWidget(self.cutout_curve_strength_label)
        self.cutout_curve_strength_spin = QSpinBox(self)
        self.cutout_curve_strength_spin.setRange(0, 100)
        self.cutout_curve_strength_spin.setKeyboardTracking(False)
        self.cutout_curve_strength_spin.setValue(50)
        self.cutout_curve_strength_spin.valueChanged.connect(self._on_spinner_param_changed)
        self.cutout_curve_strength_spin.editingFinished.connect(self._on_cutout_params_changed)
        falloff_row.addWidget(self.cutout_curve_strength_spin)
        falloff_row.addStretch(1)
        pick_colors_layout.addLayout(falloff_row)
        self.cutout_pick_rows_widget = QWidget(self.cutout_pick_colors_container)
        self.cutout_pick_rows_layout = QVBoxLayout(self.cutout_pick_rows_widget)
        self.cutout_pick_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.cutout_pick_rows_layout.setSpacing(4)
        pick_colors_layout.addWidget(self.cutout_pick_rows_widget)
        self.cutout_mark_controls_container = QWidget(self.cutout_pick_colors_container)
        cutout_mark_row = QHBoxLayout(self.cutout_mark_controls_container)
        cutout_mark_row.setContentsMargins(0, 0, 0, 0)
        cutout_mark_row.setSpacing(5)
        self.cutout_mark_undo_btn = QPushButton("↺", self.cutout_mark_controls_container)
        self.cutout_mark_redo_btn = QPushButton("↻", self.cutout_mark_controls_container)
        self.cutout_mark_add_btn = QPushButton("Add", self.cutout_mark_controls_container)
        self.cutout_mark_remove_btn = QPushButton("Remove", self.cutout_mark_controls_container)
        self.cutout_mark_stop_btn = QPushButton("Stop", self.cutout_mark_controls_container)
        self.cutout_mark_undo_btn.clicked.connect(self._on_cutout_mark_undo)
        self.cutout_mark_redo_btn.clicked.connect(self._on_cutout_mark_redo)
        self.cutout_mark_add_btn.clicked.connect(self._on_cutout_mark_add_mode)
        self.cutout_mark_remove_btn.clicked.connect(self._on_cutout_mark_remove_mode)
        self.cutout_mark_stop_btn.clicked.connect(self._on_cutout_mark_stop_mode)
        cutout_mark_row.addWidget(self.cutout_mark_undo_btn)
        cutout_mark_row.addWidget(self.cutout_mark_redo_btn)
        cutout_mark_row.addWidget(self.cutout_mark_add_btn)
        cutout_mark_row.addWidget(self.cutout_mark_remove_btn)
        cutout_mark_row.addWidget(self.cutout_mark_stop_btn)
        self.cutout_mark_count_label = QLabel("", self.cutout_mark_controls_container)
        cutout_mark_row.addWidget(self.cutout_mark_count_label, 1)
        pick_colors_layout.addWidget(self.cutout_mark_controls_container)
        side.addWidget(self.cutout_pick_colors_container)

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

        self._set_cutout_picked_colors_from_params(initial_bg_removal_params or {})
        self._set_cutout_falloff_settings_from_params(initial_bg_removal_params or {})
        self._rebuild_cutout_pick_color_rows()
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

    def _on_pick_border_template_from_gallery(self) -> None:
        dialog = TemplateGalleryDialog(
            _icon_style_gallery_entries(),
            current_key=str(self.border_combo.currentData() or "none"),
            title="Select Template",
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = dialog.selected_key()
        idx = self.border_combo.findData(selected)
        if idx >= 0:
            self.border_combo.setCurrentIndex(idx)

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
        if self._canvas.cutout_color_pick_mode():
            self._set_cutout_color_pick_mode(False)
        if self._cutout_mark_mode != "none":
            self._set_cutout_mark_mode("none")
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

    def _set_cutout_picked_colors_from_params(self, params: dict[str, object]) -> None:
        normalized = normalize_background_removal_params(params)
        entries = normalized.get("picked_colors", [])
        items: list[dict[str, object]] = []
        self._cutout_mark_history = {}
        self._active_cutout_mark_row_id = None
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                color = entry.get("color")
                if not isinstance(color, (list, tuple)) or len(color) < 3:
                    continue
                try:
                    red = max(0, min(255, int(color[0])))
                    green = max(0, min(255, int(color[1])))
                    blue = max(0, min(255, int(color[2])))
                    tolerance = max(0, min(30, int(entry.get("tolerance", 10) or 10)))
                except (TypeError, ValueError):
                    continue
                scope = str(entry.get("scope", "global") or "global").strip().casefold()
                if scope not in {"global", "contig"}:
                    scope = "global"
                falloff = str(entry.get("falloff", "flat") or "flat").strip().casefold()
                if falloff not in {"flat", "lin", "smooth", "cos", "exp", "log", "gauss"}:
                    falloff = "flat"
                include_seeds_raw = entry.get("include_seeds")
                exclude_seeds_raw = entry.get("exclude_seeds")
                include_seeds: list[list[float]] = []
                exclude_seeds: list[list[float]] = []
                if isinstance(include_seeds_raw, list):
                    for seed in include_seeds_raw:
                        if not isinstance(seed, (list, tuple)) or len(seed) < 2:
                            continue
                        try:
                            sx = max(0.0, min(1.0, float(seed[0])))
                            sy = max(0.0, min(1.0, float(seed[1])))
                        except (TypeError, ValueError):
                            continue
                        packed = [sx, sy]
                        if packed not in include_seeds:
                            include_seeds.append(packed)
                if isinstance(exclude_seeds_raw, list):
                    for seed in exclude_seeds_raw:
                        if not isinstance(seed, (list, tuple)) or len(seed) < 2:
                            continue
                        try:
                            sx = max(0.0, min(1.0, float(seed[0])))
                            sy = max(0.0, min(1.0, float(seed[1])))
                        except (TypeError, ValueError):
                            continue
                        packed = [sx, sy]
                        if packed not in exclude_seeds:
                            exclude_seeds.append(packed)
                row_id = self._cutout_row_uid_counter
                self._cutout_row_uid_counter += 1
                item = {
                    "id": row_id,
                    "color": [red, green, blue],
                    "tolerance": tolerance,
                    "scope": scope,
                    "falloff": falloff,
                    "include_seeds": include_seeds,
                    "exclude_seeds": exclude_seeds,
                }
                if item not in items:
                    items.append(item)
                self._cutout_mark_history[row_id] = {"undo": [], "redo": []}
        self._cutout_picked_colors = items

    @staticmethod
    def _default_curve_strength_for_mode(mode: str) -> int:
        token = str(mode or "").strip().casefold()
        if token == "exp":
            return 35
        if token == "log":
            return 65
        if token == "gauss":
            return 45
        return 50

    @staticmethod
    def _cutout_mode_uses_curve_strength(mode: str) -> bool:
        return str(mode or "").strip().casefold() in {"exp", "log", "gauss"}

    def _set_cutout_falloff_settings_from_params(self, params: dict[str, object]) -> None:
        normalized = normalize_background_removal_params(params)
        blocked_adv = self.cutout_falloff_advanced_check.blockSignals(True)
        self.cutout_falloff_advanced_check.setChecked(
            bool(normalized.get("pick_colors_advanced", False))
        )
        self.cutout_falloff_advanced_check.blockSignals(blocked_adv)
        blocked_curve = self.cutout_curve_strength_spin.blockSignals(True)
        strength = int(normalized.get("pick_colors_curve_strength", 50) or 50)
        self.cutout_curve_strength_spin.setValue(max(0, min(100, strength)))
        self.cutout_curve_strength_spin.blockSignals(blocked_curve)
        self._sync_cutout_falloff_controls()

    def _cutout_any_curve_mode_entries(self) -> bool:
        for entry in self._cutout_picked_colors:
            mode = str(entry.get("falloff", "flat") or "flat")
            if self._cutout_mode_uses_curve_strength(mode):
                return True
        return False

    def _sync_cutout_falloff_controls(self) -> None:
        show_curve = (
            self.cutout_falloff_advanced_check.isChecked()
            and self._cutout_any_curve_mode_entries()
        )
        self.cutout_curve_strength_label.setVisible(show_curve)
        self.cutout_curve_strength_spin.setVisible(show_curve)
        self.cutout_curve_strength_spin.setEnabled(
            show_curve and not self._processing_in_progress
        )

    def _on_cutout_falloff_advanced_toggled(self, _checked: bool) -> None:
        if not self.cutout_falloff_advanced_check.isChecked():
            blocked = self.cutout_curve_strength_spin.blockSignals(True)
            self.cutout_curve_strength_spin.setValue(50)
            self.cutout_curve_strength_spin.blockSignals(blocked)
        self._sync_cutout_falloff_controls()
        self._on_cutout_params_changed()

    def _active_cutout_mark_entry(self) -> dict[str, object] | None:
        row_id = self._active_cutout_mark_row_id
        if row_id is None:
            return None
        for entry in self._cutout_picked_colors:
            if int(entry.get("id", -1)) == int(row_id):
                return entry
        return None

    def _cutout_mark_snapshot(
        self,
        entry: dict[str, object],
    ) -> tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]]:
        include_raw = entry.get("include_seeds", [])
        exclude_raw = entry.get("exclude_seeds", [])
        include = tuple(
            (float(seed[0]), float(seed[1]))
            for seed in include_raw
            if isinstance(seed, (list, tuple)) and len(seed) >= 2
        )
        exclude = tuple(
            (float(seed[0]), float(seed[1]))
            for seed in exclude_raw
            if isinstance(seed, (list, tuple)) and len(seed) >= 2
        )
        return (include, exclude)

    def _restore_cutout_mark_snapshot(
        self,
        entry: dict[str, object],
        snapshot: tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]],
    ) -> None:
        include, exclude = snapshot
        entry["include_seeds"] = [[float(x), float(y)] for x, y in include]
        entry["exclude_seeds"] = [[float(x), float(y)] for x, y in exclude]

    def _push_cutout_mark_undo_snapshot(
        self,
        row_id: int,
        snapshot: tuple[tuple[tuple[float, float], ...], tuple[tuple[float, float], ...]],
    ) -> None:
        history = self._cutout_mark_history.setdefault(row_id, {"undo": [], "redo": []})
        history["undo"].append(snapshot)
        if len(history["undo"]) > 256:
            del history["undo"][0]
        history["redo"].clear()

    def _set_cutout_mark_mode(self, mode: str) -> None:
        normalized = str(mode or "none").strip().casefold()
        if normalized not in {"none", "add", "remove"}:
            normalized = "none"
        self._cutout_mark_mode = normalized
        self._canvas.set_cutout_mark_mode(normalized)
        self.cutout_mark_add_btn.setText("Adding..." if normalized == "add" else "Add")
        self.cutout_mark_remove_btn.setText("Removing..." if normalized == "remove" else "Remove")
        self._update_cutout_mark_controls()
        self._refresh_cutout_status()

    def _update_cutout_mark_controls(self) -> None:
        entry = self._active_cutout_mark_entry()
        row_active = entry is not None and str(entry.get("scope", "global")) == "contig"
        self.cutout_mark_controls_container.setVisible(bool(row_active))
        if not row_active:
            self.cutout_mark_count_label.setText("")
            self.cutout_mark_undo_btn.setEnabled(False)
            self.cutout_mark_redo_btn.setEnabled(False)
            self.cutout_mark_add_btn.setEnabled(False)
            self.cutout_mark_remove_btn.setEnabled(False)
            self.cutout_mark_stop_btn.setEnabled(False)
            if self._cutout_mark_mode != "none":
                self._cutout_mark_mode = "none"
                self._canvas.set_cutout_mark_mode("none")
            return
        row_id = int(entry.get("id", -1))
        history = self._cutout_mark_history.setdefault(row_id, {"undo": [], "redo": []})
        include_count = len(entry.get("include_seeds", [])) if isinstance(entry.get("include_seeds"), list) else 0
        exclude_count = len(entry.get("exclude_seeds", [])) if isinstance(entry.get("exclude_seeds"), list) else 0
        self.cutout_mark_count_label.setText(f"Row {row_id}: Add {include_count} / Remove {exclude_count}")
        can_edit = not self._processing_in_progress
        self.cutout_mark_add_btn.setEnabled(can_edit)
        self.cutout_mark_remove_btn.setEnabled(can_edit)
        self.cutout_mark_stop_btn.setEnabled(can_edit)
        self.cutout_mark_undo_btn.setEnabled(can_edit and bool(history.get("undo")))
        self.cutout_mark_redo_btn.setEnabled(can_edit and bool(history.get("redo")))

    def _on_cutout_mark_select_row(self, row_id: int) -> None:
        self._active_cutout_mark_row_id = int(row_id)
        self._set_cutout_mark_mode("none")
        self._rebuild_cutout_pick_color_rows()
        self._update_cutout_mark_controls()

    def _on_cutout_mark_add_mode(self) -> None:
        self._set_cutout_mark_mode("none" if self._cutout_mark_mode == "add" else "add")

    def _on_cutout_mark_remove_mode(self) -> None:
        self._set_cutout_mark_mode("none" if self._cutout_mark_mode == "remove" else "remove")

    def _on_cutout_mark_stop_mode(self) -> None:
        self._set_cutout_mark_mode("none")

    def _on_cutout_mark_undo(self) -> None:
        entry = self._active_cutout_mark_entry()
        if entry is None:
            return
        row_id = int(entry.get("id", -1))
        history = self._cutout_mark_history.setdefault(row_id, {"undo": [], "redo": []})
        if not history["undo"]:
            return
        current = self._cutout_mark_snapshot(entry)
        snapshot = history["undo"].pop()
        history["redo"].append(current)
        if len(history["redo"]) > 256:
            del history["redo"][0]
        self._restore_cutout_mark_snapshot(entry, snapshot)
        self._rebuild_cutout_pick_color_rows()
        self._update_cutout_mark_controls()
        self._apply_processing_settings()

    def _on_cutout_mark_redo(self) -> None:
        entry = self._active_cutout_mark_entry()
        if entry is None:
            return
        row_id = int(entry.get("id", -1))
        history = self._cutout_mark_history.setdefault(row_id, {"undo": [], "redo": []})
        if not history["redo"]:
            return
        current = self._cutout_mark_snapshot(entry)
        snapshot = history["redo"].pop()
        history["undo"].append(current)
        if len(history["undo"]) > 256:
            del history["undo"][0]
        self._restore_cutout_mark_snapshot(entry, snapshot)
        self._rebuild_cutout_pick_color_rows()
        self._update_cutout_mark_controls()
        self._apply_processing_settings()

    def _upsert_cutout_mark_point(
        self,
        points: list[list[float]],
        point: tuple[float, float],
    ) -> None:
        x_new, y_new = point
        for idx, existing in enumerate(points):
            if not isinstance(existing, (list, tuple)) or len(existing) < 2:
                continue
            if abs(float(existing[0]) - x_new) <= 0.002 and abs(float(existing[1]) - y_new) <= 0.002:
                points[idx] = [x_new, y_new]
                return
        points.append([x_new, y_new])
        if len(points) > 512:
            del points[0]

    def _on_canvas_cutout_mark_point(self, value: object) -> None:
        if self._cutout_mark_mode not in {"add", "remove"}:
            return
        entry = self._active_cutout_mark_entry()
        if entry is None or str(entry.get("scope", "global")) != "contig":
            return
        try:
            x_val = float(value[0])  # type: ignore[index]
            y_val = float(value[1])  # type: ignore[index]
        except Exception:
            return
        point = (max(0.0, min(1.0, x_val)), max(0.0, min(1.0, y_val)))
        row_id = int(entry.get("id", -1))
        before = self._cutout_mark_snapshot(entry)
        if self._cutout_mark_mode == "add":
            include_points = entry.setdefault("include_seeds", [])
            if isinstance(include_points, list):
                self._upsert_cutout_mark_point(include_points, point)
        else:
            exclude_points = entry.setdefault("exclude_seeds", [])
            if isinstance(exclude_points, list):
                self._upsert_cutout_mark_point(exclude_points, point)
        after = self._cutout_mark_snapshot(entry)
        if after != before:
            self._push_cutout_mark_undo_snapshot(row_id, before)
        self._rebuild_cutout_pick_color_rows()
        self._update_cutout_mark_controls()
        self._apply_processing_settings()

    def _set_cutout_color_pick_mode(self, enabled: bool) -> None:
        self._cutout_pick_mode_active = bool(enabled)
        self._canvas.set_cutout_color_pick_mode(bool(enabled))
        self.cutout_pick_add_btn.setText("Picking..." if enabled else "Add (eye-dropper)")
        self._refresh_cutout_status()

    def _on_cutout_pick_add_clicked(self) -> None:
        if self._processing_in_progress or not self._cutout_method_enabled():
            return
        if self.selected_bg_removal_engine() != "pick_colors":
            idx = self.bg_removal_combo.findData("pick_colors")
            if idx >= 0:
                self.bg_removal_combo.setCurrentIndex(idx)
        if self._active_seed_pick_index is not None:
            self._active_seed_pick_index = None
            self._canvas.set_seed_pick_mode(False)
            self._refresh_seed_color_controls()
        if self._manual_text_mark_mode != "none":
            self._set_manual_mark_mode("none")
        if self._cutout_mark_mode != "none":
            self._set_cutout_mark_mode("none")
        self._set_cutout_color_pick_mode(not self._canvas.cutout_color_pick_mode())

    def _on_cutout_pick_clear_clicked(self) -> None:
        if not self._cutout_picked_colors:
            return
        self._set_cutout_color_pick_mode(False)
        self._cutout_picked_colors = []
        self._cutout_mark_history.clear()
        self._active_cutout_mark_row_id = None
        self._set_cutout_mark_mode("none")
        self._rebuild_cutout_pick_color_rows()
        self._apply_processing_settings()

    def _on_canvas_cutout_color_picked(self, value: object) -> None:
        try:
            red = max(0, min(255, int(value[0])))  # type: ignore[index]
            green = max(0, min(255, int(value[1])))  # type: ignore[index]
            blue = max(0, min(255, int(value[2])))  # type: ignore[index]
        except Exception:
            self._set_cutout_color_pick_mode(False)
            return
        self._set_cutout_color_pick_mode(False)
        row_id = self._cutout_row_uid_counter
        self._cutout_row_uid_counter += 1
        entry = {
            "id": row_id,
            "color": [red, green, blue],
            "tolerance": 10,
            "scope": "global",
            "falloff": "flat",
            "include_seeds": [],
            "exclude_seeds": [],
        }
        if entry not in self._cutout_picked_colors:
            self._cutout_picked_colors.append(entry)
            self._cutout_mark_history[row_id] = {"undo": [], "redo": []}
        self._sync_cutout_falloff_controls()
        self._rebuild_cutout_pick_color_rows()
        self._apply_processing_settings()

    def _rebuild_cutout_pick_color_rows(self) -> None:
        while self.cutout_pick_rows_layout.count() > 0:
            item = self.cutout_pick_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for idx, entry in enumerate(self._cutout_picked_colors):
            row_widget = QWidget(self.cutout_pick_rows_widget)
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)
            color = entry.get("color", [0, 0, 0])
            red = int(color[0]) if isinstance(color, (list, tuple)) and len(color) > 0 else 0
            green = int(color[1]) if isinstance(color, (list, tuple)) and len(color) > 1 else 0
            blue = int(color[2]) if isinstance(color, (list, tuple)) and len(color) > 2 else 0
            swatch_btn = QPushButton(f"#{red:02X}{green:02X}{blue:02X}", row_widget)
            swatch_btn.setMinimumWidth(98)
            swatch_btn.setStyleSheet(
                "QPushButton { "
                f"background: rgb({red}, {green}, {blue}); "
                f"color: {'#000000' if ((red * 0.299) + (green * 0.587) + (blue * 0.114)) >= 140.0 else '#ffffff'}; "
                "border: 1px solid #5b5b5b; }"
            )
            swatch_btn.clicked.connect(lambda _checked=False, row_idx=idx: self._on_cutout_pick_color_button_clicked(row_idx))
            row_layout.addWidget(swatch_btn)
            row_layout.addWidget(QLabel("Tol:", row_widget))
            tol_spin = QSpinBox(row_widget)
            tol_spin.setRange(0, 30)
            tol_spin.setValue(max(0, min(30, int(entry.get("tolerance", 10) or 10))))
            tol_spin.setKeyboardTracking(False)
            tol_spin.valueChanged.connect(
                lambda value, row_idx=idx: self._on_cutout_pick_tolerance_changed(row_idx, value)
            )
            tol_spin.editingFinished.connect(self._on_cutout_params_changed)
            row_layout.addWidget(tol_spin)
            scope_combo = QComboBox(row_widget)
            scope_combo.addItem("G", "global")
            scope_combo.addItem("C", "contig")
            scope_value = str(entry.get("scope", "global") or "global").strip().casefold()
            scope_idx = scope_combo.findData(scope_value if scope_value in {"global", "contig"} else "global")
            if scope_idx >= 0:
                scope_combo.setCurrentIndex(scope_idx)
            scope_combo.currentIndexChanged.connect(
                lambda _value, row_idx=idx, combo=scope_combo: self._on_cutout_pick_scope_changed(
                    row_idx,
                    str(combo.currentData() or "global"),
                )
            )
            row_layout.addWidget(scope_combo)
            badge = QLabel("", row_widget)
            include_n = len(entry.get("include_seeds", [])) if isinstance(entry.get("include_seeds"), list) else 0
            exclude_n = len(entry.get("exclude_seeds", [])) if isinstance(entry.get("exclude_seeds"), list) else 0
            if scope_value == "contig" and (include_n > 0 or exclude_n > 0):
                badge.setText("C*")
                badge.setToolTip(f"Contig marks: Add {include_n} / Remove {exclude_n}")
                badge.setStyleSheet(
                    "QLabel { color: #f0c14b; font-weight: 700; min-width: 18px; }"
                )
            else:
                badge.setText("")
                badge.setMinimumWidth(18)
            row_layout.addWidget(badge)
            falloff_combo = QComboBox(row_widget)
            falloff_combo.addItem("Flat", "flat")
            falloff_combo.addItem("Lin", "lin")
            falloff_combo.addItem("Smooth", "smooth")
            falloff_combo.addItem("Cos", "cos")
            falloff_combo.addItem("Exp", "exp")
            falloff_combo.addItem("Log", "log")
            falloff_combo.addItem("Gauss", "gauss")
            falloff_value = str(entry.get("falloff", "flat") or "flat").strip().casefold()
            falloff_idx = falloff_combo.findData(
                falloff_value if falloff_value in {"flat", "lin", "smooth", "cos", "exp", "log", "gauss"} else "flat"
            )
            if falloff_idx >= 0:
                falloff_combo.setCurrentIndex(falloff_idx)
            falloff_combo.currentIndexChanged.connect(
                lambda _value, row_idx=idx, combo=falloff_combo: self._on_cutout_pick_falloff_changed(
                    row_idx,
                    str(combo.currentData() or "flat"),
                )
            )
            row_layout.addWidget(falloff_combo)
            edit_btn = QPushButton("Edit", row_widget)
            row_id = int(entry.get("id", -1))
            is_contig = str(entry.get("scope", "global") or "global").strip().casefold() == "contig"
            edit_btn.setEnabled(is_contig)
            if is_contig:
                edit_btn.setToolTip(f"Edit contiguous marks (Add {include_n} / Remove {exclude_n})")
            if is_contig and self._active_cutout_mark_row_id == row_id:
                edit_btn.setText("Editing...")
            edit_btn.clicked.connect(
                lambda _checked=False, rid=row_id: self._on_cutout_mark_select_row(rid)
            )
            row_layout.addWidget(edit_btn)
            remove_btn = QPushButton("X", row_widget)
            remove_btn.setToolTip("Remove picked color")
            remove_btn.setFixedWidth(28)
            remove_btn.setStyleSheet(
                "QPushButton { background: #8b1c1c; color: #ffffff; border: 1px solid #5b5b5b; font-weight: 700; }"
            )
            remove_btn.clicked.connect(lambda _checked=False, row_idx=idx: self._on_cutout_pick_remove_row(row_idx))
            row_layout.addWidget(remove_btn)
            row_layout.addStretch(1)
            self.cutout_pick_rows_layout.addWidget(row_widget)
        self.cutout_pick_clear_btn.setEnabled(bool(self._cutout_picked_colors))
        self.cutout_pick_rows_layout.addStretch(1)
        self._sync_cutout_falloff_controls()
        self._update_cutout_mark_controls()

    def _on_cutout_pick_color_button_clicked(self, index: int) -> None:
        if index < 0 or index >= len(self._cutout_picked_colors):
            return
        color = self._cutout_picked_colors[index].get("color", [0, 0, 0])
        red = int(color[0]) if isinstance(color, (list, tuple)) and len(color) > 0 else 0
        green = int(color[1]) if isinstance(color, (list, tuple)) and len(color) > 1 else 0
        blue = int(color[2]) if isinstance(color, (list, tuple)) and len(color) > 2 else 0
        selected = QColorDialog.getColor(QColor(red, green, blue), self, "Pick Cutout Color")
        if not selected.isValid():
            return
        self._cutout_picked_colors[index]["color"] = [
            int(selected.red()),
            int(selected.green()),
            int(selected.blue()),
        ]
        self._rebuild_cutout_pick_color_rows()
        self._apply_processing_settings()

    def _on_cutout_pick_tolerance_changed(self, index: int, value: int) -> None:
        if index < 0 or index >= len(self._cutout_picked_colors):
            return
        self._cutout_picked_colors[index]["tolerance"] = max(0, min(30, int(value)))
        self._on_spinner_param_changed()

    def _on_cutout_pick_scope_changed(self, index: int, scope_value: str) -> None:
        if index < 0 or index >= len(self._cutout_picked_colors):
            return
        normalized = str(scope_value or "global").strip().casefold()
        if normalized not in {"global", "contig"}:
            normalized = "global"
        self._cutout_picked_colors[index]["scope"] = normalized
        row_id = int(self._cutout_picked_colors[index].get("id", -1))
        if normalized != "contig" and self._active_cutout_mark_row_id == row_id:
            self._active_cutout_mark_row_id = None
            self._set_cutout_mark_mode("none")
        self._rebuild_cutout_pick_color_rows()
        self._on_cutout_params_changed()

    def _on_cutout_pick_falloff_changed(self, index: int, falloff_value: str) -> None:
        if index < 0 or index >= len(self._cutout_picked_colors):
            return
        falloff = str(falloff_value or "flat").strip().casefold()
        if falloff not in {"flat", "lin", "smooth", "cos", "exp", "log", "gauss"}:
            falloff = "flat"
        self._cutout_picked_colors[index]["falloff"] = falloff
        if (
            self._cutout_mode_uses_curve_strength(falloff)
            and not self.cutout_falloff_advanced_check.isChecked()
        ):
            self.cutout_curve_strength_spin.setValue(self._default_curve_strength_for_mode(falloff))
        self._sync_cutout_falloff_controls()
        self._on_cutout_params_changed()

    def _on_cutout_pick_remove_row(self, index: int) -> None:
        if index < 0 or index >= len(self._cutout_picked_colors):
            return
        row_id = int(self._cutout_picked_colors[index].get("id", -1))
        if self._active_cutout_mark_row_id == row_id:
            self._active_cutout_mark_row_id = None
            self._set_cutout_mark_mode("none")
        if row_id in self._cutout_mark_history:
            del self._cutout_mark_history[row_id]
        del self._cutout_picked_colors[index]
        self._rebuild_cutout_pick_color_rows()
        self._apply_processing_settings()

    def _set_manual_mark_mode(self, mode: str) -> None:
        normalized = str(mode or "none").strip().casefold()
        if normalized not in {"none", "add", "remove"}:
            normalized = "none"
        if normalized != "none" and self._canvas.cutout_color_pick_mode():
            self._set_cutout_color_pick_mode(False)
        if normalized != "none" and self._cutout_mark_mode != "none":
            self._set_cutout_mark_mode("none")
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
        pick_colors_enabled = (
            not busy
            and self._cutout_method_enabled()
            and self.selected_bg_removal_engine() == "pick_colors"
        )
        self.cutout_pick_add_btn.setEnabled(pick_colors_enabled)
        self.cutout_pick_clear_btn.setEnabled(pick_colors_enabled and bool(self._cutout_picked_colors))
        self.cutout_falloff_advanced_check.setEnabled(pick_colors_enabled)
        for button in self._seed_swatch_buttons:
            button.setEnabled(not busy and self._text_method_enabled())
        for idx in range(self.cutout_pick_rows_layout.count()):
            item = self.cutout_pick_rows_layout.itemAt(idx)
            widget = item.widget()
            if widget is not None:
                widget.setEnabled(pick_colors_enabled)
        self.manual_mark_undo_btn.setEnabled(False)
        self.manual_mark_redo_btn.setEnabled(False)
        self.manual_mark_add_btn.setEnabled(not busy and self._text_method_enabled())
        self.manual_mark_remove_btn.setEnabled(not busy and self._text_method_enabled())
        self.manual_mark_stop_btn.setEnabled(not busy and self._text_method_enabled())
        if busy and self._canvas.cutout_color_pick_mode():
            self._set_cutout_color_pick_mode(False)
        if busy and self._cutout_mark_mode != "none":
            self._set_cutout_mark_mode("none")
        self._sync_cutout_falloff_controls()
        self._update_cutout_mark_controls()
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
        if self._canvas.cutout_color_pick_mode():
            self.cutout_status_label.setText("Click image to add a cutout color.")
            return
        if self._cutout_mark_mode == "add":
            self.cutout_status_label.setText("Contig Add mode: click image to include matching regions.")
            return
        if self._cutout_mark_mode == "remove":
            self.cutout_status_label.setText("Contig Remove mode: click image to exclude matching regions.")
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
        if show_cutout and engine == "pick_colors" and not self._cutout_picked_colors:
            self.cutout_status_label.setText("Remove Colors mode active. Add at least one color.")
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

    def _shutdown_processing_thread(
        self,
        *,
        timeout_ms: int = 2500,
        allow_terminate: bool = False,
    ) -> None:
        thread = self._processing_thread
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
        self._processing_thread = None
        self._processing_worker = None
        self._processing_in_progress = False
        self._pending_processing = False
        self._apply_after_processing = False
        self._canvas.set_async_processing_busy(False)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._shutdown_processing_thread(timeout_ms=2200, allow_terminate=True)
        super().closeEvent(event)

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
        cutout_engine = self.selected_bg_removal_engine() if template_enabled else "none"
        cutout_enabled = template_enabled and cutout_engine != "none"
        cutout_advanced_enabled = cutout_enabled and cutout_engine in {"rembg", "bria_rmbg"}
        cutout_pick_colors_enabled = cutout_enabled and cutout_engine == "pick_colors"
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
        self.alpha_matting_check.setEnabled(cutout_advanced_enabled)
        self.fg_threshold_spin.setEnabled(cutout_advanced_enabled)
        self.bg_threshold_spin.setEnabled(cutout_advanced_enabled)
        self.erode_spin.setEnabled(cutout_advanced_enabled)
        self.edge_feather_spin.setEnabled(cutout_advanced_enabled)
        self.post_process_check.setEnabled(cutout_advanced_enabled)
        self.cutout_pick_add_btn.setEnabled(cutout_pick_colors_enabled and not self._processing_in_progress)
        self.cutout_pick_clear_btn.setEnabled(
            cutout_pick_colors_enabled
            and not self._processing_in_progress
            and bool(self._cutout_picked_colors)
        )
        self.cutout_falloff_advanced_check.setEnabled(
            cutout_pick_colors_enabled and not self._processing_in_progress
        )

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
        if not cutout_pick_colors_enabled and self._canvas.cutout_color_pick_mode():
            self._set_cutout_color_pick_mode(False)
        if not cutout_pick_colors_enabled and self._cutout_mark_mode != "none":
            self._set_cutout_mark_mode("none")

        self.shader_controls.setVisible(template_enabled)
        self.cutout_label.setVisible(template_enabled)
        self.bg_removal_combo.setVisible(template_enabled)
        self.cutout_advanced_container.setVisible(cutout_advanced_enabled)
        self.cutout_pick_colors_container.setVisible(cutout_pick_colors_enabled)
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
        self._sync_cutout_falloff_controls()
        self._update_cutout_mark_controls()
        self._refresh_seed_color_controls()
        self._refresh_manual_mark_count_label()
        self._update_roi_label()
        self._update_manual_history_buttons()

    def selected_bg_removal_engine(self) -> str:
        if not self._template_enabled():
            return "none"
        return normalize_background_removal_engine(
            str(self.bg_removal_combo.currentData() or "none")
        )

    def selected_bg_removal_params(self) -> dict[str, object]:
        advanced = bool(self.cutout_falloff_advanced_check.isChecked())
        curve_strength = int(self.cutout_curve_strength_spin.value()) if advanced else 50
        picked_rows: list[dict[str, object]] = []
        for entry in self._cutout_picked_colors:
            color = entry.get("color", [0, 0, 0])
            scope = str(entry.get("scope", "global") or "global").strip().casefold()
            if scope not in {"global", "contig"}:
                scope = "global"
            falloff = str(entry.get("falloff", "flat") or "flat").strip().casefold()
            if falloff not in {"flat", "lin", "smooth", "cos", "exp", "log", "gauss"}:
                falloff = "flat"
            include_seeds = (
                list(entry.get("include_seeds", []))
                if isinstance(entry.get("include_seeds"), list)
                else []
            )
            exclude_seeds = (
                list(entry.get("exclude_seeds", []))
                if isinstance(entry.get("exclude_seeds"), list)
                else []
            )
            picked_rows.append(
                {
                    "color": list(color) if isinstance(color, (list, tuple)) else [0, 0, 0],
                    "tolerance": int(entry.get("tolerance", 10) or 10),
                    "scope": scope,
                    "falloff": falloff,
                    "include_seeds": include_seeds,
                    "exclude_seeds": exclude_seeds,
                }
            )
        payload: dict[str, object] = {
            "alpha_matting": self.alpha_matting_check.isChecked(),
            "alpha_matting_foreground_threshold": int(self.fg_threshold_spin.value()),
            "alpha_matting_background_threshold": int(self.bg_threshold_spin.value()),
            "alpha_matting_erode_size": int(self.erode_spin.value()),
            "alpha_edge_feather": int(self.edge_feather_spin.value()),
            "post_process_mask": self.post_process_check.isChecked(),
            "picked_colors": picked_rows,
            "pick_colors_use_hsv": True,
            "pick_colors_tolerance_mode": "max",
            "pick_colors_falloff": "flat",
            "pick_colors_curve_strength": curve_strength,
            "pick_colors_advanced": advanced,
        }
        return normalize_background_removal_params(payload)

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
        self.source_browse_btn.setToolTip("Choose source image\nShortcut: Ctrl+O")
        source_row.addWidget(self.source_browse_btn)
        layout.addLayout(source_row)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("Output ICO:", self))
        self.output_edit = QLineEdit(self)
        self.output_edit.setPlaceholderText("Choose output .ico path...")
        output_row.addWidget(self.output_edit, 1)
        self.output_browse_btn = QPushButton("Save As...", self)
        self.output_browse_btn.clicked.connect(self._on_browse_output)
        self.output_browse_btn.setToolTip("Choose output .ico path\nShortcut: Ctrl+Shift+S")
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
        self.icon_style_gallery_btn = QPushButton("", self)
        self.icon_style_gallery_btn.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView)
        )
        self.icon_style_gallery_btn.setToolTip("Pick Template from Gallery\nShortcut: Alt+G")
        self.icon_style_gallery_btn.clicked.connect(self._on_pick_icon_style_from_gallery)
        style_row.addWidget(self.icon_style_gallery_btn)
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
        self.adjust_btn.setToolTip("Open framing dialog\nShortcut: Alt+F")
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
        self.convert_btn.setToolTip("Convert to ICO\nShortcut: Ctrl+Enter")
        self.close_btn.setToolTip("Close\nShortcut: Esc")
        buttons.addButton(self.convert_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.close_btn, QDialogButtonBox.ButtonRole.RejectRole)
        self.convert_btn.clicked.connect(self._on_convert)
        self.close_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(900, 240)
        self._sync_template_dependents()
        _bind_dialog_shortcut(self, "Ctrl+O", self._on_browse_source)
        _bind_dialog_shortcut(self, "Ctrl+Shift+S", self._on_browse_output)
        _bind_dialog_shortcut(self, "Alt+G", self._on_pick_icon_style_from_gallery)
        _bind_dialog_shortcut(self, "Alt+F", self._on_adjust_framing)
        _bind_dialog_shortcut(self, "Ctrl+Return", self._on_convert)
        _bind_dialog_shortcut(self, "Ctrl+Enter", self._on_convert)
        _bind_dialog_shortcut(self, "F1", self._show_shortcuts)

    def _current_icon_style(self) -> str:
        return str(self.icon_style_combo.currentData() or "none")

    def _show_shortcuts(self) -> None:
        QMessageBox.information(
            self,
            "Icon Converter Shortcuts",
            "\n".join(
                [
                    "Ctrl+O - Browse source image",
                    "Ctrl+Shift+S - Pick output path",
                    "Alt+G - Open template gallery",
                    "Alt+F - Adjust framing",
                    "Ctrl+Enter - Convert",
                    "Esc - Close",
                    "F1 - Show Shortcuts",
                ]
            ),
        )

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

    def _on_pick_icon_style_from_gallery(self) -> None:
        dialog = TemplateGalleryDialog(
            _icon_style_gallery_entries(),
            current_key=self._current_icon_style(),
            title="Select Template",
            parent=self,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        selected = dialog.selected_key()
        idx = self.icon_style_combo.findData(selected)
        if idx >= 0:
            self.icon_style_combo.setCurrentIndex(idx)

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


__all__ = [
    "BorderShaderControls",
    "BorderShaderDialog",
    "FramingProcessingWorker",
    "IconConverterDialog",
    "IconFrameCanvas",
    "IconFramingDialog",
    "SeedColorButton",
]
