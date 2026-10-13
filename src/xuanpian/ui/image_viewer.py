from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication, QDialog, QLabel, QVBoxLayout, QWidget


class ImageViewer(QDialog):
    """Full-resolution image preview dialog."""

    def __init__(
        self, image_path: str, filename: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(filename)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)

        pixmap = QPixmap(image_path)
        if not pixmap.isNull():
            screen = QApplication.primaryScreen()
            if screen is not None:
                available = screen.availableGeometry()
                target_w = int(available.width() * 0.8)
                target_h = int(available.height() * 0.8)
                pixmap = pixmap.scaled(
                    target_w,
                    target_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )

        label.setPixmap(pixmap)
        self.resize(pixmap.width() + 20, pixmap.height() + 20)
