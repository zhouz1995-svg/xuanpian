from __future__ import annotations

import csv
from copy import deepcopy
from importlib import import_module
import json
import sys
from types import SimpleNamespace

import pytest

from src.xuanpian.calibration_review import (
    CalibrationReviewError,
    DIMENSION_REVIEW_FIELDS,
    REVIEW_FIELDS,
    build_dimension_review_rows,
    build_review_rows,
    read_dimension_review_rows,
    read_review_rows,
    summarize_dimension_review,
    summarize_review,
    write_dimension_review_rows,
)
from src.xuanpian.engine.technical_analyzer import TechnicalAnalyzer


def _report() -> dict[str, object]:
    return {
        "source_files": 4,
        "photos": 4,
        "analyzed": 4,
        "elapsed_seconds": 1.0,
        "subject_backend": "fallback",
        "conclusion_counts": {},
        "portrait_assessment": {
            "assessed": 2,
            "unassessed": 2,
            "assessed_coverage": 0.5,
        },
        "failures": [],
        "source_mutations": [],
        "samples": [
            {
                "index": "photo-0000",
                "source": "IMG_0000.JPG",
                "focus": "清晰",
                "face_count": 1,
                "portrait_assessed": True,
            },
            {
                "index": "photo-0001",
                "source": "IMG_0001.JPG",
                "focus": "清晰",
                "face_count": 0,
                "portrait_assessed": True,
            },
            {
                "index": "photo-0002",
                "source": "IMG_0002.JPG",
                "focus": "建议复查",
                "face_count": 0,
                "portrait_assessed": False,
            },
            {
                "index": "photo-0003",
                "source": "IMG_0003.JPG",
                "focus": "明显失焦",
                "face_count": 1,
                "portrait_assessed": False,
            },
        ]
    }


def test_calibration_factory_builds_pipeline_without_crop_analyzer() -> None:
    calibration = import_module("scripts.calibrate_technical_samples")

    analyzer = calibration._analyzer(False)

    assert isinstance(analyzer, TechnicalAnalyzer)


def test_calibration_report_persists_portrait_assessment_and_coverage(
    monkeypatch, tmp_path
) -> None:
    calibration = import_module("scripts.calibrate_technical_samples")
    folder = tmp_path / "photos"
    folder.mkdir()
    candidates = tuple(
        SimpleNamespace(
            photo_id=f"photo-{index}",
            superseded_photo_ids=(),
            analysis_path=folder / f"photo-{index}.jpg",
        )
        for index in range(4)
    )
    results = {
        "photo-0.jpg": (0, False),
        "photo-1.jpg": (0, True),
        "photo-2.jpg": (2, False),
        "photo-3.jpg": (1, True),
    }

    class FakeImportService:
        def scan(self, _folder):
            yield candidates

    class FakeAnalyzer:
        def analyze(self, path):
            face_count, portrait_assessed = results[path.rsplit("/", 1)[-1]]
            return SimpleNamespace(
                focus=SimpleNamespace(
                    conclusion=SimpleNamespace(value="建议复查")
                ),
                exposure=SimpleNamespace(
                    conclusion=SimpleNamespace(value="正常")
                ),
                face_count=face_count,
                portrait_assessed=portrait_assessed,
            )

    monkeypatch.setattr(calibration, "ImportService", FakeImportService)
    monkeypatch.setattr(calibration, "_analyzer", lambda _enabled: FakeAnalyzer())

    report = calibration.run_calibration(folder)

    assert [sample["portrait_assessed"] for sample in report["samples"]] == [
        False,
        True,
        False,
        True,
    ]
    assert report["conclusion_counts"]["portrait"] == 1
    assert report["portrait_assessment"] == {
        "assessed": 2,
        "unassessed": 2,
        "assessed_coverage": 0.5,
    }


def _write_rows_with_header_at_end(path, fields, rows) -> None:
    path.write_text(
        "\n".join(",".join(row[field] for field in fields) for row in rows)
        + "\n"
        + ",".join(fields)
        + "\n",
        encoding="utf-8",
    )


def test_review_rows_sample_each_focus_group_deterministically() -> None:
    first = build_review_rows(_report(), per_focus=1)
    second = build_review_rows(_report(), per_focus=1)

    assert first == second
    assert len(first) == 3
    assert {row["predicted_focus"] for row in first} == {
        "清晰",
        "建议复查",
        "明显失焦",
    }
    assert all(row["truth_focus"] == "" for row in first)


def test_dimension_review_rows_sample_every_portrait_prediction() -> None:
    rows = build_dimension_review_rows(_report(), "portrait", per_group=1)

    assert {row["predicted_portrait"] for row in rows} == {
        "有人像",
        "无人像",
        "不判断",
    }
    assert all(tuple(row) == DIMENSION_REVIEW_FIELDS["portrait"] for row in rows)


@pytest.mark.parametrize(
    ("photo_id", "expected"),
    [
        ("photo-0000", "有人像"),
        ("photo-0001", "无人像"),
        ("photo-0002", "不判断"),
        ("photo-0003", "不判断"),
    ],
)
def test_portrait_prediction_requires_successful_assessment(
    photo_id, expected
) -> None:
    rows = build_review_rows(_report(), per_focus=20)

    assert {row["id"]: row["predicted_portrait"] for row in rows}[photo_id] == expected


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false"])
def test_review_samples_reject_missing_or_non_boolean_portrait_assessment(
    value
) -> None:
    report = deepcopy(_report())
    sample = report["samples"][0]
    if value is None:
        sample.pop("portrait_assessed")
    else:
        sample["portrait_assessed"] = value

    with pytest.raises(CalibrationReviewError, match="portrait_assessed"):
        build_review_rows(report)


def test_review_schema_supports_only_focus_and_portrait() -> None:
    assert REVIEW_FIELDS == (
        "id",
        "source",
        "predicted_focus",
        "predicted_portrait",
        "truth_focus",
        "truth_portrait",
    )
    assert DIMENSION_REVIEW_FIELDS == {
        "portrait": ("id", "source", "predicted_portrait", "truth_portrait")
    }


def test_framing_is_not_a_supported_review_dimension() -> None:
    with pytest.raises(CalibrationReviewError, match="unsupported review dimension"):
        build_dimension_review_rows(_report(), "framing")


@pytest.mark.parametrize("per_group", [0, 21])
def test_dimension_review_rows_reject_out_of_range_group_limit(per_group) -> None:
    with pytest.raises(
        CalibrationReviewError, match="per_group must be between 1 and 20"
    ):
        build_dimension_review_rows(_report(), "portrait", per_group=per_group)


def test_dimension_review_rows_reject_empty_samples() -> None:
    report = _report()
    report["samples"] = []

    with pytest.raises(CalibrationReviewError, match="no review samples"):
        build_dimension_review_rows(report, "portrait")


def test_portrait_review_reader_accepts_header_at_end_and_reports_mismatch(
    tmp_path,
) -> None:
    rows = build_dimension_review_rows(_report(), "portrait", per_group=20)
    mismatch = next(row for row in rows if row["predicted_portrait"] == "无人像")
    mismatch["truth_portrait"] = "有人像"
    path = tmp_path / "portrait.csv"
    _write_rows_with_header_at_end(path, DIMENSION_REVIEW_FIELDS["portrait"], rows)

    summary = summarize_dimension_review(
        _report(), "portrait", read_dimension_review_rows(path, "portrait")
    )

    assert summary["labeled"] == 1
    assert summary["mismatches"] == [mismatch["id"]]


def test_dimension_review_writer_uses_only_requested_dimension_fields(tmp_path) -> None:
    rows = build_dimension_review_rows(_report(), "portrait", per_group=20)
    path = tmp_path / "portrait.csv"

    write_dimension_review_rows(path, "portrait", rows)

    assert path.read_text(encoding="utf-8").splitlines()[0] == (
        "id,source,predicted_portrait,truth_portrait"
    )
    assert read_dimension_review_rows(path, "portrait") == rows


def test_review_summary_ignores_unknown_labels_and_reports_mismatches() -> None:
    rows = build_review_rows(_report(), per_focus=10)
    rows_by_id = {row["id"]: row for row in rows}
    rows_by_id["photo-0000"]["truth_focus"] = "明显失焦"
    rows_by_id["photo-0000"]["truth_portrait"] = "有人像"
    rows_by_id["photo-0001"]["truth_focus"] = "不确定"
    rows_by_id["photo-0001"]["truth_portrait"] = "有人像"

    summary = summarize_review(_report(), rows)

    assert summary["focus"]["labeled"] == 1
    assert summary["focus"]["matches"] == 0
    assert summary["focus"]["mismatches"] == ["photo-0000"]
    assert summary["portrait"]["labeled"] == 2
    assert summary["portrait"]["mismatches"] == ["photo-0001"]


def test_unassessed_portrait_label_is_excluded_from_accuracy_and_confusion() -> None:
    rows = build_review_rows(_report(), per_focus=20)
    rows_by_id = {row["id"]: row for row in rows}
    rows_by_id["photo-0002"]["truth_portrait"] = "无人像"

    summary = summarize_review(_report(), rows)["portrait"]

    assert summary["labeled"] == 0
    assert summary["matches"] == 0
    assert summary["accuracy"] is None
    assert summary["confusion"] == {}
    assert summary["mismatches"] == []
    assert summary["unassessed_labeled"] == 1


def test_portrait_truth_rejects_unassessed_prediction_label() -> None:
    rows = build_review_rows(_report(), per_focus=20)
    rows[0]["truth_portrait"] = "不判断"

    with pytest.raises(CalibrationReviewError, match="truth_portrait"):
        summarize_review(_report(), rows)


def test_mixed_portrait_summary_reports_coverage_and_assessed_accuracy() -> None:
    rows = build_review_rows(_report(), per_focus=20)
    rows_by_id = {row["id"]: row for row in rows}
    rows_by_id["photo-0000"]["truth_portrait"] = "有人像"
    rows_by_id["photo-0001"]["truth_portrait"] = "有人像"
    rows_by_id["photo-0002"]["truth_portrait"] = "无人像"
    rows_by_id["photo-0003"]["truth_portrait"] = "有人像"

    summary = summarize_review(_report(), rows)["portrait"]

    assert summary == {
        "assessed": 2,
        "unassessed": 2,
        "assessed_coverage": 0.5,
        "labeled": 2,
        "unassessed_labeled": 2,
        "matches": 1,
        "accuracy": 0.5,
        "confusion": {"有人像": {"有人像": 1, "无人像": 1}},
        "mismatches": ["photo-0001"],
    }


def test_all_unassessed_portraits_cannot_report_accuracy() -> None:
    report = deepcopy(_report())
    for sample in report["samples"]:
        sample["portrait_assessed"] = False
    rows = build_review_rows(report, per_focus=20)
    for row in rows:
        row["truth_portrait"] = "有人像"

    summary = summarize_review(report, rows)["portrait"]

    assert summary["assessed"] == 0
    assert summary["unassessed"] == 4
    assert summary["assessed_coverage"] == 0.0
    assert summary["labeled"] == 0
    assert summary["unassessed_labeled"] == 4
    assert summary["accuracy"] is None
    assert summary["confusion"] == {}


def test_review_summary_accepts_review_as_a_human_focus_conclusion() -> None:
    rows = build_review_rows(_report(), per_focus=10)
    rows_by_id = {row["id"]: row for row in rows}
    rows_by_id["photo-0001"]["truth_focus"] = "建议复查"

    summary = summarize_review(_report(), rows)

    assert summary["focus"]["labeled"] == 1
    assert summary["focus"]["matches"] == 0
    assert summary["focus"]["confusion"] == {"建议复查": {"清晰": 1}}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows[0].__setitem__("id", "unknown"),
        lambda rows: rows.append(dict(rows[0])),
        lambda rows: rows[0].__setitem__("truth_focus", "很清晰"),
    ],
)
def test_review_summary_rejects_invalid_review_rows(mutation) -> None:
    rows = build_review_rows(_report(), per_focus=10)
    mutation(rows)

    with pytest.raises(CalibrationReviewError):
        summarize_review(_report(), rows)


def test_read_review_rows_accepts_header_at_end_and_normalizes_blur_alias(
    tmp_path,
) -> None:
    rows = build_review_rows(_report(), per_focus=10)
    rows[0]["truth_focus"] = "失焦"
    path = tmp_path / "review.csv"
    path.write_text(
        "\n".join(
            ",".join(row[field] for field in REVIEW_FIELDS)
            for row in rows
        )
        + "\n"
        + ",".join(REVIEW_FIELDS)
        + "\n",
        encoding="utf-8",
    )

    loaded = read_review_rows(path)

    assert len(loaded) == len(rows)
    assert loaded[0]["truth_focus"] == "明显失焦"


def test_calibration_cli_writes_requested_review_template(
    monkeypatch, tmp_path, capsys
) -> None:
    calibration = import_module("scripts.calibrate_technical_samples")
    source = tmp_path / "photos"
    source.mkdir()
    report_path = tmp_path / "report.json"
    template_path = tmp_path / "review.csv"
    monkeypatch.setattr(calibration, "run_calibration", lambda *_args, **_kwargs: _report())
    monkeypatch.setattr(
        "sys.argv",
        [
            "calibrate_technical_samples.py",
            str(source),
            "--report",
            str(report_path),
            "--review-template",
            str(template_path),
            "--per-focus",
            "1",
        ],
    )

    assert calibration.main() == 0

    assert report_path.exists()
    assert template_path.exists()
    assert len(template_path.read_text(encoding="utf-8").splitlines()) == 4
    assert json.loads(report_path.read_text(encoding="utf-8")) == _report()
    assert "review template written" in capsys.readouterr().out

    monkeypatch.setattr(
        "sys.argv",
        [
            "calibrate_technical_samples.py",
            str(source),
            "--report",
            str(report_path),
            "--review-input",
            str(template_path),
        ],
    )

    assert calibration.main() == 0

    assert '"human_review"' in capsys.readouterr().out


def test_calibration_cli_rejects_template_and_input_for_same_dimension(
    monkeypatch, tmp_path
) -> None:
    calibration = import_module("scripts.calibrate_technical_samples")
    review_path = tmp_path / "review.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate_technical_samples.py",
            str(tmp_path),
            "--portrait-review-template",
            str(review_path),
            "--portrait-review-input",
            str(review_path),
        ],
    )

    with pytest.raises(SystemExit) as error:
        calibration.main()

    assert error.value.code == 2


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--per-portrait", "0"),
        ("--per-portrait", "21"),
    ],
)
def test_calibration_cli_rejects_out_of_range_subject_review_limit(
    monkeypatch, tmp_path, option, value
) -> None:
    calibration = import_module("scripts.calibrate_technical_samples")
    monkeypatch.setattr(
        sys,
        "argv",
        ["calibrate_technical_samples.py", str(tmp_path), option, value],
    )

    with pytest.raises(SystemExit) as error:
        calibration.main()

    assert error.value.code == 2


def test_calibration_cli_round_trips_portrait_review(
    monkeypatch, tmp_path, capsys
) -> None:
    calibration = import_module("scripts.calibrate_technical_samples")
    monkeypatch.setattr(calibration, "run_calibration", lambda *_args, **_kwargs: _report())
    portrait = tmp_path / "portrait.csv"
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate_technical_samples.py",
            str(tmp_path),
            "--report",
            str(report_path),
            "--portrait-review-template",
            str(portrait),
            "--per-portrait",
            "1",
        ],
    )

    assert calibration.main() == 0
    capsys.readouterr()

    with portrait.open(encoding="utf-8", newline="") as stream:
        portrait_rows = list(csv.DictReader(stream))
    assert tuple(portrait_rows[0]) == DIMENSION_REVIEW_FIELDS["portrait"]
    assert len(portrait_rows) == 3
    assert all(
        sum(row["predicted_portrait"] == prediction for row in portrait_rows) <= 1
        for prediction in {row["predicted_portrait"] for row in portrait_rows}
    )
    for row in portrait_rows:
        row["truth_portrait"] = (
            row["predicted_portrait"]
            if row["predicted_portrait"] != "不判断"
            else "有人像"
        )
    with portrait.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=DIMENSION_REVIEW_FIELDS["portrait"]
        )
        writer.writeheader()
        writer.writerows(portrait_rows)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate_technical_samples.py",
            str(tmp_path),
            "--report",
            str(report_path),
            "--portrait-review-input",
            str(portrait),
        ],
    )

    assert calibration.main() == 0

    summary, _end = json.JSONDecoder().raw_decode(capsys.readouterr().out)
    assert summary["human_review"]["portrait"]["labeled"] == 2
    assert summary["human_review"]["portrait"]["matches"] == 2
    assert summary["human_review"]["portrait"]["unassessed_labeled"] == 1
    assert summary["human_review"]["portrait"]["assessed_coverage"] == 0.5
    assert summary["human_review"]["portrait"]["accuracy"] == 1.0


def test_calibration_cli_rejects_removed_framing_review_option(
    monkeypatch, tmp_path
) -> None:
    calibration = import_module("scripts.calibrate_technical_samples")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "calibrate_technical_samples.py",
            str(tmp_path),
            "--framing-review-template",
            str(tmp_path / "framing.csv"),
        ],
    )

    with pytest.raises(SystemExit) as error:
        calibration.main()

    assert error.value.code == 2


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--af-review-template", "af-review.csv"),
        ("--af-review-input", "af-review.csv"),
        ("--af-metadata-input", "af-metadata.json"),
        ("--af-report", "af-report.json"),
        ("--af-review-limit", "30"),
    ],
)
def test_calibration_cli_rejects_removed_af_crop_options_before_analysis(
    monkeypatch, tmp_path, capsys, option, value
) -> None:
    calibration = import_module("scripts.calibrate_technical_samples")

    def unexpected_calibration(*_args, **_kwargs):
        raise AssertionError("removed AF options must fail before analysis")

    monkeypatch.setattr(calibration, "run_calibration", unexpected_calibration)
    monkeypatch.setattr(
        sys,
        "argv",
        ["calibrate_technical_samples.py", str(tmp_path), option, value],
    )

    with pytest.raises(SystemExit) as error:
        calibration.main()

    assert error.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err
