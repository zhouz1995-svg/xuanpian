from __future__ import annotations

from bisect import bisect_left
import logging
from collections.abc import Callable, Iterable
from enum import IntEnum
from typing import Protocol

from PyQt6.QtCore import QAbstractListModel, QModelIndex, Qt

from src.xuanpian.ui.image_model import ImageFileModel

logger = logging.getLogger(__name__)


class PhotoProvider(Protocol):
    def __call__(self, photo_id: str) -> ImageFileModel | None: ...


class PhotoRoles(IntEnum):
    ID = int(Qt.ItemDataRole.UserRole) + 1
    PATH = ID + 1
    FILENAME = PATH + 1
    THUMBNAIL = FILENAME + 1
    MARK = THUMBNAIL + 1
    ANALYSIS = MARK + 1
    ASSET_KIND = ANALYSIS + 1
    CAPTURE_TIME = ASSET_KIND + 1


class PhotoListModel(QAbstractListModel):
    """Qt list state whose only owned photo data is the current ID order."""

    def __init__(
        self,
        provider: PhotoProvider | Callable[[str], ImageFileModel | None],
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self._photo_ids: list[str] = []
        self._row_by_id: dict[str, int] = {}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._photo_ids)

    def roleNames(self) -> dict[int, bytes]:
        return {
            int(PhotoRoles.ID): b"photoId",
            int(PhotoRoles.PATH): b"path",
            int(PhotoRoles.FILENAME): b"filename",
            int(PhotoRoles.THUMBNAIL): b"thumbnail",
            int(PhotoRoles.MARK): b"mark",
            int(PhotoRoles.ANALYSIS): b"analysis",
            int(PhotoRoles.ASSET_KIND): b"assetKind",
            int(PhotoRoles.CAPTURE_TIME): b"captureTime",
        }

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if (
            not index.isValid()
            or index.column() != 0
            or not 0 <= index.row() < len(self._photo_ids)
        ):
            return None

        photo_id = self._photo_ids[index.row()]
        if role == PhotoRoles.ID:
            return photo_id

        model = self.model_for_id(photo_id)
        if model is None:
            if role in (
                Qt.ItemDataRole.DisplayRole,
                PhotoRoles.PATH,
                PhotoRoles.FILENAME,
                PhotoRoles.MARK,
                PhotoRoles.ASSET_KIND,
            ):
                return ""
            return None

        if role in (Qt.ItemDataRole.DisplayRole, PhotoRoles.FILENAME):
            return model.filename
        if role == PhotoRoles.PATH:
            return model.path
        if role == PhotoRoles.THUMBNAIL:
            return model.thumbnail
        if role == PhotoRoles.MARK:
            return model.mark
        if role == PhotoRoles.ANALYSIS:
            return model
        if role == PhotoRoles.ASSET_KIND:
            return self.asset_kind_for(model)
        if role == PhotoRoles.CAPTURE_TIME:
            return model.date_taken
        return None

    def set_photo_ids(self, photo_ids: Iterable[str]) -> None:
        new_ids = list(photo_ids)
        if any(
            not isinstance(photo_id, str) or not photo_id.strip()
            for photo_id in new_ids
        ):
            raise ValueError("photo_id must be a non-empty string")
        if len(set(new_ids)) != len(new_ids):
            raise ValueError("photo_id values must be unique")
        if new_ids == self._photo_ids:
            return

        old_ids = self._photo_ids
        shared_length = min(len(old_ids), len(new_ids))
        same_prefix = old_ids[:shared_length] == new_ids[:shared_length]
        if same_prefix and len(new_ids) > len(old_ids):
            self.beginInsertRows(
                QModelIndex(),
                len(old_ids),
                len(new_ids) - 1,
            )
            self._photo_ids.extend(new_ids[len(old_ids) :])
            self._rebuild_row_map()
            self.endInsertRows()
            return
        if same_prefix and len(new_ids) < len(old_ids):
            self.beginRemoveRows(
                QModelIndex(),
                len(new_ids),
                len(old_ids) - 1,
            )
            del self._photo_ids[len(new_ids) :]
            self._rebuild_row_map()
            self.endRemoveRows()
            return
        if len(new_ids) < len(old_ids) and self._is_subsequence(
            new_ids, old_ids
        ):
            kept = set(new_ids)
            removed_ranges: list[tuple[int, int]] = []
            first = -1
            for row, photo_id in enumerate(old_ids):
                if photo_id not in kept and first < 0:
                    first = row
                elif photo_id in kept and first >= 0:
                    removed_ranges.append((first, row - 1))
                    first = -1
            if first >= 0:
                removed_ranges.append((first, len(old_ids) - 1))
            for first, last in reversed(removed_ranges):
                self.beginRemoveRows(QModelIndex(), first, last)
                del self._photo_ids[first : last + 1]
                self._rebuild_row_map()
                self.endRemoveRows()
            return

        self.beginResetModel()
        self._photo_ids = new_ids
        self._rebuild_row_map()
        self.endResetModel()

    def update_photo(
        self,
        photo_id: str,
        changed_roles: Iterable[PhotoRoles | int],
    ) -> None:
        row = self.row_for_id(photo_id)
        roles = [int(role) for role in changed_roles]
        if row < 0 or not roles:
            return
        index = self.index(row, 0)
        self.dataChanged.emit(index, index, roles)

    def insert_photo_id(self, row: int, photo_id: str) -> None:
        if not isinstance(photo_id, str) or not photo_id.strip():
            raise ValueError("photo_id must be a non-empty string")
        if photo_id in self._row_by_id:
            raise ValueError("photo_id values must be unique")
        if not 0 <= row <= len(self._photo_ids):
            raise IndexError("photo row is out of range")
        self.beginInsertRows(QModelIndex(), row, row)
        self._photo_ids.insert(row, photo_id)
        self._rebuild_row_map()
        self.endInsertRows()

    def insert_photo_id_ordered(
        self,
        photo_id: str,
        order_by_id: dict[str, int],
    ) -> None:
        row = bisect_left(
            self._photo_ids,
            order_by_id[photo_id],
            key=order_by_id.__getitem__,
        )
        self.insert_photo_id(row, photo_id)

    def remove_photo_id(self, photo_id: str) -> bool:
        row = self.row_for_id(photo_id)
        if row < 0:
            return False
        self.beginRemoveRows(QModelIndex(), row, row)
        del self._photo_ids[row]
        self._rebuild_row_map()
        self.endRemoveRows()
        return True

    def photo_id_at(self, row: int) -> str | None:
        if 0 <= row < len(self._photo_ids):
            return self._photo_ids[row]
        return None

    def row_for_id(self, photo_id: str) -> int:
        return self._row_by_id.get(photo_id, -1)

    def photo_ids(self) -> list[str]:
        return list(self._photo_ids)

    def model_for_id(self, photo_id: str) -> ImageFileModel | None:
        """Resolve one stable ID without allowing provider faults into Qt."""
        try:
            model = self._provider(photo_id)
        except Exception as error:
            logger.warning(
                "photo provider failed for ID %s: %s",
                photo_id,
                type(error).__name__,
            )
            return None
        if not isinstance(model, ImageFileModel):
            logger.warning("photo provider has no valid record for ID %s", photo_id)
            return None
        if model.photo_id != photo_id:
            logger.warning("photo provider returned mismatched ID for %s", photo_id)
            return None
        return model

    def _rebuild_row_map(self) -> None:
        self._row_by_id = {
            photo_id: row for row, photo_id in enumerate(self._photo_ids)
        }

    @staticmethod
    def _is_subsequence(candidate: list[str], source: list[str]) -> bool:
        iterator = iter(source)
        return all(
            any(item == wanted for item in iterator)
            for wanted in candidate
        )

    @staticmethod
    def asset_kind_for(model: ImageFileModel) -> str:
        if model.jpeg_path and model.raw_path:
            return "RAW+JPEG"
        if model.raw_path and not model.jpeg_path:
            return "RAW"
        if model.jpeg_path:
            return "JPEG"
        return "RAW" if model.is_raw else ""
