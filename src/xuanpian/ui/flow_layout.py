from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout, QLayoutItem, QWidget


class FlowLayout(QLayout):
    """Arrange fixed-size child widgets left-to-right and wrap by width."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 8) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, dry_run=False)

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(),
            margins.top() + margins.bottom(),
        )

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def _do_layout(self, rect: QRect, dry_run: bool) -> int:
        margins = self.contentsMargins()
        left = rect.x() + margins.left()
        top = rect.y() + margins.top()
        available = max(0, rect.width() - margins.left() - margins.right())
        if not self._items or available == 0:
            return margins.top() + margins.bottom()

        x = left
        y = top
        row_height = 0
        for item in self._items:
            hint = item.sizeHint()
            width = min(hint.width(), available)
            if x > left and x + width > left + available:
                x = left
                y += row_height + self._spacing
                row_height = 0
            if not dry_run:
                item.setGeometry(QRect(x, y, width, hint.height()))
            x += width + self._spacing
            row_height = max(row_height, hint.height())
        return (y - rect.y()) + row_height + margins.bottom()
