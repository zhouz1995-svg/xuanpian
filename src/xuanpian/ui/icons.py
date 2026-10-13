from __future__ import annotations

from importlib.resources import files

from PyQt6.QtCore import QByteArray, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer


_SVG_FRAGMENTS = {
    "panel": '<path d="M3 4h18v16H3zM9 4v16"/>',
    "folder": (
        '<path d="M3 6.5h7l2 2h9v9.5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
    ),
    "analyze": (
        '<path d="m12 3 1.3 4.1L17 9l-3.7 1.9L12 15l-1.3-4.1L7 9l3.7-1.9z"/>'
        '<path d="M5 16l.8 2.2L8 19l-2.2.8L5 22l-.8-2.2L2 19l2.2-.8z"/>'
    ),
    "export": '<path d="M12 3v12m0-12 4 4m-4-4L8 7M5 13v6h14v-6"/>',
    "back": '<path d="m15 5-7 7 7 7"/>',
    "pick": '<path d="m5 12 4 4L19 6"/>',
    "pending": '<circle cx="12" cy="12" r="9"/><path d="M12 8v5m0 3h.01"/>',
    "reject": '<path d="M6 6l12 12M18 6 6 18"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
    "undo": '<path d="M9 7 4 12l5 5M5 12h8a6 6 0 0 1 6 6"/>',
    "rotate_left": '<path d="M9 7 4 12l5 5M5 12h8a6 6 0 0 1 6 6"/>',
    "rotate_right": '<path d="m15 7 5 5-5 5m4-5h-8a6 6 0 0 0-6 6"/>',
    "zoom_out": '<path d="M5 12h14"/>',
    "fit": '<path d="M8 3H3v5m13-5h5v5M8 21H3v-5m13 5h5v-5"/>',
    "actual": '<path d="M8 5h8v14H8z"/>',
    "zoom_in": '<path d="M12 5v14M5 12h14"/>',
    "tag": (
        '<path d="M4 4h7l9 9-7 7-9-9z"/>'
        '<circle cx="8.5" cy="8.5" r="1.6"/>'
    ),
}


def application_icon() -> QIcon:
    """Load the application icon from package data, independent of cwd."""
    data = files("src.xuanpian.resources").joinpath(
        "icons/app-icon.png"
    ).read_bytes()
    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        raise RuntimeError("application icon is unreadable")
    return QIcon(pixmap)


def ui_icon(name: str, *, color: str = "#AEB6C2", size: int = 18) -> QIcon:
    """Render one of the built-in line icons at a deterministic size."""
    if size <= 0:
        raise ValueError("icon size must be positive")
    fragment = _SVG_FRAGMENTS.get(name)
    if fragment is None:
        raise KeyError(f"unknown UI icon: {name}")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="1.8" '
        f'stroke-linecap="round" stroke-linejoin="round">{fragment}</svg>'
    )
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)
