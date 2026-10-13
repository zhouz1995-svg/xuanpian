from __future__ import annotations

from collections.abc import Callable, Iterable

from PyQt6.QtCore import QItemSelectionModel, QModelIndex, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QVBoxLayout,
    QWidget,
)

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.photo_list_model import PhotoListModel, PhotoRoles
from src.xuanpian.ui.preview_widget import FilmstripDelegate


class CullingFilmstrip(QWidget):
    """A single virtualized vertical view keyed only by stable photo IDs."""

    photo_activated = pyqtSignal(str)

    def __init__(
        self,
        provider: Callable[[str], ImageFileModel | None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = PhotoListModel(provider, self)
        self._view = QListView(self)
        self._view.setModel(self._model)
        self._view.setViewMode(QListView.ViewMode.ListMode)
        self._view.setFlow(QListView.Flow.TopToBottom)
        self._view.setMovement(QListView.Movement.Static)
        self._view.setWrapping(False)
        self._view.setUniformItemSizes(True)
        self._view.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self._view.setItemDelegate(FilmstripDelegate(self._view))
        self._view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._view.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self._view.activated.connect(self._activate_index)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._view)

    def set_photo_ids(self, photo_ids: Iterable[str]) -> None:
        candidate_ids = list(photo_ids)
        if any(
            not isinstance(photo_id, str) or not photo_id.strip()
            for photo_id in candidate_ids
        ):
            raise ValueError("photo_id must be a non-empty string")
        if len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("photo_id values must be unique")
        selected_id = self.current_photo_id()
        available_ids = [
            photo_id
            for photo_id in candidate_ids
            if self._model.model_for_id(photo_id) is not None
        ]
        self._model.set_photo_ids(available_ids)
        self.select_photo_id(selected_id)

    def select_photo_id(self, photo_id: str | None) -> None:
        row = self._model.row_for_id(photo_id) if photo_id is not None else -1
        if photo_id is not None and self._model.model_for_id(photo_id) is None:
            row = -1
        selection_model = self._view.selectionModel()
        if row < 0:
            selection_model.clearSelection()
            self._view.setCurrentIndex(QModelIndex())
            return
        index = self._model.index(row, 0)
        selection_model.setCurrentIndex(
            index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Current,
        )
        self._view.scrollTo(
            index,
            QAbstractItemView.ScrollHint.EnsureVisible,
        )

    def current_photo_id(self) -> str | None:
        photo_id = self._view.currentIndex().data(int(PhotoRoles.ID))
        return photo_id if isinstance(photo_id, str) and photo_id else None

    def row_for_id(self, photo_id: str) -> int:
        return self._model.row_for_id(photo_id)

    def model_for_id(self, photo_id: str) -> ImageFileModel | None:
        return self._model.model_for_id(photo_id)

    def _activate_index(self, index: QModelIndex) -> None:
        photo_id = index.data(int(PhotoRoles.ID))
        if isinstance(photo_id, str) and photo_id:
            self.photo_activated.emit(photo_id)
