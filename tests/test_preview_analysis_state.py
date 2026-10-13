import pytest
from PyQt6.QtWidgets import QLabel

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.preview_widget import PreviewWidget


def test_preview_has_no_crop_analysis_display(qapp) -> None:
    widget = PreviewWidget()
    titles = {
        label.text()
        for label in widget.findChildren(QLabel)
        if label.property("infoTitle")
    }

    assert "人物边缘" not in titles

@pytest.mark.parametrize(
    ("status", "portrait_assessed", "face_count", "expected"),
    (
        (AnalysisStatus.SUCCEEDED, False, 0, "不判断"),
        (AnalysisStatus.SUCCEEDED, False, 1, "不判断"),
        (AnalysisStatus.SUCCEEDED, True, 0, "未检测到人像"),
        (AnalysisStatus.SUCCEEDED, True, 1, "检测到人像"),
        (AnalysisStatus.FAILED, True, 1, "不判断"),
        (AnalysisStatus.PENDING, True, 1, "不判断"),
        (AnalysisStatus.RUNNING, True, 1, "不判断"),
        (AnalysisStatus.CANCELED, True, 1, "不判断"),
    ),
)
def test_preview_portrait_copy_reflects_only_successfully_assessed_results(
    qapp,
    status: AnalysisStatus,
    portrait_assessed: bool,
    face_count: int,
    expected: str,
) -> None:
    widget = PreviewWidget()
    widget._show_analysis(
        ImageFileModel(
            "a.jpg",
            technical_status=status,
            portrait_assessed=portrait_assessed,
            face_count=face_count,
        )
    )

    assert widget._portrait_value.text() == expected


def test_pending_technical_analysis_is_shown_consistently(qapp) -> None:
    widget = PreviewWidget()
    widget._show_analysis(ImageFileModel("/tmp/a.jpg"))
    assert widget._blur_value.text() == "未分析"
    assert widget._exposure_value.text() == "未分析"
    assert widget._portrait_value.text() == "不判断"
