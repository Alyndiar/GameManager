from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QVBoxLayout, QWidget, QLayout


ICON_SIZE_PREVIEW_ORDER: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)


class IconSizePreviewDialog(QDialog):
    def __init__(
        self,
        payloads_by_size: dict[int, bytes],
        parent: QWidget | None = None,
        *,
        title: str = "Icon Size Preview",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setStyleSheet("QDialog { background-color: #000000; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        root.addLayout(row, 1)

        for size in ICON_SIZE_PREVIEW_ORDER:
            image_label = QLabel(self)
            image_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
            image_label.setFixedSize(int(size), int(size))
            pixmap = QPixmap()
            payload = payloads_by_size.get(int(size), b"")
            if payload and pixmap.loadFromData(payload):
                image_label.setPixmap(pixmap)
            row.addWidget(image_label, 0, Qt.AlignmentFlag.AlignTop)
        self.adjustSize()
