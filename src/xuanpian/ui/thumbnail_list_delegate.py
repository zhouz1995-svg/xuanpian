"""List-view row delegate for the photo browser."""

from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from src.xuanpian.domain.culling import PhotoMark, normalize_legacy_mark
from src.xuanpian.ui.photo_list_model import PhotoRoles
from src.xuanpian.ui.theme import (
    ACCENT,
    BORDER,
    BORDER_STRONG,
    DANGER,
    ELEVATED,
    MUTED,
    SUCCESS,
    SURFACE,
    TEXT,
    WARNING,
)

ROW_HEIGHT = 56
THUMB_WIDTH = 70
THUMB_HEIGHT = 42
GAP = 12
CHIP_PAD_X = 8
CHIP_PAD_Y = 3
CHIP_RADIUS = 4
MARK_COLORS = {
    PhotoMark.KEEP: SUCCESS,
    PhotoMark.HOLD: WARNING,
    PhotoMark.DISCARD: DANGER,
}


def _analysis_badges(analysis: object) -> list[tuple[str, QColor]]:
    """Resolve conclusion chips from a technical analysis result."""
    badges: list[tuple[str, QColor]] = []
    focus = str(getattr(analysis, "focus_conclusion", "") or "")
    exposure = str(getattr(analysis, "exposure_conclusion", "") or "")
    face_count = int(getattr(analysis, "face_count", 0) or 0)

    focus_color = {
        "清晰": SUCCESS,
        "明显失焦": DANGER,
        "建议复查": WARNING,
    }.get(focus)
    if focus_color is not None:
        badges.append((focus, focus_color))
    if exposure and exposure != "正常":
        exposure_color = DANGER if exposure == "过曝" else WARNING
        badges.append((exposure, exposure_color))
    if face_count > 0:
        badges.append(("人像", ACCENT))
    return badges[:3]


class ThumbnailListDelegate(QStyledItemDelegate):
    """Paint one list row from ``PhotoRoles`` data."""

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: object,
    ) -> QSize:
        return QSize(0, ROW_HEIGHT)

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
        capture_time = str(index.data(int(PhotoRoles.CAPTURE_TIME)) or "")

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        bounds = QRect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            painter.fillRect(bounds, ELEVATED)
            painter.setPen(QPen(ACCENT, 1))
            painter.drawRect(bounds.adjusted(0, 0, -1, -1))
        elif hovered:
            painter.fillRect(bounds, ELEVATED)
            painter.setPen(QPen(BORDER_STRONG, 1))
            painter.drawRect(bounds.adjusted(0, 0, -1, -1))

        # Thumbnail
        thumb_rect = QRect(
            bounds.left() + GAP,
            bounds.top() + (bounds.height() - THUMB_HEIGHT) // 2,
            THUMB_WIDTH,
            THUMB_HEIGHT,
        )
        painter.setBrush(SURFACE)
        painter.setPen(QPen(BORDER, 1))
        painter.drawRoundedRect(thumb_rect, 5, 5)
        if isinstance(thumbnail, QPixmap) and not thumbnail.isNull():
            scaled = thumbnail.scaled(
                thumb_rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                thumb_rect.x() + (thumb_rect.width() - scaled.width()) // 2,
                thumb_rect.y() + (thumb_rect.height() - scaled.height()) // 2,
                scaled,
            )

        # Filename
        name_x = thumb_rect.right() + GAP
        name_width = 200
        painter.setPen(TEXT)
        font = QFont(option.font)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        elided_name = metrics.elidedText(
            filename,
            Qt.TextElideMode.ElideMiddle,
            name_width,
        )
        painter.drawText(
            QRect(name_x, bounds.top(), name_width, bounds.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided_name,
        )

        # Conclusion chips
        chip_x = name_x + name_width + GAP
        chip_y = bounds.top() + (bounds.height() - 20) // 2
        chip_height = 20
        for label, color in _analysis_badges(analysis):
            chip_width = metrics.horizontalAdvance(label) + CHIP_PAD_X * 2
            if chip_x + chip_width > bounds.right() - 210:
                break
            chip_rect = QRect(chip_x, chip_y, chip_width, chip_height)
            painter.setBrush(color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(chip_rect, CHIP_RADIUS, CHIP_RADIUS)
            painter.setPen(QColor("#0D0F12"))
            painter.drawText(
                chip_rect,
                Qt.AlignmentFlag.AlignCenter,
                label,
            )
            chip_x += chip_width + 6

        # Capture time
        time_rect = QRect(
            bounds.right() - 210,
            bounds.top(),
            190,
            bounds.height(),
        )
        painter.setPen(MUTED)
        painter.drawText(
            time_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
            capture_time,
        )

        # Mark dot + label
        mark_color = MARK_COLORS.get(normalize_legacy_mark(mark))
        if mark_color is not None:
            dot_x = bounds.right() - GAP - 26
            painter.setBrush(mark_color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(dot_x, bounds.top() + (bounds.height() - 7) // 2, 7, 7)
            painter.setPen(mark_color)
            painter.drawText(
                QRect(dot_x - 58, bounds.top(), 52, bounds.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                normalize_legacy_mark(mark).value,
            )
        painter.restore()
