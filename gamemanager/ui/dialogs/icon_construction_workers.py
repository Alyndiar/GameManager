from __future__ import annotations

from io import BytesIO

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QPushButton

from gamemanager.services.background_removal import (
    normalize_background_removal_engine,
    normalize_background_removal_params,
    remove_background_bytes,
)
from gamemanager.services.icon_pipeline import (
    build_text_extraction_alpha_mask,
    build_text_extraction_overlay,
    normalize_text_preserve_config,
    text_preserve_to_dict,
)
from .shared import normalize_image_bytes_for_canvas as _normalize_image_bytes_for_canvas

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]


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
