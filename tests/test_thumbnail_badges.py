"""Trust-boundary tests for thumbnail analysis badges."""

from __future__ import annotations

import pytest

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.thumbnail_delegate import ThumbnailDelegate
from src.xuanpian.ui.thumbnail_list_delegate import _analysis_badges


def test_failed_retry_stale_values_do_not_produce_analysis_badge(qapp) -> None:
    model = ImageFileModel(
        "/fake/stale.jpg",
        technical_status=AnalysisStatus.FAILED,
        focus_conclusion="明显失焦",
        exposure_conclusion="过曝",
    )

    assert ThumbnailDelegate._badge_style(model) is None


def test_manual_badge_does_not_require_analysis(qapp) -> None:
    model = ImageFileModel("/fake/manual.jpg", mark="入选")

    style = ThumbnailDelegate._badge_style(model)

    assert style is not None
    assert style[1] == "✓"


def test_explicit_technical_exception_has_warning_or_error_badge(qapp) -> None:
    model = ImageFileModel(
        "/fake/soft.jpg", technical_status=AnalysisStatus.SUCCEEDED,
        focus_conclusion="明显失焦",
    )
    assert ThumbnailDelegate._badge_style(model)[1] == "✗"

    model.focus_conclusion = ""
    assert ThumbnailDelegate._badge_style(model) is None
    assert _analysis_badges(model) == []


@pytest.mark.parametrize(
    ("mark", "symbol"),
    (("入选", "✓"), ("保留", "✓"), ("淘汰", "✗"), ("舍弃", "✗")),
)
def test_thumbnail_delegate_badges_use_semantic_managed_marks(
    qapp,
    mark: str,
    symbol: str,
) -> None:
    model = ImageFileModel(f"/fake/{mark}.jpg", mark=mark)

    style = ThumbnailDelegate._badge_style(model)

    assert style is not None
    assert style[1] == symbol
