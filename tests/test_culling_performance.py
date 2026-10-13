"""Measured acceptance for the 1,000-photo culling filter path."""

from __future__ import annotations

from time import perf_counter

from PyQt6.QtGui import QColor, QImage

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling_session import (
    AnalysisFilter,
    CullingSession,
    StatusFilter,
)
from src.xuanpian.ui.culling_workspace import CullingWorkspace
from src.xuanpian.ui.image_model import ImageFileModel


PHOTO_COUNT = 1_000
FILTER_BUDGET_SECONDS = 0.2


def _thousand_models() -> dict[str, ImageFileModel]:
    models: dict[str, ImageFileModel] = {}
    for row in range(PHOTO_COUNT):
        photo_id = f"photo-{row:04d}"
        model = ImageFileModel(
            f"/synthetic/{photo_id}.jpg",
            filename=f"{photo_id}.jpg",
            photo_id=photo_id,
            mark="未处理",
            technical_status=AnalysisStatus.SUCCEEDED,
            focus_conclusion=("清晰" if row % 2 == 0 else "明显失焦"),
        )
        models[photo_id] = model
    return models


def _image() -> QImage:
    image = QImage(16, 12, QImage.Format.Format_RGB32)
    image.fill(QColor("#203040"))
    return image


def test_thousand_photo_session_filter_is_bounded() -> None:
    models = _thousand_models()
    session = CullingSession(models, models.__getitem__)

    started = perf_counter()
    session.set_filters(
        StatusFilter.UNPROCESSED,
        AnalysisFilter.OUT_OF_FOCUS,
    )
    visible = session.visible_ids()
    elapsed = perf_counter() - started

    assert visible[0] == "photo-0001"
    assert visible[-1] == "photo-0999"
    assert len(visible) == 500
    assert elapsed < FILTER_BUDGET_SECONDS


def test_thousand_photo_workspace_filter_and_list_model_reset_is_bounded(
    qapp,
) -> None:
    models = _thousand_models()
    session = CullingSession(models, models.__getitem__)
    workspace = CullingWorkspace(
        provider=models.get,
        preview_decoder=lambda *_args: _image(),
    )
    workspace.set_session(session)
    workspace.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.CLEAR)
    resets = 0

    def count_reset() -> None:
        nonlocal resets
        resets += 1

    workspace._filmstrip._model.modelReset.connect(count_reset)
    try:
        started = perf_counter()
        workspace.set_filters(
            StatusFilter.UNPROCESSED,
            AnalysisFilter.OUT_OF_FOCUS,
        )
        elapsed = perf_counter() - started

        assert resets == 1
        assert workspace._filmstrip._model.rowCount() == 500
        assert workspace._filmstrip._model.photo_id_at(0) == "photo-0001"
        assert elapsed < FILTER_BUDGET_SECONDS
    finally:
        workspace.close()
        qapp.processEvents()
