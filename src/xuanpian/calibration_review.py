"""Private, repeatable human review data for technical-analysis calibration."""

from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path


class CalibrationReviewError(ValueError):
    """Raised when a calibration report or its human review is invalid."""


REVIEW_FIELDS = (
    "id",
    "source",
    "predicted_focus",
    "predicted_portrait",
    "truth_focus",
    "truth_portrait",
)

DIMENSION_REVIEW_FIELDS: Mapping[str, tuple[str, ...]] = {
    "portrait": ("id", "source", "predicted_portrait", "truth_portrait"),
}

_TRUTH_VALUES = {
    "focus": frozenset({"清晰", "建议复查", "明显失焦", "不确定", ""}),
    "portrait": frozenset({"有人像", "无人像", "不确定", ""}),
}

_PREDICTED_VALUES = {
    "portrait": frozenset({"有人像", "无人像", "不判断"}),
}

_TRUTH_ALIASES = {
    "truth_focus": {"失焦": "明显失焦"},
}


def build_review_rows(
    report: Mapping[str, object], *, per_focus: int = 20
) -> list[dict[str, str]]:
    """Build a stable, bounded review sample from an analysis report."""
    if per_focus < 1:
        raise CalibrationReviewError("per_focus must be at least 1")
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for sample in _samples(report):
        groups[sample["focus"]].append(_review_row(sample))
    rows: list[dict[str, str]] = []
    for focus in sorted(groups):
        rows.extend(sorted(groups[focus], key=_stable_row_key)[:per_focus])
    return rows


def build_dimension_review_rows(
    report: Mapping[str, object], dimension: str, *, per_group: int = 20
) -> list[dict[str, str]]:
    """Build a stable, bounded review sample for one prediction dimension."""
    fields = DIMENSION_REVIEW_FIELDS.get(dimension)
    if fields is None:
        raise CalibrationReviewError("unsupported review dimension")
    if not 1 <= per_group <= 20:
        raise CalibrationReviewError("per_group must be between 1 and 20")
    predicted_field = f"predicted_{dimension}"
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    samples = _samples(report)
    if not samples:
        raise CalibrationReviewError("no review samples")
    for sample in samples:
        row = _review_row(sample)
        prediction = row[predicted_field]
        if prediction not in _PREDICTED_VALUES[dimension]:
            raise CalibrationReviewError(
                f"unsupported {dimension} prediction: {prediction!r}"
            )
        groups[prediction].append(row)
    return [
        {field: row[field] for field in fields}
        for group in sorted(groups)
        for row in sorted(groups[group], key=_stable_row_key)[:per_group]
    ]


def write_review_rows(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    """Write the private review CSV only to the requested destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: str(row.get(field, "")) for field in REVIEW_FIELDS})


def write_dimension_review_rows(
    path: Path, dimension: str, rows: Sequence[Mapping[str, str]]
) -> None:
    """Write a review CSV containing only one prediction dimension."""
    fields = _dimension_review_fields(dimension)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: str(row.get(field, "")) for field in fields})


def read_review_rows(path: Path) -> list[dict[str, str]]:
    """Read a review CSV while preserving empty user labels."""
    with path.open("r", encoding="utf-8", newline="") as stream:
        values = list(csv.reader(stream))
    content_rows = [row for row in values if tuple(row) != REVIEW_FIELDS]
    if not content_rows or any(len(row) != len(REVIEW_FIELDS) for row in content_rows):
        raise CalibrationReviewError("review CSV has unexpected columns")
    return [
        {
            field: _TRUTH_ALIASES.get(field, {}).get(value.strip(), value.strip())
            for field, value in zip(REVIEW_FIELDS, row, strict=True)
        }
        for row in content_rows
    ]


def read_dimension_review_rows(path: Path, dimension: str) -> list[dict[str, str]]:
    """Read one dimension's review CSV while preserving empty user labels."""
    fields = _dimension_review_fields(dimension)
    with path.open("r", encoding="utf-8", newline="") as stream:
        values = list(csv.reader(stream))
    content_rows = [row for row in values if tuple(row) != fields]
    if not content_rows or any(len(row) != len(fields) for row in content_rows):
        raise CalibrationReviewError("review CSV has unexpected columns")
    return [
        {
            field: value.strip()
            for field, value in zip(fields, row, strict=True)
        }
        for row in content_rows
    ]


def summarize_review(
    report: Mapping[str, object], rows: Sequence[Mapping[str, str]]
) -> dict[str, dict[str, object]]:
    """Compare valid human labels with predictions without tuning the model."""
    expected = {sample["index"]: _review_row(sample) for sample in _samples(report)}
    seen: set[str] = set()
    dimensions = {
        "focus": ("predicted_focus", "truth_focus"),
        "portrait": ("predicted_portrait", "truth_portrait"),
    }
    result: dict[str, dict[str, object]] = {
        dimension: {
            "labeled": 0,
            "matches": 0,
            "accuracy": None,
            "confusion": {},
            "mismatches": [],
        }
        for dimension in dimensions
    }
    portrait_predictions = [
        row["predicted_portrait"] for row in expected.values()
    ]
    portrait_assessed = sum(
        prediction != "不判断" for prediction in portrait_predictions
    )
    portrait_total = len(portrait_predictions)
    result["portrait"].update(
        {
            "assessed": portrait_assessed,
            "unassessed": portrait_total - portrait_assessed,
            "assessed_coverage": (
                portrait_assessed / portrait_total if portrait_total else 0.0
            ),
            "unassessed_labeled": 0,
        }
    )
    for row in rows:
        photo_id = str(row.get("id", "")).strip()
        if not photo_id or photo_id not in expected:
            raise CalibrationReviewError(f"unknown review id: {photo_id!r}")
        if photo_id in seen:
            raise CalibrationReviewError(f"duplicate review id: {photo_id}")
        seen.add(photo_id)
        expected_row = expected[photo_id]
        for dimension, (predicted_field, truth_field) in dimensions.items():
            truth = str(row.get(truth_field, "")).strip()
            if truth not in _TRUTH_VALUES[dimension]:
                raise CalibrationReviewError(
                    f"invalid {truth_field} for {photo_id}: {truth!r}"
                )
            if truth in {"", "不确定"}:
                continue
            predicted = expected_row[predicted_field]
            summary = result[dimension]
            if dimension == "portrait" and predicted == "不判断":
                summary["unassessed_labeled"] = (
                    int(summary["unassessed_labeled"]) + 1
                )
                continue
            summary["labeled"] = int(summary["labeled"]) + 1
            if predicted == truth:
                summary["matches"] = int(summary["matches"]) + 1
            else:
                cast_mismatches = summary["mismatches"]
                assert isinstance(cast_mismatches, list)
                cast_mismatches.append(photo_id)
            cast_confusion = summary["confusion"]
            assert isinstance(cast_confusion, dict)
            truth_counts = cast_confusion.setdefault(truth, {})
            truth_counts[predicted] = truth_counts.get(predicted, 0) + 1
    for summary in result.values():
        labeled = int(summary["labeled"])
        summary["accuracy"] = (
            int(summary["matches"]) / labeled if labeled else None
        )
    return result


def summarize_dimension_review(
    report: Mapping[str, object], dimension: str, rows: Sequence[Mapping[str, str]]
) -> dict[str, object]:
    """Summarize one dimension using the same label rules as full reviews."""
    fields = _dimension_review_fields(dimension)
    truth_field = fields[-1]
    expected = {sample["index"]: _review_row(sample) for sample in _samples(report)}
    expanded_rows: list[dict[str, str]] = []
    for row in rows:
        photo_id = str(row.get("id", "")).strip()
        if not photo_id or photo_id not in expected:
            raise CalibrationReviewError(f"unknown review id: {photo_id!r}")
        expanded_row = dict(expected[photo_id])
        expanded_row[truth_field] = str(row.get(truth_field, "")).strip()
        expanded_rows.append(expanded_row)
    return summarize_review(report, expanded_rows)[dimension]


def _samples(report: Mapping[str, object]) -> list[dict[str, object]]:
    samples = report.get("samples")
    if not isinstance(samples, list):
        raise CalibrationReviewError("report samples must be a list")
    normalized: list[dict[str, object]] = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise CalibrationReviewError("report sample must be a mapping")
        if "portrait_assessed" not in sample:
            raise CalibrationReviewError(
                "report sample is missing portrait_assessed"
            )
        required = (
            "index",
            "source",
            "focus",
            "face_count",
        )
        if any(key not in sample for key in required):
            raise CalibrationReviewError("report sample is missing a required value")
        if not isinstance(sample["index"], str) or not isinstance(sample["source"], str):
            raise CalibrationReviewError("report sample identifiers must be strings")
        if not isinstance(sample["focus"], str):
            raise CalibrationReviewError("report conclusions must be strings")
        if (
            not isinstance(sample["face_count"], int)
            or isinstance(sample["face_count"], bool)
            or sample["face_count"] < 0
        ):
            raise CalibrationReviewError("report face_count must be a non-negative integer")
        if not isinstance(sample["portrait_assessed"], bool):
            raise CalibrationReviewError("report portrait_assessed must be a boolean")
        normalized.append(sample)
    return normalized


def _review_row(sample: Mapping[str, object]) -> dict[str, str]:
    if not bool(sample["portrait_assessed"]):
        predicted_portrait = "不判断"
    elif int(sample["face_count"]) > 0:
        predicted_portrait = "有人像"
    else:
        predicted_portrait = "无人像"
    return {
        "id": str(sample["index"]),
        "source": str(sample["source"]),
        "predicted_focus": str(sample["focus"]),
        "predicted_portrait": predicted_portrait,
        "truth_focus": "",
        "truth_portrait": "",
    }


def _stable_row_key(row: Mapping[str, str]) -> tuple[str, str]:
    return (
        hashlib.sha256(row["id"].encode("utf-8")).hexdigest(),
        row["id"],
    )


def _dimension_review_fields(dimension: str) -> tuple[str, ...]:
    fields = DIMENSION_REVIEW_FIELDS.get(dimension)
    if fields is None:
        raise CalibrationReviewError("unsupported review dimension")
    return fields
