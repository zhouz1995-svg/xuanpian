from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark, normalize_legacy_mark
from src.xuanpian.ui.photo_list_model import PhotoRoles
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
BADGE_MARGIN = 4

GREEN = SUCCESS
ORANGE = WARNING
RED = DANGER


class ThumbnailDelegate(QStyledItemDelegate):
    """Paint one Darkroom card exclusively from ``PhotoRoles`` data."""

    def __init__(
        self,
        parent: object | None = None,
        *,
        thumbnail_size: int = 150,
    ) -> None:
        super().__init__(parent)
        self._thumbnail_size = thumbnail_size

    def set_thumbnail_size(self, size: int) -> None:
        self._thumbnail_size = max(1, int(size))

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: object,
    ) -> QSize:
        width = self._thumbnail_size
        return QSize(
            width,
            int(width * THUMB_HEIGHT_RATIO) + OVERLAY_HEIGHT,
        )

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: object,
    ) -> None:
        thumbnail = index.data(int(PhotoRoles.THUMBNAIL))
        filename = str(index.data(int(PhotoRoles.FILENAME)) or "")
        mark = str(index.data(int(PhotoRoles.MARK)) or "")
        analysis = index.data(int(PhotoRoles.ANALYSIS))
        asset_kind = str(index.data(int(PhotoRoles.ASSET_KIND)) or "")

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRect(option.rect).adjusted(2, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            background, border = ELEVATED, ACCENT
        elif hovered:
            background, border = ELEVATED, BORDER_STRONG
        else:
            background, border = SURFACE, BORDER
        painter.setBrush(background)
        painter.setPen(QPen(border, 1))
        painter.drawRoundedRect(bounds, 5, 5)

        image_height = bounds.height() - OVERLAY_HEIGHT
        image_rect = QRect(
            bounds.left(),
            bounds.top(),
            bounds.width(),
            image_height,
        )
        if isinstance(thumbnail, QPixmap) and not thumbnail.isNull():
            scaled = thumbnail.scaled(
                image_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = image_rect.x() + (image_rect.width() - scaled.width()) // 2
            y = image_rect.y() + (image_rect.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        else:
            self._draw_loading_placeholder(painter, image_rect)

        stripe = {
            PhotoMark.KEEP: GREEN,
            PhotoMark.HOLD: ORANGE,
            PhotoMark.DISCARD: RED,
        }.get(normalize_legacy_mark(mark))
        if stripe is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(stripe)
            painter.drawRoundedRect(
                QRect(
                    bounds.left() + 1,
                    bounds.top() + 4,
                    3,
                    bounds.height() - 8,
                ),
                1,
                1,
            )

        bar = QRect(
            bounds.left(),
            bounds.bottom() - OVERLAY_HEIGHT + 1,
            bounds.width(),
            OVERLAY_HEIGHT,
        )
        painter.setBrush(QColor(0, 0, 0, 170))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRect(bar)

        name_font = QFont(painter.font())
        name_font.setPointSize(9)
        painter.setFont(name_font)
        painter.setPen(TEXT)
        name = QFontMetrics(name_font).elidedText(
            filename,
            Qt.TextElideMode.ElideRight,
            max(0, bar.width() - 14),
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
            self._exif_summary(analysis),
            Qt.TextElideMode.ElideRight,
            max(0, bar.width() - 14),
        )
        painter.drawText(
            QRect(
                bar.left() + 6,
                bar.bottom() - 17,
                max(0, bar.width() - 12),
                18,
            ),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            meta,
        )

        badge = self._badge_style(mark, analysis)
        if badge is not None:
            self._draw_analysis_badge(painter, bounds, *badge)
        if asset_kind:
            self._draw_asset_badge(painter, bounds, asset_kind)
        if analysis is not None:
            self._draw_burst_chip(painter, bounds, analysis)

        if selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(ACCENT, 2))
            painter.drawRoundedRect(bounds.adjusted(1, 1, -1, -1), 5, 5)
        painter.restore()

    @staticmethod
    def _badge_style(
        mark_or_analysis: str | object,
        analysis: object | None = None,
    ) -> tuple[QColor, str] | None:
        """Resolve manual/analysis badge from values already supplied by roles."""
        if analysis is None and not isinstance(mark_or_analysis, str):
            analysis = mark_or_analysis
            mark = str(getattr(analysis, "mark", "") or "")
        else:
            mark = str(mark_or_analysis or "")

        manual = {
            PhotoMark.KEEP: (GREEN, "✓"),
            PhotoMark.HOLD: (ORANGE, "⚠"),
            PhotoMark.DISCARD: (RED, "✗"),
        }.get(normalize_legacy_mark(mark))
        if manual is not None:
            return manual
        if analysis is None:
            return None

        if getattr(analysis, "technical_status", AnalysisStatus.PENDING) is not AnalysisStatus.SUCCEEDED:
            return None
        focus = str(getattr(analysis, "focus_conclusion", "") or "")
        exposure = str(getattr(analysis, "exposure_conclusion", "") or "")
        if focus == "明显失焦":
            return RED, "✗"
        if exposure in {"过曝", "欠曝", "曝光异常"}:
            return ORANGE, "⚠"
        return None

    @staticmethod
    def _exif_summary(analysis: object | None) -> str:
        if analysis is None:
            return "—"
        iso = str(getattr(analysis, "iso", "") or "")
        parts = [
            f"ISO {iso}" if iso else "",
            str(getattr(analysis, "aperture", "") or ""),
            str(getattr(analysis, "focal_length", "") or ""),
            str(getattr(analysis, "shutter_speed", "") or ""),
        ]
        return "  ".join(part for part in parts if part) or "—"

    @staticmethod
    def _draw_analysis_badge(
        painter: QPainter,
        bounds: QRect,
        color: QColor,
        text: str,
    ) -> None:
        rect = QRect(bounds.right() - 16, bounds.top() + 1, 14, 14)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _draw_asset_badge(
        painter: QPainter,
        bounds: QRect,
        asset_kind: str,
    ) -> None:
        font = QFont(painter.font())
        font.setPointSize(7)
        font.setBold(True)
        painter.setFont(font)
        width = QFontMetrics(font).horizontalAdvance(asset_kind) + 8
        rect = QRect(bounds.left() + 6, bounds.top() + 1, width, 14)
        color = WARNING if "RAW" in asset_kind else BORDER_STRONG
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 2, 2)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, asset_kind)

    @staticmethod
    def _draw_burst_chip(
        painter: QPainter,
        bounds: QRect,
        analysis: object,
    ) -> None:
        """Neutral chip on the first photo of a similar-photo group."""
        group_id = str(getattr(analysis, "burst_group_id", "") or "")
        size = int(getattr(analysis, "burst_group_size", 0) or 0)
        if not group_id or size < 2:
            return
        is_first = group_id == str(
            getattr(analysis, "photo_id", "") or ""
        )
        if not is_first:
            return
        text = f"连拍 · {size}"
        font = QFont(painter.font())
        font.setPointSize(7)
        font.setBold(True)
        painter.setFont(font)
        width = QFontMetrics(font).horizontalAdvance(text) + 8
        rect = QRect(bounds.left() + 6, bounds.top() + 17, width, 14)
        painter.setBrush(QColor(ACCENT))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 2, 2)
        painter.setPen(Qt.GlobalColor.white)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    @staticmethod
    def _draw_loading_placeholder(
        painter: QPainter,
        rect: QRect,
    ) -> None:
        inner = rect.adjusted(6, 6, -6, -6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(SIDEBAR)
        painter.drawRoundedRect(inner, 4, 4)
        sweep = max(1, int(inner.width() * 0.3))
        middle = inner.center().x()
        gradient = QLinearGradient(
            middle - sweep,
            0,
            middle + sweep,
            0,
        )
        gradient.setColorAt(0.0, QColor(35, 42, 52, 0))
        gradient.setColorAt(0.5, QColor(91, 127, 229, 80))
        gradient.setColorAt(1.0, QColor(35, 42, 52, 0))
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(inner, 4, 4)
