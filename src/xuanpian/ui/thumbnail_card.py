from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QEasingCurve, QPropertyAnimation, QRect, Qt, pyqtProperty, pyqtSignal
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
)
from PyQt6.QtWidgets import QFrame, QWidget

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark, normalize_legacy_mark
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.theme import (
    ACCENT,
    BORDER,
    BORDER_STRONG,
    DANGER,
    ELEVATED,
    MUTED,
    SIDEBAR,
    SUCCESS,
    SURFACE,
    TEXT,
    WARNING,
)

THUMB_HEIGHT_RATIO = 0.75
OVERLAY_HEIGHT = 36

GREEN = SUCCESS
ORANGE = WARNING
RED = DANGER


@dataclass(frozen=True)
class CardBadge:
    color: QColor
    text: str


class ThumbnailCard(QFrame):
    clicked = pyqtSignal(int)
    double_clicked = pyqtSignal(int)

    def __init__(
        self,
        model: ImageFileModel,
        model_index: int,
        thumbnail_width: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = model
        self._model_index = model_index
        self._thumbnail_width = thumbnail_width
        self._selected = False
        self._hovered = False
        self._loading = model.thumbnail is None or model.thumbnail.isNull()
        self._shimmer_offset = 0.0
        self._shimmer_animation: QPropertyAnimation | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self.set_thumbnail_width(thumbnail_width)
        if self._loading:
            self.start_shimmer()

    @property
    def model(self) -> ImageFileModel:
        return self._model

    @property
    def model_index(self) -> int:
        return self._model_index

    @property
    def is_loading(self) -> bool:
        return self._loading

    @property
    def is_selected(self) -> bool:
        return self._selected

    def set_model_index(self, index: int) -> None:
        self._model_index = index

    def set_selected(self, selected: bool) -> None:
        if self._selected != selected:
            self._selected = selected
            self.update()

    def set_thumbnail_width(self, width: int) -> None:
        self._thumbnail_width = width
        height = int(width * THUMB_HEIGHT_RATIO) + OVERLAY_HEIGHT
        self.setFixedSize(width, height)
        self.updateGeometry()
        self.update()

    def refresh(self) -> None:
        self.update()

    def analysis_badge(self) -> CardBadge | None:
        model = self._model
        mark = normalize_legacy_mark(model.mark)
        if mark is PhotoMark.KEEP:
            return CardBadge(GREEN, "✓")
        if mark is PhotoMark.HOLD:
            return CardBadge(ORANGE, "⚠")
        if mark is PhotoMark.DISCARD:
            return CardBadge(RED, "✗")
        if model.technical_status is not AnalysisStatus.SUCCEEDED:
            return None
        if model.focus_conclusion == "明显失焦":
            return CardBadge(RED, "✗")
        if model.exposure_conclusion not in {"", "正常"}:
            return CardBadge(ORANGE, "⚠")
        return None

    def exif_summary(self) -> str:
        parts = [
            f"ISO {self._model.iso}" if self._model.iso else "",
            self._model.aperture,
            self._model.focal_length,
            self._model.shutter_speed,
        ]
        return "  ".join(part for part in parts if part) or "—"

    def _get_shimmer_offset(self) -> float:
        return self._shimmer_offset

    def _set_shimmer_offset(self, value: float) -> None:
        self._shimmer_offset = value
        self.update()

    shimmerOffset = pyqtProperty(float, _get_shimmer_offset, _set_shimmer_offset)

    def start_shimmer(self) -> None:
        if self._shimmer_animation is not None:
            return
        animation = QPropertyAnimation(self, b"shimmerOffset")
        animation.setDuration(1400)
        animation.setStartValue(0.0)
        animation.setEndValue(2.5)
        animation.setLoopCount(-1)
        animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        animation.start()
        self._shimmer_animation = animation

    def stop_shimmer(self) -> None:
        if self._shimmer_animation is not None:
            self._shimmer_animation.stop()
            self._shimmer_animation.deleteLater()
            self._shimmer_animation = None

    def mark_loaded(self) -> None:
        self._loading = False
        self.stop_shimmer()
        self.update()

    def mousePressEvent(self, event: object) -> None:
        self.clicked.emit(self._model_index)

    def mouseDoubleClickEvent(self, event: object) -> None:
        self.double_clicked.emit(self._model_index)

    def enterEvent(self, event: object) -> None:
        self._hovered = True
        self.update()

    def leaveEvent(self, event: object) -> None:
        self._hovered = False
        self.update()

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = self.rect().adjusted(2, 2, -2, -2)
        if self._selected:
            background, border = ELEVATED, ACCENT
        elif self._hovered:
            background, border = ELEVATED, BORDER_STRONG
        else:
            background, border = SURFACE, BORDER
        painter.setBrush(background)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(bounds, 5, 5)

        stripe = {
            PhotoMark.KEEP: GREEN,
            PhotoMark.HOLD: ORANGE,
            PhotoMark.DISCARD: RED,
        }.get(normalize_legacy_mark(self._model.mark))
        if stripe is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(stripe)
            painter.drawRoundedRect(QRect(3, 6, 3, self.height() - 12), 1, 1)

        image_height = self.height() - OVERLAY_HEIGHT - 1
        image_rect = QRect(1, 1, self.width() - 2, image_height)
        pixmap = self._model.thumbnail
        if pixmap is not None and not pixmap.isNull():
            scaled = pixmap.scaled(
                image_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = image_rect.x() + (image_rect.width() - scaled.width()) // 2
            y = image_rect.y() + (image_rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            self._draw_shimmer(painter, image_rect)

        bar = QRect(1, self.height() - OVERLAY_HEIGHT, self.width() - 2, OVERLAY_HEIGHT)
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(bar)

        name_font = QFont(painter.font())
        name_font.setPointSize(9)
        painter.setFont(name_font)
        painter.setPen(TEXT)
        name = QFontMetrics(name_font).elidedText(
            self._model.filename,
            Qt.TextElideMode.ElideRight,
            self.width() - 14,
        )
        painter.drawText(
            bar.adjusted(6, 1, -6, -18),
            Qt.AlignmentFlag.AlignVCenter,
            name,
        )

        meta_font = QFont(name_font)
        meta_font.setPointSize(8)
        painter.setFont(meta_font)
        painter.setPen(MUTED)
        meta = QFontMetrics(meta_font).elidedText(
            self.exif_summary(),
            Qt.TextElideMode.ElideRight,
            self.width() - 14,
        )
        painter.drawText(
            QRect(7, self.height() - 18, self.width() - 14, 18),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            meta,
        )
        self._paint_badges(painter)
        painter.end()

    def _paint_badges(self, painter: QPainter) -> None:
        badge = self.analysis_badge()
        if badge is not None:
            rect = QRect(self.width() - 18, 3, 14, 14)
            painter.setBrush(badge.color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, badge.text)
        if self._model.is_raw:
            rect = QRect(8, 3, 28, 14)
            painter.setBrush(WARNING)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 2, 2)
            painter.setPen(Qt.GlobalColor.white)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "RAW")

    def _draw_shimmer(self, painter: QPainter, rect: QRect) -> None:
        inner = rect.adjusted(6, 6, -6, -6)
        painter.setBrush(SIDEBAR)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(inner, 4, 4)
        sweep = max(1, int(rect.width() * 0.3))
        middle = inner.left() + int(self._shimmer_offset * inner.width() * 1.4) - sweep
        gradient = QLinearGradient(middle, 0, middle + sweep * 2, 0)
        gradient.setColorAt(0.0, QColor(35, 42, 52, 0))
        gradient.setColorAt(0.5, QColor(91, 127, 229, 80))
        gradient.setColorAt(1.0, QColor(35, 42, 52, 0))
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(inner, 4, 4)
