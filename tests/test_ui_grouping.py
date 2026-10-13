"""Technical-conclusion grouping tests."""

from __future__ import annotations

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.thumbnail_browser import GROUPS, ThumbnailBrowser


def _model(**values: object) -> ImageFileModel:
    path = str(values.pop("path", "/fake/photo.jpg"))
    return ImageFileModel(
        path,
        technical_status=AnalysisStatus.SUCCEEDED,
        **values,
    )


def test_groups_expose_only_user_facing_technical_conclusions() -> None:
    assert tuple(GROUPS) == (
        "all", "clear", "out_of_focus", "review", "exposure", "portrait"
    )


def test_groups_match_only_explicit_conclusions() -> None:
    assert ThumbnailBrowser.classify_group(_model(focus_conclusion="清晰"), "clear")
    assert ThumbnailBrowser.classify_group(_model(focus_conclusion="明显失焦"), "out_of_focus")
    assert ThumbnailBrowser.classify_group(_model(focus_conclusion="建议复查"), "review")
    assert ThumbnailBrowser.classify_group(_model(exposure_conclusion="过曝"), "exposure")
    assert ThumbnailBrowser.classify_group(
        _model(face_count=1, portrait_assessed=True), "portrait"
    )


def test_portrait_group_requires_a_successful_assessment_with_a_face() -> None:
    assert not ThumbnailBrowser.classify_group(
        _model(face_count=1, portrait_assessed=False), "portrait"
    )
    assert not ThumbnailBrowser.classify_group(
        _model(face_count=0, portrait_assessed=True), "portrait"
    )
    assert not ThumbnailBrowser.classify_group(
        _model(face_count=0, portrait_assessed=False), "portrait"
    )


def test_pending_or_failed_technical_results_are_not_classified() -> None:
    stale = ImageFileModel(
        "/fake/stale.jpg", technical_status=AnalysisStatus.FAILED,
        focus_conclusion="清晰", exposure_conclusion="过曝", face_count=1,
        portrait_assessed=True,
    )
    assert not ThumbnailBrowser.classify_group(stale, "clear")
    assert not ThumbnailBrowser.classify_group(stale, "exposure")
    assert not ThumbnailBrowser.classify_group(stale, "portrait")


def test_group_counts_allow_one_photo_to_match_multiple_conclusions(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models([
        _model(path="/fake/clear.jpg", focus_conclusion="清晰"),
        _model(
            path="/fake/soft.jpg",
            focus_conclusion="明显失焦",
            exposure_conclusion="欠曝",
            face_count=1,
            portrait_assessed=True,
        ),
    ])
    counts = browser._compute_group_counts()
    assert counts["all"] == 2
    assert counts["clear"] == 1
    assert counts["out_of_focus"] == 1
    assert counts["exposure"] == 1
    assert counts["portrait"] == 1
