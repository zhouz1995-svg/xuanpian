from __future__ import annotations

import pytest

from src.xuanpian.domain.technical_analysis import (
    ExposureConclusion,
    FocusConclusion,
    TECHNICAL_ALGORITHM_VERSION,
    TechnicalAnalysisResult,
)


def test_public_conclusions_are_stable() -> None:
    assert FocusConclusion.CLEAR.value == "清晰"
    assert FocusConclusion.REVIEW.value == "建议复查"
    assert FocusConclusion.OUT_OF_FOCUS.value == "明显失焦"
    assert ExposureConclusion.BOTH.value == "曝光异常"


def _legacy_payload(version: str) -> dict[str, object]:
    return {
        "algorithm_version": version,
        "focus": {
            "conclusion": "建议复查",
            "confidence": 0.0,
            "evidence": [],
        },
        "exposure": {
            "conclusion": "正常",
            "overexposed_pct": 0.0,
            "underexposed_pct": 0.0,
        },
        "framing": {
            "conclusion": "不判断",
            "confidence": 0.0,
            "reason": "",
        },
        "face_count": 0,
    }


def test_v8_round_trip_omits_framing_and_persists_portrait_assessed() -> None:
    result = TechnicalAnalysisResult.review_only()
    payload = result.to_dict()

    assert payload["algorithm_version"] == "technical-v8"
    assert payload["portrait_assessed"] is False
    assert "framing" not in payload
    assert TechnicalAnalysisResult.from_dict(payload) == result


def test_v8_result_rejects_legacy_framing_key() -> None:
    payload = TechnicalAnalysisResult.review_only().to_dict()
    payload["framing"] = {"conclusion": "不判断"}

    with pytest.raises(ValueError, match="framing"):
        TechnicalAnalysisResult.from_dict(payload)


@pytest.mark.parametrize("version", ["technical-v5", "technical-v6", "technical-v7"])
def test_prior_current_format_results_remain_readable_but_are_historical(
    version: str,
) -> None:
    payload = TechnicalAnalysisResult.review_only().to_dict()
    payload["algorithm_version"] = version

    restored = TechnicalAnalysisResult.from_dict(payload)

    assert restored.algorithm_version == version
    assert restored.algorithm_version != TECHNICAL_ALGORITHM_VERSION


@pytest.mark.parametrize(
    ("section", "key"),
    [
        (None, "face_count"),
        ("focus", "conclusion"),
        ("exposure", "overexposed_pct"),
    ],
)
def test_result_rejects_missing_required_payload_values_as_value_errors(
    section: str | None,
    key: str,
) -> None:
    payload = TechnicalAnalysisResult.review_only().to_dict()
    container = payload if section is None else payload[section]
    assert isinstance(container, dict)
    del container[key]

    with pytest.raises(ValueError):
        TechnicalAnalysisResult.from_dict(payload)


@pytest.mark.parametrize("version", ["technical-v3", "technical-v4"])
def test_legacy_result_is_readable_but_portrait_is_unassessed(version: str) -> None:
    payload = _legacy_payload(version)

    restored = TechnicalAnalysisResult.from_dict(payload)

    assert restored.algorithm_version == version
    assert restored.portrait_assessed is False
    assert restored.algorithm_version != TECHNICAL_ALGORITHM_VERSION


def test_legacy_result_rejects_malformed_framing() -> None:
    payload = _legacy_payload("technical-v3")
    payload["framing"] = {"conclusion": "not-a-conclusion"}

    with pytest.raises(ValueError):
        TechnicalAnalysisResult.from_dict(payload)


def test_result_rejects_unknown_algorithm_version() -> None:
    payload = TechnicalAnalysisResult.review_only().to_dict()
    payload["algorithm_version"] = "technical-v0"

    with pytest.raises(ValueError, match="algorithm version"):
        TechnicalAnalysisResult.from_dict(payload)
