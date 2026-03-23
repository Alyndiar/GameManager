from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap


def draw_checkerboard(
    painter: QPainter,
    rect: QRectF,
    *,
    tile_size: int = 10,
    color_a: QColor | None = None,
    color_b: QColor | None = None,
) -> None:
    tile = max(2, int(tile_size))
    first = color_a or QColor(76, 76, 76)
    second = color_b or QColor(52, 52, 52)
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    left = int(rect.left())
    top = int(rect.top())
    right = int(rect.right())
    bottom = int(rect.bottom())
    y = top
    row = 0
    while y <= bottom:
        x = left
        col = row % 2
        while x <= right:
            painter.setBrush(first if (col % 2 == 0) else second)
            painter.drawRect(x, y, tile, tile)
            x += tile
            col += 1
        y += tile
        row += 1
    painter.restore()


def composite_on_checkerboard(
    source: QPixmap,
    *,
    width: int,
    height: int,
    keep_aspect: bool = True,
) -> QPixmap:
    out = QPixmap(max(1, int(width)), max(1, int(height)))
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    draw_checkerboard(painter, QRectF(0.0, 0.0, float(out.width()), float(out.height())))
    if not source.isNull():
        if keep_aspect:
            scaled = source.scaled(
                out.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        else:
            scaled = source.scaled(
                out.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        x = (out.width() - scaled.width()) // 2
        y = (out.height() - scaled.height()) // 2
        painter.drawPixmap(x, y, scaled)
    painter.end()
    return out

