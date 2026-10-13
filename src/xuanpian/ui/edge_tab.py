from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QFont, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QPushButton, QWidget

from src.xuanpian.ui.theme import ACCENT, BORDER, ELEVATED, MUTED, SURFACE

_EDGE_TAB_WIDTH = 20
_EDGE_TAB_HEIGHT = 76
_CORNER_RADIUS = 7.0


class EdgeTab(QPushButton):
    """Vertical edge tab used to collapse and expand a side panel.

    The tab stays attached to the panel edge that faces the main content:
    ``rounded_side="left"`` suits a tab sitting on a panel's right edge
    (filter sidebar), while ``rounded_side="right"`` suits a tab sitting on
    a panel's left edge (preview info panel). The rounded side is the free
    edge; the other side stays square against the panel border.
    """

    def __init__(
        self,
        text: str,
        parent: QWidget | None = None,
        *,
        rounded_side: str = "left",
    ) -> None:
        if rounded_side not in ("left", "right"):
            raise ValueError("rounded_side must be 'left' or 'right'")
        super().__init__(text, parent)
        self._rounded_side = rounded_side
        self.setFixedSize(_EDGE_TAB_WIDTH, _EDGE_TAB_HEIGHT)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = _CORNER_RADIUS
        path = QPainterPath()
        if self._rounded_side == "left":
            path.moveTo(rect.right(), rect.top())
            path.lineTo(rect.left() + radius, rect.top())
            path.quadTo(
                rect.left(),
                rect.top(),
                rect.left(),
                rect.top() + radius,
            )
            path.lineTo(rect.left(), rect.bottom() - radius)
            path.quadTo(
                rect.left(),
                rect.bottom(),
                rect.left() + radius,
                rect.bottom(),
            )
            path.lineTo(rect.right(), rect.bottom())
        else:
            path.moveTo(rect.left(), rect.top())
            path.lineTo(rect.right() - radius, rect.top())
            path.quadTo(
                rect.right(),
                rect.top(),
                rect.right(),
                rect.top() + radius,
            )
            path.lineTo(rect.right(), rect.bottom() - radius)
            path.quadTo(
                rect.right(),
                rect.bottom(),
                rect.right() - radius,
                rect.bottom(),
            )
            path.lineTo(rect.left(), rect.bottom())
        path.closeSubpath()
        hovered = self.underMouse()
        painter.setPen(QPen(ACCENT if hovered else BORDER, 1))
        painter.setBrush(ELEVATED if hovered else SURFACE)
        painter.drawPath(path)
        painter.save()
        painter.translate(rect.center())
        painter.rotate(-90)
        font = QFont(painter.font())
        font.setPointSize(10)
        painter.setFont(font)
        painter.setPen(ACCENT if hovered else MUTED)
        text_rect = QRectF(
            -rect.height() / 2,
            -rect.width() / 2,
            rect.height(),
            rect.width(),
        )
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignCenter,
            self.text(),
        )
        painter.restore()
