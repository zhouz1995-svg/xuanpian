from __future__ import annotations

from collections.abc import Callable

import pytest

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.domain.culling_session import (
    AnalysisFilter,
    CullingSession,
    StatusFilter,
)
from src.xuanpian.domain.technical_analysis import ExposureConclusion, FocusConclusion
from src.xuanpian.ui.image_model import ImageFileModel


def _model(
    photo_id: str,
    *,
    mark: str = "",
    technical_status: AnalysisStatus = AnalysisStatus.SUCCEEDED,
    focus: FocusConclusion = FocusConclusion.CLEAR,
    exposure: ExposureConclusion = ExposureConclusion.NORMAL,
    face_count: int = 0,
    portrait_assessed: bool = False,
) -> ImageFileModel:
    return ImageFileModel(
        path=f"/photos/{photo_id}.jpg",
        photo_id=photo_id,
        mark=mark,
        technical_status=technical_status,
        focus_conclusion=focus.value,
        exposure_conclusion=exposure.value,
        face_count=face_count,
        portrait_assessed=portrait_assessed,
    )


@pytest.fixture
def models() -> list[ImageFileModel]:
    return [_model("one"), _model("two"), _model("three")]


def _provider(models: list[ImageFileModel]) -> Callable[[str], ImageFileModel]:
    by_id = {model.photo_id: model for model in models}
    return by_id.__getitem__


def _session(models: list[ImageFileModel]) -> CullingSession:
    return CullingSession([model.photo_id for model in models], _provider(models))


def test_defaults_to_unprocessed_in_capture_order(models: list[ImageFileModel]) -> None:
    session = _session(models)

    assert session.status_filter is StatusFilter.UNPROCESSED
    assert session.analysis_filter is AnalysisFilter.ALL
    assert session.visible_ids() == ["one", "two", "three"]
    assert session.current_id == "one"


def test_successful_mark_removes_item_and_advances(models: list[ImageFileModel]) -> None:
    session = _session(models)
    first, second = session.visible_ids()[:2]
    models[0].mark = PhotoMark.KEEP.value

    session.advance_after_mark(first)

    assert session.current_id == second
    assert session.visible_ids() == ["two", "three"]


def test_successful_mark_advances_even_when_completed_photo_stays_visible(
    models: list[ImageFileModel],
) -> None:
    session = _session(models)
    session.set_filters(StatusFilter.ALL, AnalysisFilter.ALL)
    models[0].mark = PhotoMark.KEEP.value

    session.advance_after_mark("one")

    assert session.current_id == "two"


def test_combined_status_and_analysis_filters_are_intersected_in_capture_order() -> None:
    models = [
        _model("clear-unprocessed"),
        _model("blurred-unprocessed", focus=FocusConclusion.OUT_OF_FOCUS),
        _model("blurred-keep", mark="入选", focus=FocusConclusion.OUT_OF_FOCUS),
        _model("blurred-discard", mark="淘汰", focus=FocusConclusion.OUT_OF_FOCUS),
    ]
    session = _session(models)

    session.set_filters(StatusFilter.KEEP, AnalysisFilter.OUT_OF_FOCUS)

    assert session.visible_ids() == ["blurred-keep"]
    assert session.current_id == "blurred-keep"


@pytest.mark.parametrize("status", [AnalysisStatus.FAILED, AnalysisStatus.CANCELED])
def test_failed_or_canceled_analysis_appears_only_in_unfinished(
    status: AnalysisStatus,
) -> None:
    model = _model(
        "needs-human-review",
        technical_status=status,
        focus=FocusConclusion.CLEAR,
        exposure=ExposureConclusion.OVEREXPOSED,
        face_count=1,
        portrait_assessed=True,
    )
    session = _session([model])

    session.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.ANALYSIS_UNFINISHED)
    assert session.visible_ids() == ["needs-human-review"]

    for analysis_filter in (
        AnalysisFilter.REVIEW,
        AnalysisFilter.CLEAR,
        AnalysisFilter.OUT_OF_FOCUS,
        AnalysisFilter.EXPOSURE,
        AnalysisFilter.PORTRAIT,
    ):
        session.set_filters(StatusFilter.UNPROCESSED, analysis_filter)
        assert session.visible_ids() == []


def test_review_includes_successful_review_focus_and_all_includes_terminal_states() -> None:
    models = [
        _model("focus-review", focus=FocusConclusion.REVIEW),
        _model("failed", technical_status=AnalysisStatus.FAILED),
        _model("canceled", technical_status=AnalysisStatus.CANCELED),
    ]
    session = _session(models)

    session.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.REVIEW)
    assert session.visible_ids() == ["focus-review"]

    session.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.ALL)
    assert session.visible_ids() == ["focus-review", "failed", "canceled"]


def test_portrait_requires_successful_explicit_assessment_and_a_face() -> None:
    models = [
        _model("unassessed", face_count=1, portrait_assessed=False),
        _model("no-face", face_count=0, portrait_assessed=True),
        _model("portrait", face_count=1, portrait_assessed=True),
        _model(
            "failed",
            technical_status=AnalysisStatus.FAILED,
            face_count=1,
            portrait_assessed=True,
        ),
    ]
    session = _session(models)

    session.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.PORTRAIT)

    assert session.visible_ids() == ["portrait"]


def test_unknown_analysis_values_fail_closed() -> None:
    unknown_exposure = _model("unknown-exposure")
    unknown_exposure.exposure_conclusion = ""
    malformed_portrait = _model("malformed-portrait", portrait_assessed=True)
    malformed_portrait.face_count = "one"  # type: ignore[assignment]
    session = _session([unknown_exposure, malformed_portrait])

    session.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.EXPOSURE)
    assert session.visible_ids() == []

    session.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.PORTRAIT)
    assert session.visible_ids() == []


def test_pending_and_running_are_only_visible_through_all_analysis() -> None:
    models = [
        _model("pending", technical_status=AnalysisStatus.PENDING),
        _model("running", technical_status=AnalysisStatus.RUNNING),
    ]
    session = _session(models)

    assert session.visible_ids() == ["pending", "running"]

    for analysis_filter in AnalysisFilter:
        if analysis_filter is AnalysisFilter.ALL:
            continue
        session.set_filters(StatusFilter.UNPROCESSED, analysis_filter)
        assert session.visible_ids() == []


def test_unknown_technical_status_fails_closed_under_all_without_changing_marks() -> None:
    unknown = _model("unknown", mark=PhotoMark.KEEP.value)
    unknown.technical_status = "succeeded"  # type: ignore[assignment]
    pending = _model(
        "pending", mark=PhotoMark.HOLD.value, technical_status=AnalysisStatus.PENDING
    )
    running = _model(
        "running", mark=PhotoMark.DISCARD.value, technical_status=AnalysisStatus.RUNNING
    )
    session = _session([unknown, pending, running])

    session.set_filters(StatusFilter.ALL, AnalysisFilter.ALL)

    assert session.visible_ids() == ["pending", "running"]
    assert session.counts() == {"未处理": 0, "保留": 1, "待定": 1, "舍弃": 1}
    assert session.is_complete is False


def test_model_mutation_reclassifies_using_the_same_stable_photo_id() -> None:
    model = _model("stable", focus=FocusConclusion.CLEAR)
    session = _session([model])
    session.set_filters(StatusFilter.UNPROCESSED, AnalysisFilter.OUT_OF_FOCUS)

    assert session.visible_ids() == []
    model.focus_conclusion = FocusConclusion.OUT_OF_FOCUS.value

    assert session.visible_ids() == ["stable"]
    assert session.current_id == "stable"


def test_visibility_looks_up_each_photo_once_per_evaluation(
    models: list[ImageFileModel],
) -> None:
    calls: list[str] = []
    by_id = {model.photo_id: model for model in models}

    def provider(photo_id: str) -> ImageFileModel:
        calls.append(photo_id)
        return by_id[photo_id]

    session = CullingSession([model.photo_id for model in models], provider)
    calls.clear()

    assert session.visible_ids() == ["one", "two", "three"]
    assert calls == ["one", "two", "three"]


def test_navigation_clamps_at_bounds_without_wrapping(models: list[ImageFileModel]) -> None:
    session = _session(models)

    assert session.navigate(-1) == "one"
    assert session.current_id == "one"
    assert session.navigate(10) == "three"
    assert session.current_id == "three"
    assert session.navigate(1) == "three"
    assert session.current_id == "three"


def test_current_selection_moves_to_next_visible_item_when_current_disappears(
    models: list[ImageFileModel],
) -> None:
    session = _session(models)
    assert session.navigate(1) == "two"
    models[1].mark = PhotoMark.KEEP.value

    assert session.current_id == "three"


def test_empty_filters_have_no_current_selection_and_navigation_is_a_noop(
    models: list[ImageFileModel],
) -> None:
    session = _session(models)
    session.set_filters(StatusFilter.KEEP, AnalysisFilter.PORTRAIT)

    assert session.visible_ids() == []
    assert session.current_id is None
    assert session.navigate(1) is None


def test_counts_return_all_canonical_mark_labels_and_use_legacy_semantics() -> None:
    models = [
        _model("empty", mark=""),
        _model("legacy-keep", mark="入选"),
        _model("canonical-hold", mark=PhotoMark.HOLD.value),
        _model("legacy-discard", mark="淘汰"),
        _model("unknown", mark="external-state"),
    ]
    session = _session(models)

    assert session.counts() == {
        "未处理": 1,
        "保留": 1,
        "待定": 1,
        "舍弃": 1,
    }
    assert session.is_complete is False


def test_session_is_complete_only_when_every_known_photo_is_marked() -> None:
    models = [
        _model("keep", mark=PhotoMark.KEEP.value),
        _model("hold", mark=PhotoMark.HOLD.value),
        _model("discard", mark=PhotoMark.DISCARD.value),
    ]

    assert _session(models).is_complete is True


def test_unknown_or_missing_provider_photos_fail_closed_without_corrupting_cursor() -> None:
    known = _model("known", mark=PhotoMark.KEEP.value)
    session = CullingSession(["missing", "known"], {"known": known}.__getitem__)

    session.set_filters(StatusFilter.ALL, AnalysisFilter.ALL)

    assert session.visible_ids() == ["known"]
    assert session.current_id == "known"
    assert session.counts() == {"未处理": 0, "保留": 1, "待定": 0, "舍弃": 0}
    assert session.is_complete is False


def test_duplicate_or_blank_photo_ids_are_rejected() -> None:
    provider = {"one": _model("one")}.__getitem__

    with pytest.raises(ValueError, match="duplicate"):
        CullingSession(["one", "one"], provider)
    with pytest.raises(ValueError, match="non-empty"):
        CullingSession([""], provider)
