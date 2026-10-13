"""Tests for objective, conservative exposure conclusions."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.xuanpian.domain.technical_analysis import ExposureConclusion
from src.xuanpian.engine.exposure_analyzer import ExposureAnalyzer


@pytest.mark.parametrize(
    ("over", "under", "expected"),
    [
        (1.0, 1.0, ExposureConclusion.NORMAL),
        (20.0, 1.0, ExposureConclusion.OVEREXPOSED),
        (1.0, 40.0, ExposureConclusion.UNDEREXPOSED),
        (20.0, 40.0, ExposureConclusion.BOTH),
    ],
)
def test_exposure_conclusions(
    over: float, under: float, expected: ExposureConclusion
) -> None:
    assert ExposureAnalyzer.classify_percentages(over, under) is expected


def test_black_letterbox_and_small_lamp_do_not_trigger_abnormal() -> None:
    gray = np.full((100, 100), 128, dtype=np.uint8)
    gray[:10, :] = 0
    gray[50:52, 50:52] = 255

    result = ExposureAnalyzer().analyze_context(SimpleNamespace(gray=gray))

    assert result.conclusion is ExposureConclusion.NORMAL
