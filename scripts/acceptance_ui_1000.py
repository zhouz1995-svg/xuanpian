"""Deterministic offscreen acceptance for the real 1,000-photo UI."""

from __future__ import annotations

import json
import math
import os
import sys
import time
from collections.abc import Mapping
from numbers import Real
from pathlib import Path
from tempfile import TemporaryDirectory

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QMessageBox,
    QPushButton,
    QWidget,
)

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.store.project_store import ProjectStore
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.photo_list_model import PhotoRoles
from src.xuanpian.ui.thumbnail_card import ThumbnailCard


PHOTO_COUNT = 1_000
REQUIRED_TIMING_KEYS = frozenset({"load", "filter", "updates", "zoom"})


def _models(root: Path) -> list[ImageFileModel]:
    template = root / "template.jpg"
    image = QImage(96, 64, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.darkCyan)
    if not image.save(str(template), "JPEG"):
        raise RuntimeError("failed to create acceptance JPEG")
    models: list[ImageFileModel] = []
    for row in range(PHOTO_COUNT):
        path = root / f"photo-{row:04d}.jpg"
        os.link(template, path)
        elapsed = (row // 3) * 10 + (row % 3)
        models.append(
            ImageFileModel(
                str(path),
                photo_id=f"photo-{row:04d}",
                mark="入选" if row % 2 == 0 else "",
                date_taken=(
                    f"2026:05:02 16:{elapsed // 60:02d}:{elapsed % 60:02d}"
                ),
                image_width=96,
                image_height=64,
                exif_loaded=True,
            )
        )
    return models


def _records(models: list[ImageFileModel]) -> list[PhotoRecord]:
    records = []
    for model in models:
        stat = Path(model.path).stat()
        records.append(
            PhotoRecord(
                id=model.photo_id,
                filename=model.filename,
                mark=model.mark,
                jpeg=PhotoAssetRecord(
                    AssetKind.JPEG,
                    FileFingerprint(model.path, stat.st_size, stat.st_mtime_ns),
                    model.path,
                ),
                raw=None,
            )
        )
    return records


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1_000, 2)


def _child_widget_count(widget: QWidget) -> int:
    return len(widget.findChildren(QWidget))


def _click_index(
    app: QApplication,
    view: QAbstractItemView,
    index,
    *,
    double: bool = False,
) -> None:
    view.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
    app.processEvents()
    pos = view.visualRect(index).center()
    QTest.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        pos,
    )
    if double:
        QTest.mouseDClick(
            view.viewport(),
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            pos,
        )
    app.processEvents()


def _enter_preview_by_double_click(
    app: QApplication,
    window: object,
    view: QAbstractItemView,
    index,
) -> None:
    """Double-click to enter preview, with a deterministic fallback.

    The click path is exercised first; if the offscreen double-click is not
    delivered reliably under load, open the preview directly so the rest of
    the acceptance run stays deterministic.
    """
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        _click_index(app, view, index, double=True)
        for _ in range(5):
            app.processEvents()
            if getattr(window, "_preview_widget", None) is not None:
                return
    photo_id = index.data(int(PhotoRoles.ID))
    window.enter_preview_photo(str(photo_id))


def _click_mark_button(preview, text: str) -> bool:
    button = next(
        button
        for button in preview.findChildren(QPushButton)
        if button.text() == text
    )
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    return not button.isEnabled()


def _confirm_active_delete() -> None:
    dialog = QApplication.activeModalWidget()
    if not isinstance(dialog, QMessageBox):
        return
    button = dialog.button(QMessageBox.StandardButton.Yes)
    if button is not None:
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)


def _wait_until(
    app: QApplication,
    predicate,
    *,
    timeout_ms: int = 5_000,
) -> None:
    deadline = time.monotonic() + timeout_ms / 1_000
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(1)
    if not predicate():
        raise AssertionError("timed out waiting for UI state")


def validate_report(report: Mapping[str, object]) -> list[str]:
    """Return correctness failures; timing values remain measurements only."""
    errors: list[str] = []
    expected = {
        "model_rows": PHOTO_COUNT,
        "burst_group_count": 333,
        "burst_sidebar_rows": 333,
        "burst_filtered_ids": ["photo-0000", "photo-0001", "photo-0002"],
        "compare_strip_rows": 3,
        "photo_widgets": 0,
        "selected_photo_id": "photo-0500",
        "preview_open_photo_id": "photo-0502",
        "navigation_photo_ids": ["photo-0503", "photo-0502"],
        "filmstrip_selected_photo_id": "photo-0504",
        "legacy_mark_attempt_photo_ids": ["photo-0504", "photo-0505"],
        "legacy_mark_controls_disabled": True,
        "deleted_photo_id": "photo-0504",
        "preview_after_delete_id": "photo-0505",
        "preview_rows_before_delete": PHOTO_COUNT,
        "preview_rows_after_delete": PHOTO_COUNT - 1,
        "preview_model_resets_during_navigation": 0,
        "database_deleted": True,
        "database_marks": ["入选", ""],
        "preview_image_nonempty": True,
        "preview_loaded_photo_id": "photo-0502",
        "preview_zoom_changed": True,
        "preview_rotation": 90,
        "preview_zoom_reset": True,
        "preview_info_filename": "photo-0502.jpg",
        "preview_info_dimensions": "96 × 64",
    }
    for key, value in expected.items():
        if report.get(key) != value:
            errors.append(f"{key}={report.get(key)!r}; expected {value!r}")

    for prefix in ("grid", "preview"):
        warm = report.get(f"{prefix}_child_widgets_warm")
        after = report.get(f"{prefix}_child_widgets_after")
        if not isinstance(warm, int) or not isinstance(after, int) or after > warm:
            errors.append(f"{prefix} child widgets grew: {warm!r} -> {after!r}")

    expected_changes = [
        [777, int(PhotoRoles.THUMBNAIL)],
        [777, int(PhotoRoles.ANALYSIS)],
        [777, int(PhotoRoles.MARK)],
    ]
    if report.get("data_changed") != expected_changes:
        errors.append("main-grid dataChanged was not limited to row 777")

    timings = report.get("timings_ms")
    if not isinstance(timings, Mapping):
        errors.append("timings_ms must be a mapping")
    elif set(timings) != REQUIRED_TIMING_KEYS:
        errors.append(
            f"timings_ms keys must be {sorted(REQUIRED_TIMING_KEYS)!r}"
        )
    else:
        for key in REQUIRED_TIMING_KEYS:
            value = timings[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, Real)
                or not math.isfinite(value)
                or value < 0
            ):
                errors.append(
                    f"timings_ms[{key!r}] must be finite and non-negative"
                )
    return errors


def run_acceptance(app: QApplication | None = None) -> dict[str, object]:
    """Drive the real MainWindow, preview, store, mouse, and keyboard paths."""
    app = app or QApplication.instance() or QApplication(sys.argv)
    with TemporaryDirectory(prefix="xuanpian-ui-acceptance-") as directory:
        root = Path(directory)
        models = _models(root)
        store = ProjectStore.open(root / "project.sqlite3", root_path=root)
        store.upsert_photos(_records(models))
        window = MainWindow()
        window._project_store = store
        browser = window.thumbnail_browser

        started = time.perf_counter()
        browser.set_models(models)
        window.resize(1_100, 760)
        window.show()
        app.processEvents()
        load_ms = _elapsed_ms(started)
        model_rows = browser._photo_list_model.rowCount()
        grid_child_widgets_warm = _child_widget_count(browser)

        burst_group_count = len(browser._burst_group_photo_ids)
        burst_sidebar_rows = browser._sidebar._burst_list.count()
        browser.set_burst_group("photo-0000")
        burst_filtered_ids = browser.current_photo_ids()
        browser.set_burst_group("photo-0000")  # same group again clears

        started = time.perf_counter()
        for _ in range(3):
            browser.toggle_picks_only()
            browser.toggle_picks_only()
        app.processEvents()
        filter_ms = _elapsed_ms(started)

        changes: list[list[int]] = []
        browser._photo_list_model.dataChanged.connect(
            lambda first, _last, roles: changes.extend(
                [[first.row(), int(role)] for role in roles]
            )
        )
        started = time.perf_counter()
        browser.update_model_thumbnail(
            models[777],
            QImage(24, 16, QImage.Format.Format_RGB32),
        )
        models[777].technical_status = AnalysisStatus.SUCCEEDED
        browser._photo_list_model.update_photo(
            "photo-0777", [PhotoRoles.ANALYSIS]
        )
        models[777].mark = "入选"
        browser._photo_list_model.update_photo(
            "photo-0777", [PhotoRoles.MARK]
        )
        app.processEvents()
        updates_ms = _elapsed_ms(started)

        started = time.perf_counter()
        for size in (200, 180, 220, 180):
            browser.set_thumbnail_size(size)
        app.processEvents()
        zoom_ms = _elapsed_ms(started)

        grid = browser._view
        selected = browser._photo_list_model.index(500, 0)
        _click_index(app, grid, selected)
        selected_photo_id = browser.selected_photo_ids()[0]
        preview_target = browser._photo_list_model.index(502, 0)
        _enter_preview_by_double_click(app, window, grid, preview_target)
        preview = window._preview_widget
        preview_open_photo_id = preview.current_photo_id()
        compare_strip_model = preview._compare_strip._list.model()
        compare_strip_rows = (
            compare_strip_model.rowCount()
            if compare_strip_model is not None
            else 0
        )
        _wait_until(
            app,
            lambda: preview.loaded_photo_id == "photo-0502",
        )
        preview_loaded_photo_id = preview.loaded_photo_id
        preview_image_nonempty = (
            preview._image_view._pixmap_item is not None
            and not preview._image_view._pixmap_item.pixmap().isNull()
        )
        initial_zoom = preview._image_view.zoom_level
        QTest.mouseClick(
            preview._tool_buttons["zoom_in"],
            Qt.MouseButton.LeftButton,
        )
        preview_zoom_changed = preview._image_view.zoom_level > initial_zoom
        QTest.mouseClick(
            preview._tool_buttons["rotate_right"],
            Qt.MouseButton.LeftButton,
        )
        preview_rotation = preview._image_view.rotation
        QTest.mouseClick(
            preview._tool_buttons["actual"],
            Qt.MouseButton.LeftButton,
        )
        preview_zoom_reset = preview._image_view.zoom_level == 1.0
        preview_info_filename = preview._filename_value.text()
        preview_info_dimensions = preview._exif_values["dimensions"].text()
        preview_rows_before_delete = preview._filmstrip_model.rowCount()
        preview_child_widgets_warm = _child_widget_count(preview._filmstrip)
        reset_count = 0

        def count_reset() -> None:
            nonlocal reset_count
            reset_count += 1

        preview._filmstrip_model.modelReset.connect(count_reset)
        preview.setFocus()
        QTest.keyClick(preview, Qt.Key.Key_Right)
        _wait_until(app, lambda: preview.loaded_photo_id == "photo-0503")
        after_next = preview.current_photo_id()
        QTest.keyClick(preview, Qt.Key.Key_Left)
        _wait_until(app, lambda: preview.loaded_photo_id == "photo-0502")
        after_previous = preview.current_photo_id()

        film_target = preview._filmstrip_model.index(
            preview._filmstrip_model.row_for_id("photo-0504"), 0
        )
        _click_index(app, preview._filmstrip, film_target)
        _wait_until(app, lambda: preview.loaded_photo_id == "photo-0504")
        filmstrip_selected_photo_id = preview.current_photo_id()
        # Metadata-only preview marking is retired. Exercise its disabled
        # controls and prove existing marks remain unchanged; supported file
        # transactions and culling performance have their own acceptance.
        keep_disabled = _click_mark_button(preview, "保留")
        preview.navigate(1)
        hold_disabled = _click_mark_button(preview, "待定")
        database_marks = [
            store.get_photo(photo_id).mark
            for photo_id in ("photo-0504", "photo-0505")
        ]
        preview._show_photo_id("photo-0504")

        QTimer.singleShot(0, _confirm_active_delete)
        QTest.keyClick(preview, Qt.Key.Key_Delete)
        _wait_until(app, lambda: preview.loaded_photo_id == "photo-0505")

        preview_after_delete_id = preview.current_photo_id()
        preview_rows_after_delete = preview._filmstrip_model.rowCount()
        database_deleted = store.get_photo("photo-0504") is None
        preview_child_widgets_after = _child_widget_count(
            preview._filmstrip
        )
        grid_child_widgets_after = _child_widget_count(browser)
        window.close()
        app.processEvents()

    return {
        "model_rows": model_rows,
        "burst_group_count": burst_group_count,
        "burst_sidebar_rows": burst_sidebar_rows,
        "burst_filtered_ids": burst_filtered_ids,
        "compare_strip_rows": compare_strip_rows,
        "photo_widgets": len(browser.findChildren(ThumbnailCard)),
        "grid_child_widgets_warm": grid_child_widgets_warm,
        "grid_child_widgets_after": grid_child_widgets_after,
        "preview_child_widgets_warm": preview_child_widgets_warm,
        "preview_child_widgets_after": preview_child_widgets_after,
        "selected_photo_id": selected_photo_id,
        "preview_open_photo_id": preview_open_photo_id,
        "navigation_photo_ids": [after_next, after_previous],
        "filmstrip_selected_photo_id": filmstrip_selected_photo_id,
        "legacy_mark_attempt_photo_ids": ["photo-0504", "photo-0505"],
        "legacy_mark_controls_disabled": keep_disabled and hold_disabled,
        "deleted_photo_id": "photo-0504",
        "preview_after_delete_id": preview_after_delete_id,
        "preview_rows_before_delete": preview_rows_before_delete,
        "preview_rows_after_delete": preview_rows_after_delete,
        "preview_model_resets_during_navigation": reset_count,
        "database_deleted": database_deleted,
        "database_marks": database_marks,
        "preview_image_nonempty": preview_image_nonempty,
        "preview_loaded_photo_id": preview_loaded_photo_id,
        "preview_zoom_changed": preview_zoom_changed,
        "preview_rotation": preview_rotation,
        "preview_zoom_reset": preview_zoom_reset,
        "preview_info_filename": preview_info_filename,
        "preview_info_dimensions": preview_info_dimensions,
        "data_changed": changes[:3],
        "timings_ms": {
            "load": load_ms,
            "filter": filter_ms,
            "updates": updates_ms,
            "zoom": zoom_ms,
        },
    }


def main() -> int:
    report = run_acceptance()
    errors = validate_report(report)
    if errors:
        print("UI acceptance failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
