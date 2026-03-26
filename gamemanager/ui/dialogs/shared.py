from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDialog


def bind_dialog_shortcut(
    dialog: QDialog,
    sequence: str,
    callback: Callable[[], None],
) -> QAction:
    action = QAction(dialog)
    action.setShortcut(QKeySequence(sequence))
    action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
    action.triggered.connect(callback)
    dialog.addAction(action)
    return action


def normalize_image_bytes_for_canvas(payload: bytes) -> bytes:
    if not payload:
        return payload
    try:
        from PIL import Image, ImageOps

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


def icon_style_gallery_entries() -> list[tuple[str, str, Path | None]]:
    from gamemanager.services.icon_pipeline import icon_style_options, resolve_icon_template

    entries: list[tuple[str, str, Path | None]] = [("none", "No Template", None)]
    for label, value in icon_style_options():
        key = str(value or "").strip()
        if not key or key == "none":
            continue
        spec = resolve_icon_template(key, circular_ring=False)
        entries.append((key, str(label), spec.path))
    return entries

