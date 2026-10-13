from PyQt6.QtGui import QImage

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.keyboard_manager import KeyboardManager
from src.xuanpian.ui.photo_list_model import PhotoRoles
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser


def test_browser_keeps_o1_model_provider_lookup(qapp) -> None:
    models = [
        ImageFileModel(f"/fake/{index}.jpg", photo_id=f"photo-{index}")
        for index in range(3)
    ]
    browser = ThumbnailBrowser()

    browser.set_models(models)

    assert set(browser._model_by_photo_id) == {
        model.photo_id for model in models
    }
    assert browser._model_by_photo_id["photo-1"] is models[1]


def test_thumbnail_update_reaches_filtered_model_by_identity(qapp) -> None:
    first = ImageFileModel("/fake/first.jpg", photo_id="first")
    second = ImageFileModel("/fake/second.jpg", photo_id="second", mark="入选")
    browser = ThumbnailBrowser()
    browser.set_models([first, second])
    browser.toggle_picks_only()
    image = QImage(32, 24, QImage.Format.Format_RGB32)

    changes: list[tuple[int, list[int]]] = []
    browser._photo_list_model.dataChanged.connect(
        lambda top_left, _bottom_right, roles: changes.append(
            (top_left.row(), list(roles))
        )
    )

    browser.update_thumbnail(1, image)

    assert second.thumbnail is not None
    assert changes == [(0, [int(PhotoRoles.THUMBNAIL)])]


def test_selected_indexes_are_indexes_in_current_filtered_models(qapp) -> None:
    models = [
        ImageFileModel("/fake/a.jpg"),
        ImageFileModel("/fake/b.jpg", mark="入选"),
    ]
    browser = ThumbnailBrowser()
    browser.set_models(models)
    browser.toggle_picks_only()
    browser.select_index(0)

    assert browser.selected_indexes() == [0]


def test_select_index_updates_view_selection(qapp) -> None:
    browser = ThumbnailBrowser()
    browser.set_models([ImageFileModel("/fake/a.jpg"), ImageFileModel("/fake/b.jpg")])

    browser.select_index(1)

    assert browser.selected_indexes() == [1]


def test_refresh_badges_reapplies_active_picks_filter(qapp) -> None:
    picked = ImageFileModel("/fake/picked.jpg", mark="入选")
    browser = ThumbnailBrowser()
    browser.set_models([picked])
    browser.toggle_picks_only()
    picked.mark = "待定"

    browser.refresh_badges()

    assert browser.current_models() == []


def test_current_and_legacy_keep_marks_share_filter_and_sidebar_counts(qapp) -> None:
    current = ImageFileModel(
        "/fake/current.jpg",
        photo_id="current",
        mark="保留",
    )
    legacy = ImageFileModel(
        "/fake/legacy.jpg",
        photo_id="legacy",
        mark="入选",
    )
    discarded = ImageFileModel(
        "/fake/discarded.jpg",
        photo_id="discarded",
        mark="舍弃",
    )
    browser = ThumbnailBrowser()
    browser.set_models([current, legacy, discarded])

    browser.toggle_picks_only()

    assert browser.current_photo_ids() == ["current", "legacy"]
    assert browser._sidebar._picks_label.text() == "保留 · 2"
    assert browser._sidebar._rejects_label.text() == "舍弃 · 1"


def test_filtered_selection_is_exposed_and_restored_by_photo_id(qapp) -> None:
    first = ImageFileModel("/fake/first.jpg", photo_id="first")
    second = ImageFileModel(
        "/fake/second.jpg",
        photo_id="second",
        mark="入选",
    )
    browser = ThumbnailBrowser()
    browser.set_models([first, second])
    browser.toggle_picks_only()

    browser.select_photo_id("second")

    assert browser.current_photo_ids() == ["second"]
    assert browser.selected_photo_ids() == ["second"]
    assert browser.selected_indexes() == [0]


def test_double_click_passes_photo_id_to_main_window(qapp, monkeypatch) -> None:
    browser = ThumbnailBrowser()
    browser.set_models(
        [ImageFileModel("/fake/photo.jpg", photo_id="stable-photo")]
    )
    calls: list[str] = []

    class MainWindow:
        def enter_preview_photo(self, photo_id: str) -> None:
            calls.append(photo_id)

    monkeypatch.setattr(browser, "_find_main_window", lambda: MainWindow())

    browser._on_card_double_clicked(0)

    assert calls == ["stable-photo"]


def test_keyboard_business_commands_pass_selected_photo_ids(qapp) -> None:
    first = ImageFileModel("/fake/first.jpg", photo_id="first")
    second = ImageFileModel(
        "/fake/second.jpg",
        photo_id="second",
        mark="入选",
    )
    browser = ThumbnailBrowser()
    browser.set_models([first, second])
    browser.toggle_picks_only()
    browser.select_photo_id("second")
    calls: list[tuple[str, str, str | None]] = []

    class MainWindow:
        def enter_preview_photo(self, photo_id: str) -> None:
            calls.append(("preview", photo_id, None))

        def mark_photo(self, photo_id: str, mark: str) -> None:
            calls.append(("mark", photo_id, mark))

        def delete_photo(self, photo_id: str) -> None:
            calls.append(("delete", photo_id, None))

    manager = KeyboardManager()
    manager._mw = MainWindow()
    manager._browser = browser
    manager._in_preview = lambda: False

    manager._on_space()
    manager._mark_selected("待定")
    manager._on_delete()

    assert calls == [
        ("preview", "second", None),
        ("mark", "second", "待定"),
        ("delete", "second", None),
    ]


def test_browser_repairs_legacy_empty_photo_id_at_ui_boundary(qapp) -> None:
    model = ImageFileModel("/fake/legacy.jpg")
    model.photo_id = ""
    browser = ThumbnailBrowser()

    browser.set_models([model])

    assert model.photo_id == "/fake/legacy.jpg"
    assert browser.current_photo_ids() == ["/fake/legacy.jpg"]


def test_browser_preserves_nonempty_opaque_photo_id(qapp) -> None:
    model = ImageFileModel("/fake/photo.jpg", photo_id=" photo ")
    browser = ThumbnailBrowser()
    browser.set_models([model])
    image = QImage(8, 8, QImage.Format.Format_RGB32)

    browser.update_model_thumbnail(model, image)

    assert browser.current_photo_ids() == [" photo "]
    assert model.thumbnail is not None
