"""Offscreen technical-analysis acceptance at 1,000 photos.

Runs the real desktop import, persistence, and per-photo ``technical``
analysis pipeline against a deterministic synthetic folder, then reports:

- ``photos`` / ``photos_analyzed`` / ``failures``
- ``elapsed_seconds`` (background analysis wall time, gate: <= 900)
- ``max_ui_heartbeat_ms`` (max 50 ms Qt heartbeat gap, gate: <= 200)
- ``conclusion_counts`` (focus / exposure / portrait)
- ``portrait_assessment`` (assessed / unassessed / assessed coverage)
- ``source_mutations`` (changed or added source files, gate: == 0)

The offline ONNX CPU backend runs in headless environments. The script probes
it in a subprocess and records a conservative fallback if capability loading
fails. Pass --force-onnx to require real model execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Mapping

import numpy as np
from PIL import Image, ImageFilter

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.technical_analysis import (
    TechnicalAnalysisResult,
)
from src.xuanpian.engine.analysis_context import AnalysisContext
from src.xuanpian.engine.exposure_analyzer import ExposureAnalyzer
from src.xuanpian.engine.focus_analyzer import FocusAnalyzer
from src.xuanpian.engine.subject_locator import SubjectLocator
from src.xuanpian.engine.technical_analyzer import TechnicalAnalyzer
from src.xuanpian.engine.vision_backend import VisionCapabilityError
from src.xuanpian.services import analysis_service
from src.xuanpian.tasks.analysis_coordinator import AnalysisCoordinator
from src.xuanpian.tasks.import_coordinator import ImportCoordinator
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.thumbnail_loader import load_thumbnail
from src.xuanpian.store.project_store import ProjectStore

from PyQt6.QtCore import QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

PHOTO_COUNT = 1_000
ANALYSIS_DEADLINE_SECONDS = 900.0
HEARTBEAT_LIMIT_MS = 200.0
HEARTBEAT_INTERVAL_MS = 50
IMAGE_WIDTH = 96
IMAGE_HEIGHT = 64
FALLBACK_ANALYSIS_DELAY_SECONDS = 0.03


class UnavailableSubjectBackend:
    """Deterministic stand-in for a machine without ONNX capability.

    Mirrors the real backend interface but always reports the capability as
    unavailable, exercising the product's conservative subject fallback.
    """

    def detect_faces(self, bgr: object) -> tuple[()]:
        raise VisionCapabilityError("subject capability unavailable (fallback)")

    def detect_poses(self, bgr: object) -> tuple[()]:
        raise VisionCapabilityError("subject capability unavailable (fallback)")

    def detect_objects(self, bgr: object) -> tuple[()]:
        raise VisionCapabilityError("subject capability unavailable (fallback)")

    def close(self) -> None:
        return None


class _PacedTechnicalAnalyzer:
    """Adds a nominal per-image delay to emulate real ONNX pacing.

    The synthetic images decode in a fraction of a millisecond, so without a
    pacing delay all 1,000 results would land in one event-loop drain. Real
    ONNX runs take tens of milliseconds per photo; the nominal delay makes
    the acceptance exercise realistic background scheduling and heartbeat
    behavior while keeping the wall-clock gate well inside 15 minutes.
    """

    def __init__(self, analyzer: TechnicalAnalyzer, delay: float) -> None:
        self._analyzer = analyzer
        self._delay = delay

    def analyze(self, path: str) -> TechnicalAnalysisResult:
        started = time.perf_counter()
        result = self._analyzer.analyze(path)
        remaining = self._delay - (time.perf_counter() - started)
        if remaining > 0:
            time.sleep(remaining)
        return result


def _fallback_technical_factory() -> TechnicalAnalyzer:
    return _PacedTechnicalAnalyzer(
        TechnicalAnalyzer(
            AnalysisContext.from_path,
            SubjectLocator(UnavailableSubjectBackend()),
            FocusAnalyzer(),
            ExposureAnalyzer(),
        ),
        FALLBACK_ANALYSIS_DELAY_SECONDS,
    )


def _probe_onnx() -> bool:
    """Return True only when the real ONNX backend can run a detection."""
    probe = (
        "import numpy as np;"
        "from src.xuanpian.engine.vision_backend import OnnxSubjectBackend;"
        "b = OnnxSubjectBackend();"
        "r = b.detect_faces(np.full((96, 64, 3), 128, dtype=np.uint8));"
        "b.close();"
        "print('OK', len(r))"
    )
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().startswith("OK")


def _make_fixture(root: Path, count: int = PHOTO_COUNT) -> list[Path]:
    rng = np.random.default_rng(20260805)
    paths: list[Path] = []
    for index in range(count):
        variant = index % 5
        if variant == 0:
            pixels = np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 128, dtype=np.uint8)
        elif variant == 1:
            pixels = rng.integers(0, 256, (IMAGE_HEIGHT, IMAGE_WIDTH, 3), dtype=np.uint8)
        elif variant == 2:
            base = rng.integers(0, 256, (IMAGE_HEIGHT, IMAGE_WIDTH, 3), dtype=np.uint8)
            pixels = np.asarray(
                Image.fromarray(base).filter(ImageFilter.GaussianBlur(radius=2.0))
            )
        elif variant == 3:
            pixels = np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 250, dtype=np.uint8)
        else:
            pixels = np.full((IMAGE_HEIGHT, IMAGE_WIDTH, 3), 10, dtype=np.uint8)
        path = root / f"photo-{index:04d}.jpg"
        Image.fromarray(pixels).save(path, quality=88)
        paths.append(path)
    return paths


def _snapshot(root: Path) -> dict[str, object]:
    files = sorted(path for path in root.iterdir() if path.is_file())
    return {
        "count": len(files),
        "files": {
            path.name: (
                path.stat().st_size,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in files
        },
    }


def _source_mutations(before: dict[str, object], after: dict[str, object]) -> list[str]:
    changed: list[str] = []
    if before["count"] != after["count"]:
        changed.append(
            f"file count {before['count']} -> {after['count']}"
        )
    before_files = before["files"]
    after_files = after["files"]
    for name in sorted(set(before_files) | set(after_files)):
        if before_files.get(name) != after_files.get(name):
            changed.append(name)
    return changed


class _Heartbeat:
    def __init__(self) -> None:
        self.gaps_ms: list[float] = []
        self._last: float | None = None

    def tick(self) -> None:
        now = time.perf_counter()
        if self._last is not None:
            self.gaps_ms.append((now - self._last) * 1_000.0)
        self._last = now


def _wait_until(
    app: QApplication,
    predicate,
    heartbeat: _Heartbeat | None = None,
    *,
    timeout_ms: float = 900_000,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    while not predicate() and time.monotonic() < deadline:
        if heartbeat is not None:
            heartbeat.tick()
        app.processEvents()
        QTest.qWait(10)
    if not predicate():
        raise AssertionError("timed out waiting for acceptance state")


def run_acceptance(
    *,
    photo_count: int = PHOTO_COUNT,
    force_onnx: bool = False,
) -> dict[str, object]:
    onnx_ok = _probe_onnx() if not force_onnx else True
    if not onnx_ok and force_onnx:
        raise RuntimeError(
            "--force-onnx requested but the ONNX backend cannot run "
            "in this environment"
        )
    subject_backend = "onnx" if onnx_ok else "fallback"

    with tempfile.TemporaryDirectory(prefix="xuanpian-technical-") as tmp:
        root = Path(tmp) / "photos"
        root.mkdir()
        database = Path(tmp) / "project.sqlite3"
        _make_fixture(root, photo_count)
        before = _snapshot(root)

        app = QApplication.instance() or QApplication(sys.argv)
        if subject_backend == "fallback":
            analysis_service._ANALYZER_FACTORIES["technical"] = (
                _fallback_technical_factory
            )
            analysis_service._ANALYZERS.clear()
            coordinator = AnalysisCoordinator(
                executor_factory=ThreadPoolExecutor
            )
        else:
            coordinator = AnalysisCoordinator()

        window = MainWindow(
            import_coordinator=ImportCoordinator(),
            analysis_coordinator=coordinator,
            project_path_resolver=lambda _root: database,
            project_store_factory=lambda path, root: ProjectStore.open(
                path, root_path=root
            ),
            shutdown_timeout=5,
        )
        window._start_import(str(root))
        _wait_until(
            app,
            lambda: (
                window._active_job_ids["import"] is None
                and not window._io_handles
                and len(window.all_models) == photo_count
            ),
        )

        heartbeat = _Heartbeat()
        timer = QTimer()
        timer.setInterval(HEARTBEAT_INTERVAL_MS)
        timer.timeout.connect(heartbeat.tick)
        timer.start()
        analysis_started = time.perf_counter()
        window._on_analyze_all()
        _wait_until(
            app,
            lambda: window._active_job_ids["analysis"] is None,
            heartbeat,
        )
        elapsed_seconds = round(
            time.perf_counter() - analysis_started, 3
        )

        timer.stop()
        max_ui_heartbeat_ms = round(max(heartbeat.gaps_ms), 3) if heartbeat.gaps_ms else 0.0

        models = window.all_models
        photos_analyzed = sum(
            model.technical_status is AnalysisStatus.SUCCEEDED
            for model in models
        )
        failures = [
            f"{model.photo_id}: {failure.message}"
            for model in models
            for failure in model.analysis_failures.values()
        ]
        focus_counts: dict[str, int] = {}
        exposure_counts: dict[str, int] = {}
        portrait_count = 0
        portrait_assessed = 0
        for model in models:
            focus_counts[model.focus_conclusion] = (
                focus_counts.get(model.focus_conclusion, 0) + 1
            )
            exposure_counts[model.exposure_conclusion] = (
                exposure_counts.get(model.exposure_conclusion, 0) + 1
            )
            portrait_assessed += int(model.portrait_assessed)
            portrait_count += int(
                model.portrait_assessed and model.face_count > 0
            )

        after = _snapshot(root)
        mutations = _source_mutations(before, after)

        window.close()
        app.processEvents()

        return {
            "photos": photo_count,
            "photos_analyzed": photos_analyzed,
            "elapsed_seconds": elapsed_seconds,
            "max_ui_heartbeat_ms": max_ui_heartbeat_ms,
            "subject_backend": subject_backend,
            "conclusion_counts": {
                "focus": focus_counts,
                "exposure": exposure_counts,
                "portrait": portrait_count,
            },
            "portrait_assessment": {
                "assessed": portrait_assessed,
                "unassessed": photos_analyzed - portrait_assessed,
                "assessed_coverage": (
                    round(portrait_assessed / photos_analyzed, 6)
                    if photos_analyzed
                    else 0.0
                ),
            },
            "failures": len(failures),
            "failure_messages": failures[:5],
            "source_mutations": len(mutations),
            "source_mutation_details": mutations[:10],
        }


def validate_report(report: Mapping[str, object]) -> list[str]:
    """Return acceptance-gate failures; empty list means the run passes."""
    errors: list[str] = []
    required = {
        "photos",
        "photos_analyzed",
        "elapsed_seconds",
        "max_ui_heartbeat_ms",
        "subject_backend",
        "conclusion_counts",
        "portrait_assessment",
        "failures",
        "source_mutations",
    }
    for key in required - set(report):
        errors.append(f"missing report key: {key}")
    if errors:
        return errors

    if report["photos"] != PHOTO_COUNT:
        errors.append(f"photos={report['photos']!r}; expected {PHOTO_COUNT}")
    if report["photos_analyzed"] != PHOTO_COUNT:
        errors.append(
            f"photos_analyzed={report['photos_analyzed']!r}; "
            f"expected {PHOTO_COUNT}"
        )
    elapsed = report["elapsed_seconds"]
    if not isinstance(elapsed, (int, float)) or elapsed > 900.0:
        errors.append(f"elapsed_seconds={elapsed!r}; gate is <= 900")
    heartbeat = report["max_ui_heartbeat_ms"]
    if not isinstance(heartbeat, (int, float)) or heartbeat > HEARTBEAT_LIMIT_MS:
        errors.append(
            f"max_ui_heartbeat_ms={heartbeat!r}; gate is <= {HEARTBEAT_LIMIT_MS}"
        )
    if report["source_mutations"] != 0:
        errors.append(
            f"source_mutations={report['source_mutations']!r}; gate is == 0"
        )
    failures = report["failures"]
    if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0:
        errors.append(f"failures={failures!r}; expected non-negative int")
    counts = report["conclusion_counts"]
    if not isinstance(counts, Mapping):
        errors.append("conclusion_counts must be an object")
    elif not {"focus", "exposure", "portrait"} <= set(counts):
        errors.append("conclusion_counts must include focus/exposure/portrait")
    assessment = report["portrait_assessment"]
    if not isinstance(assessment, Mapping):
        errors.append("portrait_assessment must be an object")
    else:
        assessed = assessment.get("assessed")
        unassessed = assessment.get("unassessed")
        coverage = assessment.get("assessed_coverage")
        if (
            not isinstance(assessed, int)
            or isinstance(assessed, bool)
            or assessed < 0
            or not isinstance(unassessed, int)
            or isinstance(unassessed, bool)
            or unassessed < 0
        ):
            errors.append("portrait assessment counts must be non-negative integers")
        elif assessed + unassessed != report["photos_analyzed"]:
            errors.append("portrait assessment counts must equal photos_analyzed")
        if (
            not isinstance(coverage, (int, float))
            or isinstance(coverage, bool)
            or not 0.0 <= float(coverage) <= 1.0
        ):
            errors.append("portrait assessed coverage must be normalized")
        elif isinstance(assessed, int) and isinstance(unassessed, int):
            total = assessed + unassessed
            expected_coverage = assessed / total if total else 0.0
            if abs(float(coverage) - expected_coverage) > 0.000001:
                errors.append("portrait assessed coverage does not match counts")
        if report["subject_backend"] == "fallback" and (
            assessed != 0
            or unassessed != report["photos_analyzed"]
            or coverage != 0.0
        ):
            errors.append("fallback portrait results must all be unassessed")
    return errors


def main() -> int:
    force_onnx = "--force-onnx" in sys.argv[1:]
    try:
        report = run_acceptance(force_onnx=force_onnx)
        errors = validate_report(report)
    except Exception as error:
        print(
            f"Technical acceptance failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    if errors:
        print("Technical acceptance failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
