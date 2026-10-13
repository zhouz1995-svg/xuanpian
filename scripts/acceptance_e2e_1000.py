"""Real offscreen import/persistence/cache/keyboard acceptance at 1,000 photos."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from PyQt6.QtCore import QItemSelectionModel, QTimer, Qt, qInstallMessageHandler
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from src.xuanpian.cache.thumbnail_cache import ThumbnailCache
from src.xuanpian.services.import_service import ImportService
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.tasks.import_coordinator import ImportBatch, ImportCoordinator
from src.xuanpian.tasks.io_coordinator import (
    IoCoordinator,
    MetadataResult,
)
from src.xuanpian.ui.keyboard_manager import KeyboardManager
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.preview_widget import preview_decode_reaper_count
from src.xuanpian.ui.thumbnail_card import ThumbnailCard
from src.xuanpian.ui.thumbnail_loader import load_thumbnail


JPEG_COUNT = 1_000
RAW_COUNT = 400
FILE_COUNT = JPEG_COUNT + RAW_COUNT
CANDIDATE_UPDATES = JPEG_COUNT + RAW_COUNT + RAW_COUNT
MEMORY_LIMIT = 8 * 1024 * 1024
DISK_LIMIT = 64 * 1024 * 1024
DISK_ENTRIES = 1_100


class _DecoderCounter:
    def __init__(self) -> None:
        self.count = 0
        self._lock = threading.Lock()

    def __call__(self, path: str, size: tuple[int, int]):
        with self._lock:
            self.count += 1
        return load_thumbnail(path, size)


def _wait_until(
    app: QApplication,
    predicate,
    *,
    timeout_ms: int = 60_000,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(1)
    if not predicate():
        raise AssertionError("timed out waiting for end-to-end state")


def _make_fixture(root: Path) -> None:
    root.mkdir(parents=True)
    template = root / ".template.jpg"
    image = QImage(96, 64, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.darkCyan)
    if not image.save(str(template), "JPEG"):
        raise RuntimeError("failed to create JPEG fixture")
    for index in range(JPEG_COUNT):
        os.link(template, root / f"photo-{index:04d}.JPG")
    template.unlink()
    for index in range(RAW_COUNT):
        (root / f"photo-{index:04d}.NEF").touch()


def _gated_service(gate: threading.Event) -> ImportService:
    def paths(root: Path):
        if not gate.wait(5):
            raise TimeoutError("import start gate was not released")
        yield from ImportService.iter_paths(root)

    return ImportService(path_source=paths)


def _cache(root: Path) -> ThumbnailCache:
    return ThumbnailCache(
        root,
        MEMORY_LIMIT,
        disk_limit_bytes=DISK_LIMIT,
        disk_limit_entries=DISK_ENTRIES,
    )


def _window(
    database: Path,
    photos: Path,
    cache: ThumbnailCache,
    decoder: _DecoderCounter,
    transactions: list[str],
    gate: threading.Event,
) -> MainWindow:
    coordinator = IoCoordinator(
        max_workers=4,
        thumbnail_cache=cache,
        thumbnail_decoder=decoder,
    )

    def open_store(path: Path, root: Path) -> ProjectStore:
        store = ProjectStore.open(path, root_path=root)
        store._connection.set_trace_callback(  # type: ignore[attr-defined]
            lambda statement: transactions.append(statement)
        )
        return store

    return MainWindow(
        import_coordinator=ImportCoordinator(_gated_service(gate)),
        io_coordinator=coordinator,
        thumbnail_cache=cache,
        project_path_resolver=lambda _root: database,
        project_store_factory=open_store,
        shutdown_timeout=5,
    )


def _start_and_wait(
    app: QApplication,
    window: MainWindow,
    photos: Path,
    gate: threading.Event,
    *,
    expected_rows: int = JPEG_COUNT,
) -> None:
    window._start_import(str(photos))
    gate.set()
    _wait_until(
        app,
        lambda: (
            window._active_job_ids["import"] is None
            and not window._io_handles
            and len(window.all_models) == expected_rows
        ),
    )


def _press(app: QApplication, target: QWidget, key: Qt.Key) -> None:
    target.setFocus()
    QTest.keyClick(target, key)
    app.processEvents()


def _confirm_delete() -> None:
    dialog = QApplication.activeModalWidget()
    if isinstance(dialog, QMessageBox):
        button = dialog.button(QMessageBox.StandardButton.Yes)
        if button is not None:
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def _pair_ids(window: MainWindow) -> dict[str, str]:
    return {
        Path(model.jpeg_path).stem: model.photo_id
        for model in window.all_models
        if model.jpeg_path and model.raw_path
    }


def _schema_version(database: Path) -> int:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
    return int(row[0])


def _drive_keys(
    app: QApplication,
    window: MainWindow,
    messages: list[str],
) -> tuple[dict[str, object], str, str]:
    browser = window.thumbnail_browser
    ids = browser.current_photo_ids()
    grid = browser._view
    warm_widgets = len(browser.findChildren(QWidget))
    browser.select_photo_id(ids[10])

    _press(app, grid, Qt.Key.Key_Right)
    grid_next = browser.selected_photo_ids() == [ids[11]]
    _press(app, grid, Qt.Key.Key_Left)
    grid_back = browser.selected_photo_ids() == [ids[10]]
    _press(app, grid, Qt.Key.Key_Space)
    preview = window._preview_widget
    preview_start = preview.current_photo_id()
    reset_count = 0

    def count_reset() -> None:
        nonlocal reset_count
        reset_count += 1

    preview._filmstrip_model.modelReset.connect(count_reset)
    _press(app, preview, Qt.Key.Key_Right)
    preview_next = preview.current_photo_id() == ids[11]
    _press(app, preview, Qt.Key.Key_Left)
    preview_back = preview.current_photo_id() == ids[10]
    _wait_until(app, lambda: preview.loaded_photo_id == ids[10])
    initial_zoom = preview._image_view.zoom_level
    QTest.mouseClick(
        preview._tool_buttons["zoom_in"],
        Qt.MouseButton.LeftButton,
    )
    zoom_changed = preview._image_view.zoom_level > initial_zoom
    QTest.mouseClick(
        preview._tool_buttons["rotate_right"],
        Qt.MouseButton.LeftButton,
    )
    rotation = preview._image_view.rotation
    info_filename = preview._filename_value.text()
    _press(app, preview, Qt.Key.Key_Escape)
    escaped = window._stack.currentWidget() is browser

    stale_grid_id = ids[0]
    preview_mark_id = ids[2]
    browser.select_photo_id(stale_grid_id)
    window.enter_preview_photo(preview_mark_id)
    preview = window._preview_widget
    preview_marks_before = {
        photo_id: window.model_for_photo_id(photo_id).mark
        for photo_id in (preview_mark_id, stale_grid_id)
    }
    for key in (Qt.Key.Key_1, Qt.Key.Key_2, Qt.Key.Key_3):
        _press(app, preview, key)
    preview_marks_unchanged = all(
        window.model_for_photo_id(photo_id).mark == mark
        for photo_id, mark in preview_marks_before.items()
    )
    _press(app, preview, Qt.Key.Key_Escape)

    multi_ids = (ids[5], ids[6])
    selection = grid.selectionModel()
    selection.clearSelection()
    for photo_id in multi_ids:
        row = browser._photo_list_model.row_for_id(photo_id)
        selection.select(
            browser._photo_list_model.index(row, 0),
            QItemSelectionModel.SelectionFlag.Select,
        )
    grid_marks_before = {
        photo_id: window.model_for_photo_id(photo_id).mark
        for photo_id in multi_ids
    }
    grid_shortcut_results: dict[int, bool] = {}
    for shortcut, key in (
        (1, Qt.Key.Key_1),
        (2, Qt.Key.Key_2),
        (3, Qt.Key.Key_3),
    ):
        _press(app, grid, key)
        grid_shortcut_results[shortcut] = all(
            window.model_for_photo_id(photo_id).mark == mark
            for photo_id, mark in grid_marks_before.items()
        )

    delete_id = next(
        model.photo_id
        for model in window.all_models
        if not model.raw_path and model.photo_id not in multi_ids
    )
    window.enter_preview_photo(delete_id)
    QTimer.singleShot(0, _confirm_delete)
    _press(app, window._preview_widget, Qt.Key.Key_Delete)
    _wait_until(app, lambda: window.model_for_photo_id(delete_id) is None)

    return (
        {
            "grid_navigation": grid_next and grid_back,
            "preview_navigation": (
                preview_start == ids[10] and preview_next and preview_back
            ),
            "preview_marks_unchanged": preview_marks_unchanged,
            "grid_key_1_marks_unchanged": grid_shortcut_results[1],
            "grid_key_2_marks_unchanged": grid_shortcut_results[2],
            "grid_key_3_marks_unchanged": grid_shortcut_results[3],
            "delete": window.model_for_photo_id(delete_id) is None,
            "escape": escaped,
            "ambiguous_warnings": sum(
                "ambiguous" in message.lower() for message in messages
            ),
            "widgets_bounded": (
                len(browser.findChildren(ThumbnailCard)) == 0
                and len(browser.findChildren(QWidget)) <= warm_widgets
            ),
            "navigation_model_resets": reset_count,
            "zoom_changed": zoom_changed,
            "rotation": rotation,
            "info_filename": info_filename,
        },
        multi_ids[0],
        delete_id,
    )


def validate_report(report: Mapping[str, object]) -> list[str]:
    errors: list[str] = []
    required = {
        "files",
        "updates",
        "pairs",
        "transactions",
        "cache",
        "reopen",
        "keys",
    }
    for section in required - set(report):
        errors.append(f"missing section: {section}")
    if errors:
        return errors

    files = report["files"]
    updates = report["updates"]
    pairs = report["pairs"]
    transactions = report["transactions"]
    cache = report["cache"]
    reopen = report["reopen"]
    keys = report["keys"]
    if not all(
        isinstance(section, Mapping)
        for section in (
            files,
            updates,
            pairs,
            transactions,
            cache,
            reopen,
            keys,
        )
    ):
        return ["all report sections must be mappings"]

    expected = (
        (files, "jpeg", JPEG_COUNT),
        (files, "raw", RAW_COUNT),
        (files, "total", FILE_COUNT),
        (updates, "candidates", CANDIDATE_UPDATES),
        (updates, "batches", 36),
        (pairs, "initial", RAW_COUNT),
        (pairs, "reopened", RAW_COUNT),
        (transactions, "schema_version", 3),
        (reopen, "rows", JPEG_COUNT - 1),
        (reopen, "mark", "未处理"),
        (reopen, "pair_ids_preserved", True),
        (reopen, "tombstone_preserved", True),
        (cache, "decoder_first", JPEG_COUNT),
        (cache, "decoder_reopen", 0),
        (cache, "max_workers", 4),
        (cache, "max_unfinished", 8),
    )
    for section, key, value in expected:
        if section.get(key) != value:
            errors.append(
                f"{key}={section.get(key)!r}; expected {value!r}"
            )
    if not isinstance(updates.get("metadata_results"), int) or updates.get(
        "metadata_results", 0
    ) < JPEG_COUNT:
        errors.append("metadata_results must cover every JPEG")
    if not isinstance(transactions.get("count"), int) or transactions.get(
        "count", 0
    ) <= 0:
        errors.append("transactions.count must be positive")
    for current, limit, label in (
        ("memory_bytes", "memory_limit_bytes", "memory"),
        ("disk_bytes", "disk_limit_bytes", "disk"),
    ):
        if not (
            isinstance(cache.get(current), int)
            and isinstance(cache.get(limit), int)
            and 0 <= cache[current] <= cache[limit]
        ):
            errors.append(f"cache {label} limit exceeded")
    for key in (
        "grid_navigation",
        "preview_navigation",
        "preview_marks_unchanged",
        "grid_key_1_marks_unchanged",
        "grid_key_2_marks_unchanged",
        "grid_key_3_marks_unchanged",
        "delete",
        "escape",
    ):
        if keys.get(key) is not True:
            errors.append(f"keys.{key} must be true")
    if keys.get("ambiguous_warnings") != 0:
        errors.append("keys.ambiguous_warnings must be zero")
    if keys.get("widgets_bounded") is not True:
        errors.append("keys.widgets_bounded must be true")
    if keys.get("navigation_model_resets") != 0:
        errors.append("keys.navigation_model_resets must be zero")
    if keys.get("zoom_changed") is not True:
        errors.append("keys.zoom_changed must be true")
    if keys.get("rotation") != 90:
        errors.append("keys.rotation must be 90")
    if not str(keys.get("info_filename", "")).lower().endswith(".jpg"):
        errors.append("keys.info_filename must expose the current JPEG")
    return errors


def run_acceptance(
    app: QApplication | None = None,
) -> dict[str, object]:
    app = app or QApplication.instance() or QApplication(sys.argv)
    with TemporaryDirectory(prefix="xuanpian-e2e-1000-") as directory:
        base = Path(directory)
        photos = base / "photos"
        database = base / "project.sqlite3"
        cache_root = base / "cache"
        _make_fixture(photos)

        transactions: list[str] = []
        decoder_first = _DecoderCounter()
        cache_first = _cache(cache_root)
        gate = threading.Event()
        window = _window(
            database,
            photos,
            cache_first,
            decoder_first,
            transactions,
            gate,
        )
        batches = 0
        candidate_updates = 0
        metadata_results = 0

        def count_batch(event: object) -> None:
            nonlocal batches, candidate_updates
            if isinstance(event, ImportBatch):
                batches += 1
                candidate_updates += len(event.items)

        def count_metadata(event: object) -> None:
            nonlocal metadata_results
            if isinstance(event, MetadataResult):
                metadata_results += 1

        window._job_bridge.batch.connect(count_batch)
        window._job_bridge.metadata.connect(count_metadata)
        window.install_keyboard_manager(KeyboardManager())
        window.resize(1_100, 760)
        window.show()
        app.processEvents()
        _start_and_wait(app, window, photos, gate)
        initial_pairs = _pair_ids(window)
        if len(initial_pairs) != RAW_COUNT:
            raise AssertionError("scanner did not produce 400 JPEG/RAW pairs")

        messages: list[str] = []
        previous_handler = qInstallMessageHandler(
            lambda _kind, _context, message: messages.append(message)
        )
        try:
            keys, marked_id, deleted_id = _drive_keys(
                app,
                window,
                messages,
            )
        finally:
            qInstallMessageHandler(previous_handler)

        initial_updates = candidate_updates
        initial_batches = batches
        gate_rescan = gate
        _start_and_wait(
            app,
            window,
            photos,
            gate_rescan,
            expected_rows=JPEG_COUNT - 1,
        )
        if len(window.all_models) != JPEG_COUNT - 1:
            raise AssertionError("deleted photo returned after rescan")
        cache_first.drain()
        cache_entries = len(cache_first._disk_entries)
        memory_bytes = cache_first._memory_bytes
        disk_bytes = cache_first._disk_bytes
        first_decoder_count = decoder_first.count
        window.close()
        app.processEvents()
        if preview_decode_reaper_count() != 0:
            raise AssertionError("preview decoder thread leaked at close")

        decoder_reopen = _DecoderCounter()
        cache_reopen = _cache(cache_root)
        gate_reopen = threading.Event()
        reopened = _window(
            database,
            photos,
            cache_reopen,
            decoder_reopen,
            transactions,
            gate_reopen,
        )
        reopened.install_keyboard_manager(KeyboardManager())
        reopened.show()
        app.processEvents()
        _start_and_wait(
            app,
            reopened,
            photos,
            gate_reopen,
            expected_rows=JPEG_COUNT - 1,
        )
        reopened_pairs = _pair_ids(reopened)
        reopened_mark = reopened.model_for_photo_id(marked_id).mark
        tombstone_preserved = reopened.model_for_photo_id(deleted_id) is None
        reopened_rows = len(reopened.all_models)
        reopened.close()
        app.processEvents()

        report = {
            "files": {
                "jpeg": JPEG_COUNT,
                "raw": RAW_COUNT,
                "total": FILE_COUNT,
            },
            "updates": {
                "candidates": initial_updates,
                "batches": initial_batches,
                "metadata_results": metadata_results,
            },
            "pairs": {
                "initial": len(initial_pairs),
                "reopened": len(reopened_pairs),
            },
            "transactions": {
                "count": transactions.count("BEGIN IMMEDIATE"),
                "schema_version": _schema_version(database),
            },
            "cache": {
                "entries": cache_entries,
                "memory_bytes": memory_bytes,
                "memory_limit_bytes": MEMORY_LIMIT,
                "disk_bytes": disk_bytes,
                "disk_limit_bytes": DISK_LIMIT,
                "decoder_first": first_decoder_count,
                "decoder_reopen": decoder_reopen.count,
                "max_workers": 4,
                "max_unfinished": 8,
            },
            "reopen": {
                "rows": reopened_rows,
                "mark": reopened_mark,
                "pair_ids_preserved": initial_pairs == reopened_pairs,
                "tombstone_preserved": tombstone_preserved,
            },
            "keys": keys,
        }
        return report


def main() -> int:
    try:
        report = run_acceptance()
        errors = validate_report(report)
    except Exception as error:
        print(
            f"E2E acceptance failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    if errors:
        print("E2E acceptance failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
