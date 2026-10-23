"""Private calibration runner for the technical analysis pipeline.

Analyzes a real photo folder (JPEG preferred, RAW fallback, mirroring the
product's pairing rules), verifies the source folder is never modified, and
writes a private JSON report. Photo identities use stable integer indexes, but
the report still contains local folder and source names and must not be committed.

Usage:
    python scripts/calibrate_technical_samples.py /path/to/photos [--report out.json]

The subject backend defaults to the conservative fallback so the run works in
headless environments. Pass ``--onnx`` to exercise the real offline CPU models.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.exposure_analyzer import ExposureAnalyzer
from src.xuanpian.engine.focus_analyzer import FocusAnalyzer
from src.xuanpian.engine.subject_locator import SubjectLocator
from src.xuanpian.engine.technical_analyzer import TechnicalAnalyzer
from src.xuanpian.engine.vision_backend import (
    OnnxSubjectBackend,
    VisionCapabilityError,
)
from src.xuanpian.calibration_review import (
    build_dimension_review_rows,
    build_review_rows,
    read_dimension_review_rows,
    read_review_rows,
    summarize_dimension_review,
    summarize_review,
    write_dimension_review_rows,
    write_review_rows,
)
from src.xuanpian.services.import_service import ImportService


class _UnavailableSubjectBackend:
    def detect_faces(self, bgr: object) -> tuple[()]:
        raise VisionCapabilityError("subject capability unavailable")

    def detect_poses(self, bgr: object) -> tuple[()]:
        raise VisionCapabilityError("subject capability unavailable")

    def detect_objects(self, bgr: object) -> tuple[()]:
        raise VisionCapabilityError("subject capability unavailable")

    def close(self) -> None:
        return None


def _subject_review_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 20") from error
    if not 1 <= limit <= 20:
        raise argparse.ArgumentTypeError("must be between 1 and 20")
    return limit


def _analyzer(use_onnx: bool) -> TechnicalAnalyzer:
    backend = (
        OnnxSubjectBackend()
        if use_onnx
        else _UnavailableSubjectBackend()
    )
    return TechnicalAnalyzer(
        AnalysisContext.from_path,
        SubjectLocator(backend),
        FocusAnalyzer(),
        ExposureAnalyzer(),
    )


def _snapshot(root: Path) -> dict[str, object]:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    return {
        "count": len(files),
        "files": {
            str(path.relative_to(root)): (
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in files
        },
    }


def _mutations(before: dict[str, object], after: dict[str, object]) -> list[str]:
    changed: list[str] = []
    if before["count"] != after["count"]:
        changed.append(f"file count {before['count']} -> {after['count']}")
    before_files = before["files"]
    after_files = after["files"]
    for name in sorted(set(before_files) | set(after_files)):
        if before_files.get(name) != after_files.get(name):
            changed.append(name)
    return changed


def run_calibration(
    folder: Path,
    *,
    use_onnx: bool = False,
    max_workers: int = 4,
) -> dict[str, object]:
    if not folder.is_dir():
        raise ValueError(f"not a directory: {folder}")

    before = _snapshot(folder)
    service = ImportService()
    latest: dict[str, object] = {}
    for batch in service.scan(folder):
        for candidate in batch:
            for photo_id in candidate.superseded_photo_ids:
                latest.pop(photo_id, None)
            latest[candidate.photo_id] = candidate
    candidates = list(latest.values())
    analyzer = _analyzer(use_onnx)

    rows: list[dict[str, object]] = []
    started = time.perf_counter()
    failures: list[str] = []

    def analyze_one(candidate: object) -> dict[str, object]:
        path = str(candidate.analysis_path)
        result = analyzer.analyze(path)
        return {
            "path": path,
            "result": result,
        }

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            (candidate, pool.submit(analyze_one, candidate))
            for candidate in candidates
        ]
        for index, (candidate, future) in enumerate(futures):
            try:
                outcome = future.result()
            except Exception as error:
                failures.append(
                    f"photo-{index:04d}: {type(error).__name__}: {error}"
                )
                continue
            result = outcome["result"]
            rows.append(
                {
                    "index": f"photo-{index:04d}",
                    "source": Path(outcome["path"]).name,
                    "focus": result.focus.conclusion.value,
                    "exposure": result.exposure.conclusion.value,
                    "face_count": result.face_count,
                    "portrait_assessed": result.portrait_assessed,
                }
            )

    elapsed_seconds = round(time.perf_counter() - started, 3)
    after = _snapshot(folder)
    changed = _mutations(before, after)

    focus_counts: dict[str, int] = {}
    exposure_counts: dict[str, int] = {}
    portrait = 0
    portrait_assessed = 0
    for row in rows:
        focus_counts[row["focus"]] = focus_counts.get(row["focus"], 0) + 1
        exposure_counts[row["exposure"]] = (
            exposure_counts.get(row["exposure"], 0) + 1
        )
        assessed = bool(row["portrait_assessed"])
        portrait_assessed += int(assessed)
        portrait += int(assessed and row["face_count"] > 0)

    portrait_total = len(rows)

    return {
        "folder": str(folder),
        "source_files": before["count"],
        "photos": len(candidates),
        "analyzed": len(rows),
        "elapsed_seconds": elapsed_seconds,
        "subject_backend": "onnx" if use_onnx else "fallback",
        "conclusion_counts": {
            "focus": focus_counts,
            "exposure": exposure_counts,
            "portrait": portrait,
        },
        "portrait_assessment": {
            "assessed": portrait_assessed,
            "unassessed": portrait_total - portrait_assessed,
            "assessed_coverage": (
                round(portrait_assessed / portrait_total, 6)
                if portrait_total
                else 0.0
            ),
        },
        "failures": failures,
        "source_mutations": changed,
        "samples": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path, nargs="?")
    parser.add_argument("--report", type=Path, default=Path("/tmp/technical-calibration.json"))
    parser.add_argument("--onnx", action="store_true")
    parser.add_argument(
        "--review-template",
        type=Path,
        help="write a private CSV for human truth labels",
    )
    parser.add_argument(
        "--review-input",
        type=Path,
        help="read a completed private review CSV and print comparison metrics",
    )
    portrait_review = parser.add_mutually_exclusive_group()
    portrait_review.add_argument(
        "--portrait-review-template",
        type=Path,
        help="write a private CSV for independent portrait review",
    )
    portrait_review.add_argument(
        "--portrait-review-input",
        type=Path,
        help="read a completed private portrait review CSV",
    )
    parser.add_argument(
        "--per-focus",
        type=int,
        default=20,
        help="maximum review rows per predicted focus conclusion (default: 20)",
    )
    parser.add_argument(
        "--per-portrait",
        type=_subject_review_limit,
        default=20,
        help="maximum review rows per predicted portrait conclusion (default: 20)",
    )
    args = parser.parse_args()

    if args.folder is None:
        parser.error("folder is required")
    try:
        report = run_calibration(args.folder, use_onnx=args.onnx)
    except Exception as error:
        print(f"Calibration failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if args.review_template is not None:
        review_rows = build_review_rows(report, per_focus=args.per_focus)
        write_review_rows(args.review_template, review_rows)
    if args.portrait_review_template is not None:
        review_rows = build_dimension_review_rows(
            report, "portrait", per_group=args.per_portrait
        )
        write_dimension_review_rows(
            args.portrait_review_template, "portrait", review_rows
        )
    summary = {
        key: report[key]
        for key in (
            "source_files",
            "photos",
            "analyzed",
            "elapsed_seconds",
            "subject_backend",
            "conclusion_counts",
            "portrait_assessment",
            "failures",
            "source_mutations",
        )
    }
    if args.review_input is not None:
        summary["human_review"] = summarize_review(
            report,
            read_review_rows(args.review_input),
        )
    if args.portrait_review_input is not None:
        human_review = summary.setdefault("human_review", {})
        assert isinstance(human_review, dict)
        human_review["portrait"] = summarize_dimension_review(
            report,
            "portrait",
            read_dimension_review_rows(args.portrait_review_input, "portrait"),
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"report written to {args.report}")
    if args.review_template is not None:
        print(f"review template written to {args.review_template}")
    if args.portrait_review_template is not None:
        print(f"portrait review template written to {args.portrait_review_template}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
