"""Stable-ID state for the culling workspace, without filesystem access."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum
from typing import TYPE_CHECKING

from .analysis import AnalysisStatus
from .culling import PhotoMark, normalize_legacy_mark
from .technical_analysis import ExposureConclusion, FocusConclusion

if TYPE_CHECKING:
    from src.xuanpian.ui.image_model import ImageFileModel


class StatusFilter(str, Enum):
    UNPROCESSED = PhotoMark.UNPROCESSED.value
    KEEP = PhotoMark.KEEP.value
    HOLD = PhotoMark.HOLD.value
    DISCARD = PhotoMark.DISCARD.value
    ALL = "全部"


class AnalysisFilter(str, Enum):
    ALL = "全部"
    CLEAR = FocusConclusion.CLEAR.value
    OUT_OF_FOCUS = FocusConclusion.OUT_OF_FOCUS.value
    REVIEW = FocusConclusion.REVIEW.value
    EXPOSURE = "曝光异常"
    PORTRAIT = "人像"
    ANALYSIS_UNFINISHED = "分析未完成"


PhotoProvider = Callable[[str], "ImageFileModel"]


class CullingSession:
    """Filter stable photo IDs while retaining a bounded, capture-order cursor.

    ``counts()`` always returns a new dictionary with exactly the canonical
    persisted mark labels (``未处理``, ``保留``, ``待定``, ``舍弃``) as keys.
    Historical marks are normalized before filtering or counting. Unknown and
    unavailable photos are excluded from the queue and prevent completion.
    """

    def __init__(self, photo_ids: Iterable[str], provider: PhotoProvider) -> None:
        self._ordered_ids = tuple(photo_ids)
        if any(not isinstance(photo_id, str) or not photo_id for photo_id in self._ordered_ids):
            raise ValueError("photo IDs must be non-empty strings")
        if len(set(self._ordered_ids)) != len(self._ordered_ids):
            raise ValueError("duplicate photo IDs are not allowed")
        if not callable(provider):
            raise TypeError("provider must be callable")

        self._provider = provider
        self.status_filter = StatusFilter.UNPROCESSED
        self.analysis_filter = AnalysisFilter.ALL
        self._current_id: str | None = None
        self._cursor_hint = 0
        self._synchronize_current()

    @property
    def current_id(self) -> str | None:
        self._synchronize_current()
        return self._current_id

    @property
    def is_complete(self) -> bool:
        for photo_id in self._ordered_ids:
            model = self._model_for(photo_id)
            if model is None or not self._has_known_technical_status(model):
                return False
            mark = self._normalized_mark(model)
            if mark is None or mark is PhotoMark.UNPROCESSED:
                return False
        return True

    def visible_ids(self) -> list[str]:
        """Return matching stable IDs in the supplied capture order.

        Each provider lookup occurs at most once per photo for this evaluation,
        so a provider may safely expose a model whose mutable fields change
        between separate session operations.
        """
        visible: list[str] = []
        for photo_id in self._ordered_ids:
            model = self._model_for(photo_id)
            if (
                model is not None
                and self._matches_status(model)
                and self._matches_analysis(model)
            ):
                visible.append(photo_id)
        return visible

    def has_unfinished_analysis(self) -> bool:
        return any(
            model is not None
            and model.technical_status in {AnalysisStatus.FAILED, AnalysisStatus.CANCELED}
            for model in (self._model_for(photo_id) for photo_id in self._ordered_ids)
        )

    def set_filters(
        self, status_filter: StatusFilter, analysis_filter: AnalysisFilter
    ) -> None:
        if not isinstance(status_filter, StatusFilter):
            raise TypeError("status_filter must be a StatusFilter")
        if not isinstance(analysis_filter, AnalysisFilter):
            raise TypeError("analysis_filter must be an AnalysisFilter")
        self.status_filter = status_filter
        self.analysis_filter = analysis_filter
        self._synchronize_current()

    def advance_after_mark(self, completed_id: str) -> None:
        """Reconcile after a committed mark without wrapping the cursor."""
        index = self._index_of(completed_id)
        if index is not None:
            self._cursor_hint = index
        visible = self.visible_ids()
        if completed_id in visible:
            completed_index = visible.index(completed_id)
            self._current_id = visible[min(completed_index + 1, len(visible) - 1)]
            self._cursor_hint = self._index_of(self._current_id) or 0
            return
        self._synchronize_current(visible)

    def navigate(self, offset: int) -> str | None:
        """Move by ``offset`` visible photos, clamped at either boundary."""
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise TypeError("offset must be an integer")
        visible = self.visible_ids()
        self._synchronize_current(visible)
        if self._current_id is None:
            return None
        current_index = visible.index(self._current_id)
        target_index = min(max(current_index + offset, 0), len(visible) - 1)
        self._current_id = visible[target_index]
        self._cursor_hint = self._index_of(self._current_id) or 0
        return self._current_id

    def select_photo_id(self, photo_id: str) -> None:
        """Restore the current identity after a verified undo."""
        if photo_id not in self.visible_ids():
            raise ValueError("photo is outside the current filter")
        self._current_id = photo_id
        self._cursor_hint = self._index_of(photo_id) or 0

    def counts(self) -> dict[str, int]:
        """Return semantic mark counts keyed by all four canonical labels."""
        counts = {mark.value: 0 for mark in PhotoMark}
        for photo_id in self._ordered_ids:
            model = self._model_for(photo_id)
            if model is None:
                continue
            mark = self._normalized_mark(model)
            if mark is not None:
                counts[mark.value] += 1
        return counts

    def _synchronize_current(self, visible: list[str] | None = None) -> None:
        visible = self.visible_ids() if visible is None else visible
        if self._current_id in visible:
            current_index = self._index_of(self._current_id)
            if current_index is not None:
                self._cursor_hint = current_index
            return
        if not visible:
            self._current_id = None
            return

        for photo_id in visible:
            index = self._index_of(photo_id)
            if index is not None and index >= self._cursor_hint:
                self._current_id = photo_id
                return
        self._current_id = visible[-1]

    def _model_for(self, photo_id: str) -> ImageFileModel | None:
        try:
            model = self._provider(photo_id)
        except KeyError:
            return None
        if model is None or getattr(model, "photo_id", None) != photo_id:
            return None
        return model

    @staticmethod
    def _normalized_mark(model: ImageFileModel) -> PhotoMark | None:
        mark = getattr(model, "mark", None)
        return normalize_legacy_mark(mark) if isinstance(mark, str) else None

    def _matches_status(self, model: ImageFileModel) -> bool:
        mark = self._normalized_mark(model)
        if mark is None:
            return False
        if self.status_filter is StatusFilter.ALL:
            return True
        return mark.value == self.status_filter.value

    def _matches_analysis(self, model: ImageFileModel) -> bool:
        status = getattr(model, "technical_status", None)
        if not isinstance(status, AnalysisStatus):
            return False
        if self.analysis_filter is AnalysisFilter.ALL:
            return True
        if status in {AnalysisStatus.FAILED, AnalysisStatus.CANCELED}:
            return self.analysis_filter is AnalysisFilter.ANALYSIS_UNFINISHED
        if status is not AnalysisStatus.SUCCEEDED:
            return False

        if self.analysis_filter is AnalysisFilter.CLEAR:
            return model.focus_conclusion == FocusConclusion.CLEAR.value
        if self.analysis_filter is AnalysisFilter.OUT_OF_FOCUS:
            return model.focus_conclusion == FocusConclusion.OUT_OF_FOCUS.value
        if self.analysis_filter is AnalysisFilter.REVIEW:
            return model.focus_conclusion == FocusConclusion.REVIEW.value
        if self.analysis_filter is AnalysisFilter.EXPOSURE:
            return model.exposure_conclusion in {
                ExposureConclusion.OVEREXPOSED.value,
                ExposureConclusion.UNDEREXPOSED.value,
                ExposureConclusion.BOTH.value,
            }
        if self.analysis_filter is AnalysisFilter.PORTRAIT:
            return (
                model.portrait_assessed is True
                and isinstance(model.face_count, int)
                and not isinstance(model.face_count, bool)
                and model.face_count > 0
            )
        return False

    @staticmethod
    def _has_known_technical_status(model: ImageFileModel) -> bool:
        return isinstance(getattr(model, "technical_status", None), AnalysisStatus)

    def _index_of(self, photo_id: str | None) -> int | None:
        if photo_id is None:
            return None
        try:
            return self._ordered_ids.index(photo_id)
        except ValueError:
            return None
