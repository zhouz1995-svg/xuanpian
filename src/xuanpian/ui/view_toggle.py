"""Segmented grid/list view toggle for the main toolbar."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QPushButton,
    QWidget,
)


class ViewToggle(QWidget):
    """Two-option segmented control switching between grid and list."""

    view_changed = pyqtSignal(str)

    _OPTIONS = (("grid", "网格"), ("list", "列表"))

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("viewToggle")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(0)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QPushButton] = {}
        for key, label in self._OPTIONS:
            button = QPushButton(label, self)
            button.setCheckable(True)
            button.setProperty("viewOption", key)
            self._group.addButton(button)
            layout.addWidget(button)
            self._buttons[key] = button

        self._current = self._OPTIONS[0][0]
        self._buttons[self._current].setChecked(True)
        self._group.buttonClicked.connect(self._on_clicked)

    def _on_clicked(self, button: QPushButton) -> None:
        key = str(button.property("viewOption"))
        if key == self._current:
            return
        self._current = key
        self.view_changed.emit(key)

    def set_view(self, key: str) -> None:
        if key not in self._buttons:
            raise ValueError(f"unknown view key: {key!r}")
        self._current = key
        self._buttons[key].setChecked(True)

    def current_view(self) -> str:
        return self._current
