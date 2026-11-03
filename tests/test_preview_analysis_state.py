import pytest
from PyQt6.QtWidgets import QLabel

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.preview_widget import PreviewWidget


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ((("af_used", 1.0), ("af_x", 0.2), ("af_y", 0.2), ("af_width", 0.1), ("af_height", 0.1)), "AF 区域校验"),
        ((("subject_roi_x", 0.1), ("subject_roi_y", 0.1), ("subject_roi_width", 0.3), ("subject_roi_height", 0.3)), "主体区域"),
        ((("scene_regions", 5.0),), "场景图像回退"),
        ((("scene_insufficient_texture", 1.0),), "场景图像回退"),
        ((("subject_capability_unavailable", 1.0),), "依据不足"),
        ((("subject_ambiguous_or_unavailable", 1.0),), "依据不足"),
        ((("roi_too_small", 1.0),), "主体区域信息不足"),
        ((("texture", 2.0),), "依据不足"),
        ((), "未记录依据"),
        ((("future_source", 1.0),), "依据不足"),
        ((("af_used", 0.0),), "依据不足"),
        ((("af_used", 1.0),), "依据不足"),
        ((("af_review_invalid_input", 1.0),), "依据不足"),
        ((("af_used", 1.0), ("af_x", 0.9), ("af_y", 0.2), ("af_width", 0.3), ("af_height", 0.1)), "依据不足"),
        ((("af_used", 1.0), ("af_x", 0.2), ("af_y", 0.2), ("af_width", 0.1), ("af_height", 0.1), ("scene_regions", 5.0)), "依据不足"),
    ],
)
def test_focus_basis_uses_saved_evidence_in_both_panels(qapp, qtbot, evidence, expected):
    from src.xuanpian.domain.technical_analysis import (
        TECHNICAL_ALGORITHM_VERSION, TechnicalAnalysisResult, FocusResult,
        FocusConclusion, ExposureResultV2, ExposureConclusion,
    )
    from src.xuanpian.ui.main_window import MainWindow
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    result = TechnicalAnalysisResult(
        TECHNICAL_ALGORITHM_VERSION, FocusResult(FocusConclusion.REVIEW, evidence=evidence),
        ExposureResultV2(ExposureConclusion.NORMAL, 0.0, 0.0), 0, False,
    )
    model = ImageFileModel("synthetic.jpg", technical_status=AnalysisStatus.SUCCEEDED)
    MainWindow._apply_technical_result(model, result)
    preview, workspace = PreviewWidget(), CullingWorkspace()
    qtbot.addWidget(preview)
    qtbot.addWidget(workspace)
    preview._show_analysis(model)
    workspace._show_information(model)

    assert preview._focus_basis_value.text() == expected
    assert workspace._information_values["focus_basis"].text() == expected
    assert preview._focus_basis_value.toolTip()
    assert preview._focus_basis_value.toolTip() == workspace._information_values["focus_basis"].toolTip()
    assert model.focus_conclusion == "建议复查"

    MainWindow._clear_technical_result(model)
    model.technical_status = AnalysisStatus.PENDING
    preview._show_analysis(model)
    workspace._show_information(model)
    assert model.focus_evidence == ()
    assert preview._focus_basis_value.text() == "未分析"
    assert preview._focus_basis_value.toolTip() == ""
    assert workspace._information_values["focus_basis"].toolTip() == ""


@pytest.mark.parametrize(
    ("evidence", "explanation"),
    [
        ((("scene_regions", 5.0), ("af_unavailable_metadata_runtime_missing", 1.0)), "元数据工具不可用"),
        ((("subject_roi_x", 0.1), ("subject_roi_y", 0.1), ("subject_roi_width", 0.3), ("subject_roi_height", 0.3), ("af_unavailable_raw_mapping_unavailable", 1.0)), "RAW"),
        ((("af_used", 1.0), ("af_x", 0.2), ("af_y", 0.2), ("af_width", 0.1), ("af_height", 0.1), ("af_review_subject_conflict", 1.0)), "位置不一致"),
    ],
)
def test_focus_basis_explains_known_limitations_without_claiming_eye_sharpness(evidence, explanation):
    model = ImageFileModel("synthetic.jpg", technical_status=AnalysisStatus.SUCCEEDED)
    model.focus_evidence = evidence
    _, detail = model.focus_basis_info()
    assert explanation in detail
    assert "不代表相机实际对焦点" in detail or "不保证眼部或主体一定清晰" in detail


@pytest.mark.parametrize("status", [AnalysisStatus.PENDING, AnalysisStatus.RUNNING, AnalysisStatus.FAILED, AnalysisStatus.CANCELED])
def test_unfinished_focus_basis_never_reuses_stale_evidence(qapp, qtbot, status):
    from src.xuanpian.ui.culling_workspace import CullingWorkspace

    model = ImageFileModel("synthetic.jpg", technical_status=status)
    model.focus_evidence = (("scene_regions", 5.0),)
    preview, workspace = PreviewWidget(), CullingWorkspace()
    qtbot.addWidget(preview)
    qtbot.addWidget(workspace)
    preview._show_analysis(model)
    workspace._show_information(model)
    assert preview._focus_basis_value.text() in {"未分析", "分析中", "分析未完成"}
    assert workspace._information_values["focus_basis"].text() == preview._focus_basis_value.text()
    assert preview._focus_basis_value.toolTip() == ""
    assert workspace._information_values["focus_basis"].toolTip() == ""


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
