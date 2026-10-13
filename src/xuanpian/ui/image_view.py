from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter, QPixmap, QWheelEvent
from PyQt6.QtWidgets import QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QWidget


class ImageView(QGraphicsView):
    """Zoomable, pannable image view whose transforms never touch source files."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._rotation = 0
        self._fit_mode = True
        self.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.Antialiasing
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse
        )
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setBackgroundBrush(Qt.GlobalColor.black)

    @property
    def zoom_level(self) -> float:
        return self.transform().m11()

    @property
    def rotation(self) -> int:
        return self._rotation

    def set_pixmap(self, pixmap: QPixmap, rotation: int = 0) -> None:
        self._scene.clear()
        self._pixmap_item = self._scene.addPixmap(pixmap)
        self._pixmap_item.setTransformOriginPoint(
            self._pixmap_item.boundingRect().center()
        )
        self._rotation = rotation % 360
        self._pixmap_item.setRotation(self._rotation)
        self._scene.setSceneRect(self._pixmap_item.sceneBoundingRect())
        self.zoom_to_fit()

    def clear_image(self, message: str = "") -> None:
        self._scene.clear()
        self._pixmap_item = None
        if message:
            item = self._scene.addText(message)
            item.setDefaultTextColor(Qt.GlobalColor.lightGray)
            self._scene.setSceneRect(item.boundingRect())

    def zoom(self, factor: float) -> None:
        target = self.zoom_level * factor
        if 0.05 <= target <= 20.0:
            self.scale(factor, factor)
            self._fit_mode = False

    def zoom_in(self) -> None:
        self.zoom(1.2)

    def zoom_out(self) -> None:
        self.zoom(1 / 1.2)

    def zoom_to_fit(self) -> None:
        if self._pixmap_item is None:
            return
        self.resetTransform()
        self.fitInView(
            self._pixmap_item.sceneBoundingRect(),
            Qt.AspectRatioMode.KeepAspectRatio,
        )
        self._fit_mode = True

    def zoom_to_100(self) -> None:
        self.resetTransform()
        self._fit_mode = False

    def toggle_fit_100(self) -> None:
        if self._fit_mode:
            self.zoom_to_100()
        else:
            self.zoom_to_fit()

    def rotate(self, delta: int) -> int:
        self._rotation = (self._rotation + delta) % 360
        if self._pixmap_item is not None:
            self._pixmap_item.setRotation(self._rotation)
            self._scene.setSceneRect(self._pixmap_item.sceneBoundingRect())
            if self._fit_mode:
                self.zoom_to_fit()
        return self._rotation

    def wheelEvent(self, event: QWheelEvent | None) -> None:
        if event is None:
            return
        self.zoom_in() if event.angleDelta().y() > 0 else self.zoom_out()
        event.accept()

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        if self._fit_mode and self._pixmap_item is not None:
            self.zoom_to_fit()
