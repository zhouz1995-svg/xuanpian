from __future__ import annotations

import json
from copy import deepcopy
from importlib import import_module
from pathlib import Path

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractItemView, QMessageBox, QWidget

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.photo_list_model import PhotoRoles
from src.xuanpian.ui.main_window import MainWindow
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser
from src.xuanpian.ui.thumbnail_card import ThumbnailCard


PHOTO_COUNT = 1_000


def _models() -> list[ImageFileModel]:
    return [
        ImageFileModel(
            f"/deterministic/photo-{row:04d}.jpg",
            photo_id=f"photo-{row:04d}",
            mark="入选" if row % 2 == 0 else "",
        )
        for row in range(PHOTO_COUNT)
    ]


def _child_widget_count(browser: ThumbnailBrowser) -> int:
    return len(browser.findChildren(QWidget))


def _valid_acceptance_report() -> dict[str, object]:
    return {
        "model_rows": PHOTO_COUNT,
        "burst_group_count": 333,
        "burst_sidebar_rows": 333,
        "burst_filtered_ids": ["photo-0000", "photo-0001", "photo-0002"],
        "compare_strip_rows": 3,
        "photo_widgets": 0,
        "grid_child_widgets_warm": 24,
        "grid_child_widgets_after": 24,
        "preview_child_widgets_warm": 5,
        "preview_child_widgets_after": 5,
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
        "data_changed": [
            [777, int(PhotoRoles.THUMBNAIL)],
            [777, int(PhotoRoles.ANALYSIS)],
            [777, int(PhotoRoles.MARK)],
        ],
        "timings_ms": {
            "load": 0.0,
            "filter": 1.5,
            "updates": 2.0,
            "zoom": 3.0,
        },
    }


def test_main_window_toggle_click_shows_only_compact_list(qapp) -> None:
    window = MainWindow()
    window.thumbnail_browser.set_models(_models()[:3])
    window.resize(900, 650)
    window.show()
    qapp.processEvents()

    QTest.mouseClick(
        window._view_toggle._buttons["list"],
        Qt.MouseButton.LeftButton,
    )
    qapp.processEvents()

    browser = window.thumbnail_browser
    assert window.view_mode() == "list"
    assert browser._list_view.isVisible()
    assert not browser._view.isVisible()
    assert browser._list_view.sizeHintForRow(0) == 56
    window.close()


def test_offscreen_1000_ui_acceptance_reports_virtualized_stable_state(qapp) -> None:
    script = Path("scripts/acceptance_ui_1000.py")
    assert script.is_file()
    run_acceptance = import_module("scripts.acceptance_ui_1000").run_acceptance
    report = run_acceptance(qapp)

    assert report["model_rows"] == PHOTO_COUNT
    assert report["photo_widgets"] == 0
    assert report["grid_child_widgets_after"] <= report["grid_child_widgets_warm"]
    assert (
        report["preview_child_widgets_after"]
        <= report["preview_child_widgets_warm"]
    )
    assert report["selected_photo_id"] == "photo-0500"
    assert report["preview_open_photo_id"] == "photo-0502"
    assert report["navigation_photo_ids"] == ["photo-0503", "photo-0502"]
    assert report["filmstrip_selected_photo_id"] == "photo-0504"
    assert report["legacy_mark_attempt_photo_ids"] == ["photo-0504", "photo-0505"]
    assert report["legacy_mark_controls_disabled"] is True
    assert report["deleted_photo_id"] == "photo-0504"
    assert report["preview_after_delete_id"] == "photo-0505"
    assert report["preview_rows_before_delete"] == PHOTO_COUNT
    assert report["preview_rows_after_delete"] == PHOTO_COUNT - 1
    assert report["preview_model_resets_during_navigation"] == 0
    assert report["database_deleted"] is True
    assert report["database_marks"] == ["入选", ""]
    assert report["preview_image_nonempty"] is True
    assert report["preview_loaded_photo_id"] == "photo-0502"
    assert report["preview_zoom_changed"] is True
    assert report["preview_rotation"] == 90
    assert report["preview_zoom_reset"] is True
    assert report["preview_info_filename"] == "photo-0502.jpg"
    assert report["preview_info_dimensions"] == "96 × 64"
    assert [change[0] for change in report["data_changed"]] == [777, 777, 777]
    assert set(report["timings_ms"]) == {"load", "filter", "updates", "zoom"}


def test_offscreen_acceptance_main_exits_nonzero_for_invalid_report(
    monkeypatch, capsys
) -> None:
    acceptance = import_module("scripts.acceptance_ui_1000")
    monkeypatch.setattr(
        acceptance,
        "run_acceptance",
        lambda: {"model_rows": PHOTO_COUNT - 1},
    )

    assert acceptance.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "model_rows" in captured.err
    assert "child widgets" in captured.err


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("wrong attempted IDs", lambda report: report.__setitem__("legacy_mark_attempt_photo_ids", ["wrong"])),
        ("missing attempted IDs", lambda report: report.pop("legacy_mark_attempt_photo_ids")),
        ("wrong deleted ID", lambda report: report.__setitem__("deleted_photo_id", "wrong")),
        ("missing deleted ID", lambda report: report.pop("deleted_photo_id")),
        ("missing timings", lambda report: report.pop("timings_ms")),
        (
            "non-mapping timings",
            lambda report: report.__setitem__("timings_ms", []),
        ),
        (
            "missing timing key",
            lambda report: report["timings_ms"].pop("zoom"),  # type: ignore[index,union-attr]
        ),
        (
            "unexpected timing key",
            lambda report: report["timings_ms"].__setitem__("extra", 1.0),  # type: ignore[index,union-attr]
        ),
        (
            "string timing",
            lambda report: report["timings_ms"].__setitem__("load", "fast"),  # type: ignore[index,union-attr]
        ),
        (
            "boolean timing",
            lambda report: report["timings_ms"].__setitem__("load", True),  # type: ignore[index,union-attr]
        ),
        (
            "negative timing",
            lambda report: report["timings_ms"].__setitem__("load", -0.1),  # type: ignore[index,union-attr]
        ),
        (
            "NaN timing",
            lambda report: report["timings_ms"].__setitem__("load", float("nan")),  # type: ignore[index,union-attr]
        ),
        (
            "infinite timing",
            lambda report: report["timings_ms"].__setitem__("load", float("inf")),  # type: ignore[index,union-attr]
        ),
    ],
)
def test_validate_report_rejects_bad_stable_ids_and_timings(
    case, mutate, monkeypatch, capsys
) -> None:
    acceptance = import_module("scripts.acceptance_ui_1000")
    report = _valid_acceptance_report()
    mutate(report)

    assert acceptance.validate_report(report), case
    monkeypatch.setattr(acceptance, "run_acceptance", lambda: report)
    assert acceptance.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err


def test_validate_report_allows_complete_finite_timing_report(monkeypatch, capsys) -> None:
    acceptance = import_module("scripts.acceptance_ui_1000")
    report = _valid_acceptance_report()

    assert acceptance.validate_report(report) == []
    monkeypatch.setattr(acceptance, "run_acceptance", lambda: deepcopy(report))
    assert acceptance.main() == 0
    assert json.loads(capsys.readouterr().out) == report


def test_1000_photo_updates_and_repeated_filters_do_not_create_photo_widgets(
    qapp,
) -> None:
    browser = ThumbnailBrowser()
    models = _models()
    browser.set_models(models)
    qapp.processEvents()
    warm_child_widgets = _child_widget_count(browser)
    changes: list[tuple[int, list[int]]] = []
    browser._photo_list_model.dataChanged.connect(
        lambda first, last, roles: changes.append(
            (first.row(), list(roles)) if first == last else (-1, list(roles))
        )
    )

    for _ in range(4):
        browser.toggle_picks_only()
        browser.toggle_picks_only()
        browser.set_thumbnail_size(200)
        browser.set_thumbnail_size(180)
        browser.update_model_thumbnail(
            models[777],
            QImage(24, 16, QImage.Format.Format_RGB32),
        )
        models[777].technical_status = AnalysisStatus.SUCCEEDED
        browser._photo_list_model.update_photo("photo-0777", [PhotoRoles.ANALYSIS])
        models[777].mark = "入选"
        browser._photo_list_model.update_photo("photo-0777", [PhotoRoles.MARK])
        qapp.processEvents()

    assert browser._photo_list_model.rowCount() == PHOTO_COUNT
    assert browser.findChildren(ThumbnailCard) == []
    assert _child_widget_count(browser) <= warm_child_widgets
    assert changes == [
        (777, [int(PhotoRoles.THUMBNAIL)]),
        (777, [int(PhotoRoles.ANALYSIS)]),
        (777, [int(PhotoRoles.MARK)]),
    ] * 4


def test_selection_double_click_and_keyboard_navigation_keep_photo_ids_after_reset(
    qapp, monkeypatch
) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(_models())
    browser.resize(900, 600)
    browser.show()
    qapp.processEvents()
    previews: list[str] = []
    selections: list[list[str]] = []

    class MainWindow:
        def enter_preview_photo(self, photo_id: str) -> None:
            previews.append(photo_id)

    monkeypatch.setattr(browser, "_find_main_window", lambda: MainWindow())
    browser.selection_changed.connect(
        lambda: selections.append(browser.selected_photo_ids())
    )
    view = browser._view
    selected_index = browser._photo_list_model.index(
        browser._photo_list_model.row_for_id("photo-0500"),
        0,
    )
    view.scrollTo(
        selected_index,
        QAbstractItemView.ScrollHint.PositionAtCenter,
    )
    qapp.processEvents()
    QTest.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        view.visualRect(selected_index).center(),
    )
    browser.toggle_picks_only()
    preview_index = browser._photo_list_model.index(
        browser._photo_list_model.row_for_id("photo-0502"),
        0,
    )
    view.scrollTo(
        preview_index,
        QAbstractItemView.ScrollHint.PositionAtCenter,
    )
    qapp.processEvents()
    QTest.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        view.visualRect(preview_index).center(),
    )
    QTest.mouseDClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        view.visualRect(preview_index).center(),
    )
    navigation_start_index = browser._photo_list_model.index(
        browser._photo_list_model.row_for_id("photo-0500"),
        0,
    )
    view.scrollTo(
        navigation_start_index,
        QAbstractItemView.ScrollHint.PositionAtCenter,
    )
    qapp.processEvents()
    QTest.mouseClick(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        view.visualRect(navigation_start_index).center(),
    )
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Right)
    qapp.processEvents()

    assert browser.selected_photo_ids() == ["photo-0502"]
    assert previews == ["photo-0502"]
    assert ["photo-0500"] in selections
    assert ["photo-0502"] in selections


def test_mark_and_delete_current_rows_keep_their_photo_ids_after_filter_reset(
    qapp, monkeypatch
) -> None:
    window = MainWindow()
    models = _models()
    browser = window.thumbnail_browser
    browser.set_models(models)
    browser.toggle_picks_only()

    window.mark_current(
        window.current_row_for_photo_id("photo-0500"),
        "待定",
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    window.delete_current(window.current_row_for_photo_id("photo-0502"))
    browser.toggle_picks_only()

    assert models[500].mark == "待定"
    assert browser.model_for_photo_id("photo-0502") is None
    assert browser.current_photo_ids() == [
        f"photo-{row:04d}"
        for row in range(PHOTO_COUNT)
        if row != 502
    ]
    assert browser.model_for_photo_id("photo-0500") is models[500]
    window.close()
