from __future__ import annotations

from collections import OrderedDict

from PyQt6.QtCore import (
    QEasingCurve,
    QItemSelectionModel,
    QModelIndex,
    QPropertyAnimation,
    Qt,
    pyqtSignal,
)
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QListView,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark, normalize_legacy_mark
from src.xuanpian.ui.edge_tab import EdgeTab
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.icons import application_icon
from src.xuanpian.ui.photo_list_model import PhotoListModel, PhotoRoles
from src.xuanpian.services.burst_grouper import group_photos, parse_capture_time
from src.xuanpian.ui.thumbnail_delegate import ThumbnailDelegate
from src.xuanpian.ui.thumbnail_list_delegate import ThumbnailListDelegate

_ZOOM_STEP = 20
_ZOOM_MIN = 80
_ZOOM_MAX = 400
_THUMB_DEFAULT = 150
_SIDEBAR_WIDTH = 200
_SIDEBAR_COLLAPSED_WIDTH = 24
_TAB_GAP = _SIDEBAR_COLLAPSED_WIDTH + 4


def _empty_state_icon_pixmap(widget: QWidget) -> QPixmap:
    """Render the application icon for the empty state at the display scale."""
    dpr = max(1.0, widget.devicePixelRatioF())
    logical_size = 64
    physical = max(1, round(logical_size * dpr))
    pixmap = application_icon().pixmap(physical, physical)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


GROUPS = OrderedDict(
    [
        ("all", "全部"),
        ("clear", "清晰"),
        ("out_of_focus", "明显失焦"),
        ("review", "建议复查"),
        ("exposure", "曝光异常"),
        ("portrait", "人像"),
    ]
)

GROUP_ICONS = {
    "all": "▦",
    "clear": "◎",
    "out_of_focus": "◌",
    "review": "◐",
    "exposure": "☾",
    "portrait": "☺",
}


class GroupBar(QWidget):
    group_changed = pyqtSignal(str)

    def __init__(
        self, parent: QWidget | None = None, *, vertical: bool = False
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2 if vertical else 6)
        if vertical:
            self.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Preferred,
            )
        self._buttons: dict[str, QPushButton] = {}
        for key, label in GROUPS.items():
            button = QPushButton(f"{GROUP_ICONS[key]}  {label}")
            button.setCheckable(True)
            button.setProperty(
                "filterBtn" if vertical else "groupBarButton",
                True,
            )
            button.setProperty("groupNavBtn", True)
            button.clicked.connect(
                lambda checked=False, group_key=key: self._on_clicked(
                    group_key
                )
            )
            if vertical:
                button.setSizePolicy(
                    QSizePolicy.Policy.Expanding,
                    QSizePolicy.Policy.Fixed,
                )
            layout.addWidget(button)
            self._buttons[key] = button
        layout.addStretch()
        self._current = "all"
        self._buttons["all"].setChecked(True)

    def _on_clicked(self, key: str) -> None:
        if key == self._current:
            return
        self.set_current(key)
        self.group_changed.emit(key)

    def set_current(self, key: str) -> None:
        self._current = key
        for button_key, button in self._buttons.items():
            button.setChecked(button_key == key)

    def update_counts(self, counts: dict[str, int]) -> None:
        for key, button in self._buttons.items():
            button.setText(
                f"{GROUP_ICONS[key]}  {GROUPS[key]}  "
                f"({counts.get(key, 0)})"
            )


class LeftSidebar(QFrame):
    toggled = pyqtSignal(bool)
    burst_group_selected = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("leftSidebar")
        self.setMinimumWidth(_SIDEBAR_WIDTH)
        self.setMaximumWidth(_SIDEBAR_WIDTH)
        self._collapsed = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, _TAB_GAP, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        self._header_label = QLabel("图库")
        self._header_label.setProperty("class", "sidebarHeader")
        header.addWidget(self._header_label)
        header.addStretch()
        layout.addLayout(header)

        self._group_bar = GroupBar(self, vertical=True)
        layout.addWidget(self._group_bar)
        self._picks_label = QLabel("保留 · 0")
        self._pending_label = QLabel("待定 · 0")
        self._rejects_label = QLabel("舍弃 · 0")
        for stat in (
            self._picks_label,
            self._pending_label,
            self._rejects_label,
        ):
            stat.setProperty("class", "statLabel")
            layout.addWidget(stat)

        self._burst_header = QLabel("相似组")
        self._burst_header.setProperty("class", "sidebarHeader")
        layout.addWidget(self._burst_header)
        self._burst_list = QListWidget(self)
        self._burst_list.setObjectName("burstGroupList")
        self._burst_list.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._burst_list.setMaximumHeight(150)
        self._burst_list.itemClicked.connect(self._on_burst_item_clicked)
        layout.addWidget(self._burst_list)
        self._burst_list.hide()
        self._burst_header.hide()
        layout.addStretch()

        self._animation = QPropertyAnimation(self, b"maximumWidth", self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.finished.connect(self._on_animation_finished)

        self._tab = EdgeTab("面板", self, rounded_side="left")
        self._tab.setObjectName("sidebarEdgeTab")
        self._tab.setToolTip("展开筛选面板")
        self._tab.clicked.connect(self.toggle)
        self._tab.show()
        self._position_tab()

    @property
    def group_bar(self) -> GroupBar:
        return self._group_bar

    @property
    def is_collapsed(self) -> bool:
        return self._collapsed

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self._position_tab()

    def _position_tab(self, width: int | None = None) -> None:
        self._tab.move(
            (self.width() if width is None else width)
            - self._tab.width()
            - 1,
            max(4, (self.height() - self._tab.height()) // 2),
        )
        self._tab.raise_()

    def toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._animation.stop()
        if self._collapsed:
            self.setMinimumWidth(0)
            self._animation.setStartValue(self.width())
            self._animation.setEndValue(_SIDEBAR_COLLAPSED_WIDTH)
            self._header_label.hide()
            self._group_bar.hide()
            self._picks_label.hide()
            self._pending_label.hide()
            self._rejects_label.hide()
            self._burst_header.hide()
            self._burst_list.hide()
            self._tab.show()
        else:
            self._animation.setStartValue(self.width())
            self._animation.setEndValue(_SIDEBAR_WIDTH)
        self._tab.setToolTip(
            "收起筛选面板" if self._collapsed else "展开筛选面板"
        )
        self._animation.start()
        self.toggled.emit(not self._collapsed)

    def _on_animation_finished(self) -> None:
        self._position_tab(
            _SIDEBAR_COLLAPSED_WIDTH if self._collapsed else _SIDEBAR_WIDTH
        )
        if self._collapsed:
            return
        self.setMinimumWidth(_SIDEBAR_WIDTH)
        self._header_label.show()
        self._group_bar.show()
        self._picks_label.show()
        self._pending_label.show()
        self._rejects_label.show()
        if self._burst_list.count():
            self._burst_header.show()
            self._burst_list.show()

    def _on_burst_item_clicked(self, item: QListWidgetItem) -> None:
        group_id = item.data(Qt.ItemDataRole.UserRole)
        self.burst_group_selected.emit(str(group_id))

    def update_burst_groups(self, items: list[tuple[str, str]]) -> None:
        """Populate the similar-photo list; hides the section when empty."""
        current_group_id = None
        current_row = self._burst_list.currentRow()
        if current_row >= 0:
            current_item = self._burst_list.item(current_row)
            current_group_id = (
                current_item.data(Qt.ItemDataRole.UserRole)
                if current_item is not None
                else None
            )
        self._burst_list.clear()
        for group_id, label in items:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, group_id)
            self._burst_list.addItem(item)
        has_items = bool(items)
        self._burst_header.setVisible(has_items and not self._collapsed)
        self._burst_list.setVisible(has_items and not self._collapsed)
        if not has_items:
            self._burst_list.clearSelection()
        elif current_group_id is not None:
            for row in range(self._burst_list.count()):
                item = self._burst_list.item(row)
                if item.data(Qt.ItemDataRole.UserRole) == current_group_id:
                    self._burst_list.setCurrentRow(row)
                    break

    def clear_burst_selection(self) -> None:
        self._burst_list.clearSelection()

    def update_stats(
        self,
        picks: int,
        pending: int,
        rejects: int,
    ) -> None:
        self._picks_label.setText(f"保留 · {picks}")
        self._pending_label.setText(f"待定 · {pending}")
        self._rejects_label.setText(f"舍弃 · {rejects}")


class _ThumbnailListView(QListView):
    zoom_requested = pyqtSignal(int)
    viewport_resized = pyqtSignal()

    def wheelEvent(self, event: object) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_requested.emit(event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def resizeEvent(self, event: object) -> None:
        super().resizeEvent(event)
        self.viewport_resized.emit()


class ThumbnailBrowser(QWidget):
    selection_changed = pyqtSignal()
    thumbnail_size_changed = pyqtSignal(int)
    model_set_changed = pyqtSignal()
    analysis_status_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._sidebar = LeftSidebar(self)
        self._group_bar = self._sidebar.group_bar
        self._group_bar.group_changed.connect(self._set_group)
        self._sidebar.burst_group_selected.connect(self.set_burst_group)
        root.addWidget(self._sidebar)

        center = QWidget(self)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        self._empty_widget = QWidget(center)
        empty_layout = QVBoxLayout(self._empty_widget)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_icon = QLabel()
        self._empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_icon.setPixmap(_empty_state_icon_pixmap(self))
        empty_layout.addWidget(self._empty_icon)
        self._empty_label = QLabel("导入图片文件夹开始选片")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._empty_label)
        self._empty_hint = QLabel("支持 JPEG / HEIC / 常见 RAW 配对导入")
        self._empty_hint.setProperty("class", "emptyHint")
        self._empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self._empty_hint)

        self._loading_bar = QProgressBar(center)
        self._loading_bar.setRange(0, 0)
        self._loading_bar.setTextVisible(False)
        self._loading_bar.setFixedHeight(3)

        self._model_by_photo_id: dict[str, ImageFileModel] = {}
        self._all_photo_ids: list[str] = []
        self._order_by_photo_id: dict[str, int] = {}
        self._groups_by_photo_id: dict[str, frozenset[str]] = {}
        self._marks_by_photo_id: dict[str, PhotoMark | None] = {}
        self._activity_by_photo_id: dict[str, bool] = {}
        self._burst_group_by_photo_id: dict[str, str] = {}
        self._burst_group_photo_ids: dict[str, tuple[str, ...]] = {}
        self._current_burst_group: str | None = None
        self._burst_sidebar_signature: tuple[tuple[str, int], ...] | None = None
        self._group_counts = {key: 0 for key in GROUPS}
        self._mark_counts = {
            PhotoMark.KEEP: 0,
            PhotoMark.HOLD: 0,
            PhotoMark.DISCARD: 0,
        }
        self._analysis_activity_count = 0
        self._photo_list_model = PhotoListModel(
            self._model_by_photo_id.get,
            self,
        )
        self._delegate = ThumbnailDelegate(
            self,
            thumbnail_size=_THUMB_DEFAULT,
        )
        self._view = _ThumbnailListView(center)
        self._view.setObjectName("thumbnailView")
        self._view.setModel(self._photo_list_model)
        self._view.setItemDelegate(self._delegate)
        self._view.setViewMode(QListView.ViewMode.IconMode)
        self._view.setResizeMode(QListView.ResizeMode.Adjust)
        self._view.setMovement(QListView.Movement.Static)
        self._view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems
        )
        self._view.setUniformItemSizes(True)
        self._view.setWrapping(True)
        # QListView ignores item spacing once a fixed gridSize is set, so
        # keep it at zero and let the adaptive width fill the viewport.
        self._view.setSpacing(0)
        self._view.setMouseTracking(True)
        self._view.setSelectionRectVisible(True)
        self._view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._view.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._view.viewport_resized.connect(self._refit_grid_to_width)
        self._view.selectionModel().selectionChanged.connect(
            self._on_view_selection_changed
        )
        self._view.doubleClicked.connect(self._on_index_double_clicked)
        self._view.zoom_requested.connect(self._zoom_from_delta)

        self._list_delegate = ThumbnailListDelegate(self)
        self._list_view = QListView(center)
        self._list_view.setObjectName("thumbnailListView")
        self._list_view.setModel(self._photo_list_model)
        self._list_view.setItemDelegate(self._list_delegate)
        self._list_view.setSelectionModel(self._view.selectionModel())
        self._list_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self._list_view.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectItems
        )
        self._list_view.setUniformItemSizes(True)
        self._list_view.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._list_view.doubleClicked.connect(self._on_index_double_clicked)
        self._list_view.setVisible(False)

        center_layout.addWidget(self._empty_widget)
        center_layout.addWidget(self._loading_bar)
        center_layout.addWidget(self._view)
        center_layout.addWidget(self._list_view)
        root.addWidget(center, 1)

        self._current_group = "all"
        self._picks_only = False
        self._thumb_size = _THUMB_DEFAULT
        self._view_mode = "grid"
        self.setFocusProxy(self._view)
        self.set_thumbnail_size(_THUMB_DEFAULT)
        self._show_empty_state()

    def _show_empty_state(
        self,
        message: str = "导入图片文件夹开始选片",
    ) -> None:
        self._empty_label.setText(message)
        self._empty_widget.setVisible(True)
        self._view.setVisible(False)
        self._list_view.setVisible(False)
        self._sidebar.setVisible(False)
        self._loading_bar.setVisible(False)

    def _show_filtered_empty(self, message: str) -> None:
        self._empty_label.setText(message)
        self._empty_widget.setVisible(True)
        self._view.setVisible(False)
        self._list_view.setVisible(False)
        self._sidebar.setVisible(True)
        self._loading_bar.setVisible(False)

    def _show_grid(self) -> None:
        self._empty_widget.setVisible(False)
        self._sidebar.setVisible(True)
        self._apply_view_visibility()

    def _active_view(self) -> QListView:
        return self._list_view if self._view_mode == "list" else self._view

    def _apply_view_visibility(self) -> None:
        if self._empty_widget.isVisible():
            return
        self._view.setVisible(self._view_mode == "grid")
        self._list_view.setVisible(self._view_mode == "list")

    def set_view_mode(self, mode: str) -> None:
        if mode not in ("grid", "list"):
            raise ValueError(f"unknown view mode: {mode!r}")
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self._apply_view_visibility()
        self.setFocusProxy(self._active_view())
        self._scroll_to_selection()

    def view_mode(self) -> str:
        return self._view_mode

    def current_group_label(self) -> str:
        return GROUPS.get(self._current_group, "全部")

    def _scroll_to_selection(self) -> None:
        selected = self._view.selectionModel().selectedIndexes()
        if not selected:
            return
        self._active_view().scrollTo(
            selected[0],
            QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    @staticmethod
    def classify_group(
        model: ImageFileModel,
        group_key: str,
    ) -> bool:
        if group_key == "all":
            return True
        if model.technical_status is not AnalysisStatus.SUCCEEDED:
            return False
        if group_key == "clear":
            return model.focus_conclusion == "清晰"
        if group_key == "out_of_focus":
            return model.focus_conclusion == "明显失焦"
        if group_key == "review":
            return model.focus_conclusion == "建议复查"
        if group_key == "exposure":
            return model.exposure_conclusion != "正常" and bool(model.exposure_conclusion)
        if group_key == "portrait":
            return model.portrait_assessed and model.face_count > 0
        return False

    _classify_group = classify_group

    def _groups_for(self, model: ImageFileModel) -> frozenset[str]:
        return frozenset(
            key for key in GROUPS if self.classify_group(model, key)
        )

    @staticmethod
    def _has_model_analysis_activity(model: ImageFileModel) -> bool:
        return any(
            status is not AnalysisStatus.PENDING
            for status in (model.technical_status,)
        )

    def _rebuild_derived_state(self) -> None:
        self._order_by_photo_id = {
            photo_id: row
            for row, photo_id in enumerate(self._all_photo_ids)
        }
        self._groups_by_photo_id = {}
        self._marks_by_photo_id = {}
        self._activity_by_photo_id = {}
        self._group_counts = {key: 0 for key in GROUPS}
        self._mark_counts = {
            PhotoMark.KEEP: 0,
            PhotoMark.HOLD: 0,
            PhotoMark.DISCARD: 0,
        }
        self._analysis_activity_count = 0
        for photo_id in self._all_photo_ids:
            model = self._model_by_photo_id[photo_id]
            memberships = self._groups_for(model)
            self._groups_by_photo_id[photo_id] = memberships
            for key in memberships:
                self._group_counts[key] += 1
            mark = normalize_legacy_mark(model.mark)
            self._marks_by_photo_id[photo_id] = mark
            if mark in self._mark_counts:
                self._mark_counts[mark] += 1
            active = self._has_model_analysis_activity(model)
            self._activity_by_photo_id[photo_id] = active
            self._analysis_activity_count += int(active)

    def _recompute_burst_groups(self) -> None:
        """Derive similar-photo groups from EXIF capture time in display order."""
        capture_time_by_id: dict[str, float | None] = {}
        for photo_id in self._all_photo_ids:
            model = self._model_by_photo_id.get(photo_id)
            capture_time_by_id[photo_id] = (
                parse_capture_time(model.date_taken)
                if model is not None
                else None
            )
        groups = group_photos(
            list(self._all_photo_ids),
            capture_time_by_id,
        )
        by_photo_id: dict[str, str] = {}
        by_group_id: dict[str, tuple[str, ...]] = {}
        for group in groups.values():
            by_group_id[group.group_id] = group.photo_ids
            for photo_id in group.photo_ids:
                by_photo_id[photo_id] = group.group_id
        self._burst_group_by_photo_id = by_photo_id
        self._burst_group_photo_ids = by_group_id
        for photo_id, model in self._model_by_photo_id.items():
            group = groups.get(photo_id)
            model.burst_group_id = group.group_id if group is not None else ""
            model.burst_group_size = group.size if group is not None else 0
        if (
            self._current_burst_group is not None
            and self._current_burst_group not in by_group_id
        ):
            self._current_burst_group = None
        self._refresh_burst_sidebar()

    def _refresh_burst_sidebar(self) -> None:
        signature = tuple(
            sorted(
                (group_id, len(member_ids))
                for group_id, member_ids in self._burst_group_photo_ids.items()
            )
        )
        if signature == self._burst_sidebar_signature:
            return
        self._burst_sidebar_signature = signature
        items: list[tuple[str, str]] = []
        for group_id in self._burst_group_photo_ids:
            member_ids = self._burst_group_photo_ids[group_id]
            first_model = self._model_by_photo_id.get(member_ids[0])
            if first_model is None:
                continue
            items.append(
                (
                    group_id,
                    f"{first_model.filename} · 连拍 {len(member_ids)}张",
                )
            )
        self._sidebar.update_burst_groups(items)

    def refresh_burst_groups(self) -> None:
        """Recompute similar-photo groups from the current model metadata."""
        self._recompute_burst_groups()

    def _compute_group_counts(self) -> dict[str, int]:
        return dict(self._group_counts)

    def compute_group_counts(self) -> dict[str, int]:
        return self._compute_group_counts()

    def update_group_counts(self) -> None:
        self._rebuild_derived_state()
        self._group_bar.update_counts(self._compute_group_counts())
        self._update_sidebar_stats()
        self._apply_filter()

    def _update_sidebar_stats(self) -> None:
        self._sidebar.update_stats(
            self._mark_counts[PhotoMark.KEEP],
            self._mark_counts[PhotoMark.HOLD],
            self._mark_counts[PhotoMark.DISCARD],
        )

    def set_models(self, models: list[ImageFileModel]) -> None:
        photo_ids = [self._ensure_photo_id(model) for model in models]
        if len(photo_ids) != len(set(photo_ids)):
            raise ValueError("photo_id values must be unique")
        self._model_by_photo_id.clear()
        self._model_by_photo_id.update(
            zip(photo_ids, models, strict=True)
        )
        self._all_photo_ids = photo_ids
        self._recompute_burst_groups()
        self._rebuild_derived_state()
        self._current_group = "all"
        self._current_burst_group = None
        self._picks_only = False
        self._sidebar.clear_burst_selection()
        self._group_bar.set_current("all")
        self._group_bar.update_counts(self._compute_group_counts())
        self._update_sidebar_stats()
        self._set_current_photo_ids(photo_ids)
        if models:
            self._show_grid()
        else:
            self._show_empty_state()
        self.model_set_changed.emit()

    def upsert_models(self, models: list[ImageFileModel]) -> None:
        replaced_ids: list[str] = []
        for model in models:
            photo_id = self._ensure_photo_id(model)
            existing = self._model_by_photo_id.get(photo_id)
            self._model_by_photo_id[photo_id] = model
            if existing is None:
                self._all_photo_ids.append(photo_id)
            elif existing is not model:
                replaced_ids.append(photo_id)

        self._recompute_burst_groups()
        self._rebuild_derived_state()
        self._apply_filter()
        for photo_id in replaced_ids:
            self._photo_list_model.update_photo(
                photo_id,
                [
                    PhotoRoles.PATH,
                    PhotoRoles.FILENAME,
                    PhotoRoles.THUMBNAIL,
                    PhotoRoles.MARK,
                    PhotoRoles.ANALYSIS,
                    PhotoRoles.ASSET_KIND,
                ],
            )
        if self._all_photo_ids and self.current_photo_ids():
            self._show_grid()
        self._group_bar.update_counts(self._compute_group_counts())
        self._update_sidebar_stats()
        self.model_set_changed.emit()

    def remove_models(self, models: list[ImageFileModel]) -> None:
        removed_ids = {
            model.photo_id for model in models if model.photo_id
        }
        if not removed_ids:
            return
        self._all_photo_ids = [
            photo_id
            for photo_id in self._all_photo_ids
            if photo_id not in removed_ids
        ]
        self._set_current_photo_ids(
            [
                photo_id
                for photo_id in self.current_photo_ids()
                if photo_id not in removed_ids
            ]
        )
        for photo_id in removed_ids:
            self._model_by_photo_id.pop(photo_id, None)
        self._recompute_burst_groups()
        self._rebuild_derived_state()
        if not self._all_photo_ids:
            self._show_empty_state()
        elif not self.current_photo_ids():
            self._apply_filter()
        self._group_bar.update_counts(self._compute_group_counts())
        self._update_sidebar_stats()
        self.model_set_changed.emit()

    def _get_filtered(self) -> list[str]:
        photo_ids = [
            photo_id
            for photo_id in self._all_photo_ids
            if self.classify_group(
                self._model_by_photo_id[photo_id],
                self._current_group,
            )
        ]
        if self._current_burst_group is not None:
            photo_ids = [
                photo_id
                for photo_id in photo_ids
                if self._burst_group_by_photo_id.get(photo_id)
                == self._current_burst_group
            ]
        if self._picks_only:
            photo_ids = [
                photo_id
                for photo_id in photo_ids
                if normalize_legacy_mark(
                    self._model_by_photo_id[photo_id].mark
                )
                is PhotoMark.KEEP
            ]
        return photo_ids

    def _has_analysis_activity(self) -> bool:
        return self._analysis_activity_count > 0

    def _is_visible(
        self,
        memberships: frozenset[str],
        mark: PhotoMark | None,
        photo_id: str,
    ) -> bool:
        return (
            self._current_group in memberships
            and (
                self._current_burst_group is None
                or self._burst_group_by_photo_id.get(photo_id)
                == self._current_burst_group
            )
            and (not self._picks_only or mark is PhotoMark.KEEP)
        )

    def _refresh_empty_state(self) -> None:
        if self._photo_list_model.rowCount():
            self._show_grid()
        elif self._current_group != "all" and not self._has_analysis_activity():
            self._show_filtered_empty("请先执行全部分析")
        else:
            self._show_filtered_empty("此筛选条件下没有照片")

    def refresh_photo(
        self,
        photo_id: str,
        changed_roles: list[PhotoRoles | int]
        | tuple[PhotoRoles | int, ...],
    ) -> None:
        """Refresh one mutated photo without rebuilding global derived state."""
        model = self._model_by_photo_id.get(photo_id)
        old_memberships = self._groups_by_photo_id.get(photo_id)
        if model is None or old_memberships is None:
            return
        old_mark = self._marks_by_photo_id[photo_id]
        old_active = self._activity_by_photo_id[photo_id]
        new_memberships = self._groups_for(model)
        new_mark = normalize_legacy_mark(model.mark)
        new_active = self._has_model_analysis_activity(model)

        for key in old_memberships - new_memberships:
            self._group_counts[key] -= 1
        for key in new_memberships - old_memberships:
            self._group_counts[key] += 1
        if old_mark != new_mark:
            if old_mark in self._mark_counts:
                self._mark_counts[old_mark] -= 1
            if new_mark in self._mark_counts:
                self._mark_counts[new_mark] += 1
        if old_active != new_active:
            self._analysis_activity_count += 1 if new_active else -1

        self._groups_by_photo_id[photo_id] = new_memberships
        self._marks_by_photo_id[photo_id] = new_mark
        self._activity_by_photo_id[photo_id] = new_active

        old_visible = self._is_visible(old_memberships, old_mark, photo_id)
        new_visible = self._is_visible(new_memberships, new_mark, photo_id)
        if old_visible and not new_visible:
            self._photo_list_model.remove_photo_id(photo_id)
        elif not old_visible and new_visible:
            self._photo_list_model.insert_photo_id_ordered(
                photo_id,
                self._order_by_photo_id,
            )
        elif new_visible:
            self._photo_list_model.update_photo(photo_id, changed_roles)

        self._group_bar.update_counts(self._group_counts)
        self._update_sidebar_stats()
        self._refresh_empty_state()
        if any(
            int(role) == int(PhotoRoles.ANALYSIS)
            for role in changed_roles
        ):
            self.analysis_status_changed.emit()

    def _apply_filter(self) -> None:
        filtered = self._get_filtered()
        self._set_current_photo_ids(filtered)
        self._group_bar.update_counts(self._compute_group_counts())
        self._group_bar.set_current(self._current_group)
        self._refresh_empty_state()

    def _set_current_photo_ids(self, photo_ids: list[str]) -> None:
        selection_model = self._view.selectionModel()
        selected_before = self.selected_photo_ids()
        was_blocked = selection_model.blockSignals(True)
        try:
            self._photo_list_model.set_photo_ids(photo_ids)
            first_selected = QModelIndex()
            for photo_id in selected_before:
                row = self._photo_list_model.row_for_id(photo_id)
                if row < 0:
                    continue
                index = self._photo_list_model.index(row, 0)
                selection_model.select(
                    index,
                    QItemSelectionModel.SelectionFlag.Select,
                )
                if not first_selected.isValid():
                    first_selected = index
            if first_selected.isValid():
                selection_model.setCurrentIndex(
                    first_selected,
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        finally:
            selection_model.blockSignals(was_blocked)
        if self.selected_photo_ids() != selected_before:
            self.selection_changed.emit()

    def _set_group(self, key: str) -> None:
        self._current_group = key
        self._current_burst_group = None
        self._picks_only = False
        self._sidebar.clear_burst_selection()
        self._apply_filter()

    def set_burst_group(self, group_id: str) -> None:
        if group_id and group_id not in self._burst_group_photo_ids:
            return
        target = group_id or None
        if target == self._current_burst_group:
            self._current_burst_group = None
            self._sidebar.clear_burst_selection()
        else:
            self._current_burst_group = target
        self._apply_filter()

    def burst_member_ids(self, photo_id: str) -> tuple[str, ...] | None:
        """Return the similar-photo group for a photo, or ``None``."""
        group_id = self._burst_group_by_photo_id.get(photo_id)
        if group_id is None:
            return None
        return self._burst_group_photo_ids.get(group_id)

    def toggle_picks_only(self) -> bool:
        self._picks_only = not self._picks_only
        self._apply_filter()
        return self._picks_only

    @property
    def picks_only(self) -> bool:
        return self._picks_only

    def _on_view_selection_changed(self, *_args: object) -> None:
        self.selection_changed.emit()

    def _on_index_double_clicked(self, index: QModelIndex) -> None:
        photo_id = index.data(int(PhotoRoles.ID))
        if not photo_id:
            return
        main_window = self._find_main_window()
        if main_window is not None:
            main_window.enter_preview_photo(str(photo_id))

    def _on_card_double_clicked(self, index: int) -> None:
        self._on_index_double_clicked(
            self._photo_list_model.index(index, 0)
        )

    def _find_main_window(self) -> object | None:
        for widget in QApplication.topLevelWidgets():
            if widget.__class__.__name__ == "MainWindow":
                return widget
        return None

    def update_thumbnail(self, index: int, qimage: QImage) -> None:
        if not 0 <= index < len(self._all_photo_ids):
            return
        photo_id = self._all_photo_ids[index]
        model = self._model_by_photo_id[photo_id]
        model.thumbnail = QPixmap.fromImage(qimage)
        self._photo_list_model.update_photo(
            photo_id,
            [PhotoRoles.THUMBNAIL],
        )

    def update_model_thumbnail(
        self,
        model: ImageFileModel,
        qimage: QImage,
    ) -> None:
        photo_id = model.photo_id
        if self._model_by_photo_id.get(photo_id) is not model:
            return
        model.thumbnail = QPixmap.fromImage(qimage)
        self._photo_list_model.update_photo(
            photo_id,
            [PhotoRoles.THUMBNAIL],
        )

    def current_models(self) -> list[ImageFileModel]:
        return [
            self._model_by_photo_id[photo_id]
            for photo_id in self.current_photo_ids()
        ]

    def all_models(self) -> list[ImageFileModel]:
        return [
            self._model_by_photo_id[photo_id]
            for photo_id in self._all_photo_ids
        ]

    def current_photo_ids(self) -> list[str]:
        return self._photo_list_model.photo_ids()

    def model_for_photo_id(
        self,
        photo_id: str,
    ) -> ImageFileModel | None:
        return self._model_by_photo_id.get(photo_id)

    def selected_indexes(self) -> list[int]:
        return sorted(
            index.row()
            for index in self._view.selectionModel().selectedIndexes()
            if index.isValid()
        )

    def selected_photo_ids(self) -> list[str]:
        photo_ids: list[str] = []
        for index in sorted(
            self._view.selectionModel().selectedIndexes(),
            key=lambda item: item.row(),
        ):
            photo_id = index.data(int(PhotoRoles.ID))
            if photo_id:
                photo_ids.append(str(photo_id))
        return photo_ids

    def select_photo_id(self, photo_id: str) -> None:
        row = self._photo_list_model.row_for_id(photo_id)
        if row >= 0:
            self.select_index(row)

    def select_index(self, index: int) -> None:
        model_index = self._photo_list_model.index(index, 0)
        if not model_index.isValid():
            return
        selection_model = self._view.selectionModel()
        selection_model.setCurrentIndex(
            model_index,
            QItemSelectionModel.SelectionFlag.NoUpdate,
        )
        selection_model.select(
            model_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect,
        )
        self._view.scrollTo(
            model_index,
            QAbstractItemView.ScrollHint.EnsureVisible,
        )

    def select_all(self) -> None:
        self._view.selectAll()

    def _effective_thumb_width(self) -> int:
        """Card width that makes the grid columns fill the viewport width."""
        if not self._view.isVisible():
            return self._thumb_size
        viewport_width = self._view.viewport().width()
        if viewport_width <= 0:
            return self._thumb_size
        # QListView subtracts the visible vertical scrollbar from the column
        # count but still positions items from the left edge, so account for
        # it here or the last column falls short of the right edge.
        scrollbar = self._view.verticalScrollBar()
        scrollbar_width = scrollbar.width() if scrollbar.isVisible() else 0
        usable = max(1, viewport_width - scrollbar_width)
        cols = max(
            1,
            usable // self._thumb_size,
        )
        effective = usable // cols
        return max(_ZOOM_MIN, min(_ZOOM_MAX, effective))

    def _apply_grid_size(self) -> None:
        width = self._effective_thumb_width()
        self._delegate.set_thumbnail_size(width)
        item_size = self._delegate.sizeHint(
            QStyleOptionViewItem(),
            QModelIndex(),
        )
        self._view.setGridSize(item_size)
        self._view.doItemsLayout()
        self._view.viewport().update()

    def _refit_grid_to_width(self) -> None:
        self._apply_grid_size()

    def set_thumbnail_size(self, size: int) -> None:
        self._thumb_size = int(size)
        self._apply_grid_size()

    def showEvent(self, event: object) -> None:
        super().showEvent(event)
        self._apply_grid_size()

    def _zoom_from_delta(self, delta: int) -> None:
        new_size = self._thumb_size
        if delta > 0:
            new_size = min(_ZOOM_MAX, self._thumb_size + _ZOOM_STEP)
        elif delta < 0:
            new_size = max(_ZOOM_MIN, self._thumb_size - _ZOOM_STEP)
        if new_size != self._thumb_size:
            self.set_thumbnail_size(new_size)
            self.thumbnail_size_changed.emit(new_size)

    def wheelEvent(self, event: object) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom_from_delta(event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def show_loading(self) -> None:
        self._loading_bar.setVisible(True)

    def hide_loading(self) -> None:
        self._loading_bar.setVisible(False)

    def refresh_badges(self) -> None:
        self.update_group_counts()
        self._view.viewport().update()

    @staticmethod
    def _ensure_photo_id(model: ImageFileModel) -> str:
        photo_id = model.photo_id
        if not photo_id.strip():
            photo_id = model.analysis_path.strip()
            if not photo_id:
                raise ValueError(
                    "photo_id must be non-empty at the UI boundary"
                )
            model.photo_id = photo_id
        return photo_id
