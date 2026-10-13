"""Headless P1 acceptance.

Run the separate offscreen virtualized UI acceptance with:
``QT_QPA_PLATFORM=offscreen PYTHONPATH=. python scripts/acceptance_ui_1000.py``.
Keeping it separate preserves this P1 filesystem/import acceptance entry point.
"""

from __future__ import annotations

import json
import tempfile
import time
import tracemalloc
from pathlib import Path
from threading import Event
from uuid import uuid4

from PIL import Image

from src.xuanpian.services.import_service import ImportService
from src.xuanpian.services.export_service import (
    ExportItem,
    ExportMode,
    ExportRequest,
    ExportService,
    ExportUnit,
)
from src.xuanpian.tasks.contracts import JobFinished, JobState
from src.xuanpian.tasks.import_coordinator import ImportBatch, ImportCoordinator
from src.xuanpian.tasks.io_coordinator import (
    IoCoordinator,
    MetadataRequest,
    MetadataResult,
)


PAIRED = 400
JPEG_ONLY = 500
RAW_ONLY = 100
BATCH_SIZE = 50


def _make_fixture(root: Path) -> list[Path]:
    paths: list[Path] = []
    image = Image.new("RGB", (16, 12), "navy")
    for index in range(PAIRED):
        jpeg = root / f"paired_{index:04d}.JPG"
        raw = root / f"paired_{index:04d}.NEF"
        image.save(jpeg, quality=75)
        raw.touch()
        paths.extend((jpeg, raw))
    for index in range(JPEG_ONLY):
        jpeg = root / f"jpeg_{index:04d}.JPG"
        image.save(jpeg, quality=75)
        paths.append(jpeg)
    for index in range(RAW_ONLY):
        raw = root / f"raw_{index:04d}.NEF"
        raw.touch()
        paths.append(raw)
    return paths


def _run_import(root: Path) -> dict[str, float | int]:
    tracemalloc.start()
    start = time.perf_counter()
    batches = ImportService().scan(root, BATCH_SIZE)
    first_batch = next(batches)
    first_batch_seconds = time.perf_counter() - start
    latest = {}
    updates = 0

    def fold(batch) -> None:
        nonlocal updates
        for candidate in batch:
            updates += 1
            for photo_id in candidate.superseded_photo_ids:
                latest.pop(photo_id, None)
            latest[candidate.photo_id] = candidate

    fold(first_batch)
    for batch in batches:
        fold(batch)
    total_seconds = time.perf_counter() - start
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    paired = sum(len(item.source_paths) == 2 for item in latest.values())
    jpeg_only = sum(
        bool(item.jpeg_path and not item.raw_path)
        for item in latest.values()
    )
    raw_only = sum(item.is_raw_only for item in latest.values())
    assert len(first_batch) == BATCH_SIZE
    assert len(latest) == PAIRED + JPEG_ONLY + RAW_ONLY
    assert (paired, jpeg_only, raw_only) == (PAIRED, JPEG_ONLY, RAW_ONLY)
    assert all(
        item.analysis_path == item.jpeg_path
        for item in latest.values()
        if item.jpeg_path and item.raw_path
    )
    return {
        "logical_photos": len(latest),
        "filesystem_files": PAIRED * 2 + JPEG_ONLY + RAW_ONLY,
        "candidate_updates": updates,
        "first_batch_ms": round(first_batch_seconds * 1000, 2),
        "total_scan_ms": round(total_seconds * 1000, 2),
        "peak_memory_mb": round(peak_bytes / 1024 / 1024, 2),
        "paired": paired,
        "jpeg_only": jpeg_only,
        "raw_only": raw_only,
    }


def _run_metadata(root: Path) -> dict[str, float | int]:
    jpeg_paths = sorted(root.glob("*.JPG"))
    project_id = uuid4()
    job_id = uuid4()
    requests = tuple(
        MetadataRequest(
            project_id,
            job_id,
            f"jpeg-{index}",
            str(path),
            (120, 90),
        )
        for index, path in enumerate(jpeg_paths)
    )
    results = 0
    first_result_seconds = None
    start = time.perf_counter()

    def on_event(event: object) -> None:
        nonlocal results, first_result_seconds
        if isinstance(event, MetadataResult):
            results += 1
            if first_result_seconds is None:
                first_result_seconds = time.perf_counter() - start

    coordinator = IoCoordinator(max_workers=4)
    handle = coordinator.submit_metadata(requests, on_event)
    assert handle.wait(30)
    total_seconds = time.perf_counter() - start
    coordinator.shutdown()
    assert results == len(jpeg_paths)
    return {
        "jpeg_metadata_count": results,
        "first_metadata_ms": round((first_result_seconds or 0) * 1000, 2),
        "total_metadata_ms": round(total_seconds * 1000, 2),
        "max_workers": 4,
        "max_unfinished": 8,
    }


def _run_cancellation(root: Path) -> dict[str, float | int]:
    paths = sorted(path for path in root.iterdir() if path.is_file())

    def slow_source(scan_root: Path):
        for path in paths:
            time.sleep(0.0005)
            yield path

    first_batch = Event()
    finished = Event()
    discovered = 0
    final_state = None

    def on_event(event: object) -> None:
        nonlocal discovered, final_state
        if isinstance(event, ImportBatch):
            discovered += len(event.items)
            first_batch.set()
        elif isinstance(event, JobFinished):
            final_state = event.state
            finished.set()

    coordinator = ImportCoordinator(ImportService(path_source=slow_source))
    handle = coordinator.start(uuid4(), root, on_event, batch_size=20)
    assert first_batch.wait(3)
    cancel_start = time.perf_counter()
    handle.cancel()
    assert finished.wait(3)
    cancel_ms = (time.perf_counter() - cancel_start) * 1000
    assert final_state is JobState.CANCELED
    assert discovered < len(paths)
    return {
        "cancel_latency_ms": round(cancel_ms, 2),
        "updates_before_cancel": discovered,
    }


def _run_paired_export(root: Path) -> dict[str, float | int]:
    destination = root / "acceptance-export"
    units = []
    for index in range(20):
        jpeg = root / f"paired_{index:04d}.JPG"
        raw = root / f"paired_{index:04d}.NEF"
        units.append(
            ExportUnit(
                f"paired-{index}",
                (
                    ExportItem(jpeg, jpeg.name),
                    ExportItem(raw, raw.name),
                ),
            )
        )
    request = ExportRequest(
        destination=destination,
        items=(),
        units=tuple(units),
        mode=ExportMode.COPY,
    )
    started = time.perf_counter()
    report = ExportService().export(request, lambda: False)
    total_ms = (time.perf_counter() - started) * 1000
    assert len(report.unit_results) == 20
    assert all(result.success for result in report.unit_results)
    assert len(list(destination.iterdir())) == 40
    assert all(item.source.exists() for unit in units for item in unit.items)
    return {
        "paired_units": len(report.unit_results),
        "exported_files": len(report.results),
        "copy_ms": round(total_ms, 2),
    }


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="xuanpian-acceptance-") as folder:
        root = Path(folder)
        started = time.perf_counter()
        paths = _make_fixture(root)
        fixture_ms = (time.perf_counter() - started) * 1000
        assert len(paths) == 1400
        report = {
            "fixture_create_ms": round(fixture_ms, 2),
            "import": _run_import(root),
            "metadata": _run_metadata(root),
            "cancellation": _run_cancellation(root),
            "paired_export": _run_paired_export(root),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
