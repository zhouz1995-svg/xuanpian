from __future__ import annotations

from copy import deepcopy
from importlib import import_module
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from src.xuanpian.domain.technical_analysis import TECHNICAL_ALGORITHM_VERSION


def _acceptance() -> object:
    return import_module("scripts.acceptance_technical_1000")


def _valid_report() -> dict[str, object]:
    return {
        "photos": 1000,
        "photos_analyzed": 1000,
        "elapsed_seconds": 42.0,
        "max_ui_heartbeat_ms": 18.0,
        "subject_backend": "fallback",
        "conclusion_counts": {
            "focus": {"清晰": 200, "建议复查": 800, "明显失焦": 0},
            "exposure": {"正常": 400, "过曝": 200, "欠曝": 200, "曝光异常": 0},
            "portrait": 0,
        },
        "portrait_assessment": {
            "assessed": 0,
            "unassessed": 1000,
            "assessed_coverage": 0.0,
        },
        "failures": 0,
        "failure_messages": [],
        "source_mutations": 0,
        "source_mutation_details": [],
    }


def test_acceptance_report_gate_passes_for_valid_report() -> None:
    acceptance = _acceptance()
    assert acceptance.validate_report(_valid_report()) == []


def test_acceptance_report_gate_rejects_each_failure_mode() -> None:
    acceptance = _acceptance()

    cases = {
        "elapsed_seconds": 901.0,
        "max_ui_heartbeat_ms": 201.0,
        "source_mutations": 1,
        "photos": 999,
        "photos_analyzed": 999,
    }
    for key, bad_value in cases.items():
        broken = deepcopy(_valid_report())
        broken[key] = bad_value  # type: ignore[index]
        errors = acceptance.validate_report(broken)
        assert any(key in error for error in errors), (key, errors)


def test_acceptance_report_gate_requires_conclusion_counts() -> None:
    acceptance = _acceptance()

    broken = deepcopy(_valid_report())
    broken["conclusion_counts"].pop("focus")  # type: ignore[union-attr]
    assert acceptance.validate_report(broken)

    missing = deepcopy(_valid_report())
    missing.pop("conclusion_counts")  # type: ignore[arg-type]
    assert acceptance.validate_report(missing)


def test_fallback_1000_report_requires_honest_unassessed_portrait_coverage() -> None:
    acceptance = _acceptance()

    missing = deepcopy(_valid_report())
    missing.pop("portrait_assessment")
    assert acceptance.validate_report(missing)

    dishonest = deepcopy(_valid_report())
    dishonest["portrait_assessment"] = {
        "assessed": 1000,
        "unassessed": 0,
        "assessed_coverage": 1.0,
    }
    assert any("fallback" in error for error in acceptance.validate_report(dishonest))


def test_fallback_technical_factory_builds_pipeline_without_crop_analyzer(
    tmp_path: Path,
) -> None:
    acceptance = _acceptance()
    pixels = np.full((96, 64, 3), 128, dtype=np.uint8)
    path = tmp_path / "sample.jpg"
    Image.fromarray(pixels).save(path)

    analyzer = acceptance._fallback_technical_factory()
    result = analyzer.analyze(str(path))

    assert result.algorithm_version == TECHNICAL_ALGORITHM_VERSION
    assert result.face_count == 0
    assert result.portrait_assessed is False
    assert result.exposure.conclusion.value
    assert result.focus.conclusion.value


def test_source_mutations_detects_content_and_count_changes(tmp_path: Path) -> None:
    acceptance = _acceptance()
    root = tmp_path / "photos"
    root.mkdir()
    (root / "a.jpg").write_bytes(b"a")
    (root / "b.jpg").write_bytes(b"b")
    before = acceptance._snapshot(root)

    (root / "b.jpg").write_bytes(b"changed")
    after = acceptance._snapshot(root)
    assert acceptance._source_mutations(before, after) == ["b.jpg"]

    (root / "c.jpg").write_bytes(b"c")
    after_added = acceptance._snapshot(root)
    mutations = acceptance._source_mutations(before, after_added)
    assert "b.jpg" in mutations
    assert "c.jpg" in mutations
    assert any("file count" in item for item in mutations)

    assert acceptance._source_mutations(after, after) == []
