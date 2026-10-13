from __future__ import annotations

import pytest

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QListView, QListWidget, QStyleOptionViewItem, QWidget

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.photo_list_model import PhotoListModel, PhotoRoles
from src.xuanpian.ui.preview_widget import FilmstripDelegate, PreviewWidget, _CompareDelegate
from src.xuanpian.ui.theme import DANGER, SUCCESS


def _models(count: int = 1_000) -> dict[str, ImageFileModel]:
    return {
        f"photo-{row:04d}": ImageFileModel(
            f"/missing/photo-{row:04d}.jpg",
            photo_id=f"photo-{row:04d}",
        )
        for row in range(count)
    }


def test_real_preview_filmstrip_is_model_view_virtualized(qapp) -> None:
    models = _models()
    photo_ids = list(models)
    shared_model = PhotoListModel(models.get)
    shared_model.set_photo_ids(photo_ids)
    preview = PreviewWidget()
    preview.resize(1_000, 700)
    preview.show()
    qapp.processEvents()
    resets = 0

    def count_reset() -> None:
        nonlocal resets
        resets += 1

    shared_model.modelReset.connect(count_reset)
    preview.load_photo(
        "photo-0500",
        lambda: list(photo_ids),
        models.get,
        photo_list_model=shared_model,
    )
    qapp.processEvents()
    child_widgets = len(preview._filmstrip.findChildren(QWidget))

    preview.navigate(1)
    preview.navigate(-1)
    preview._show_photo_id("photo-0777")
    qapp.processEvents()

    assert isinstance(preview._filmstrip, QListView)
    assert not isinstance(preview._filmstrip, QListWidget)
    assert preview._filmstrip.model() is shared_model
    assert shared_model.rowCount() == 1_000
    assert resets == 0
    assert len(preview._filmstrip.findChildren(QWidget)) <= child_widgets
    assert preview.current_photo_id() == "photo-0777"
    assert preview._filmstrip.currentIndex().data(int(PhotoRoles.ID)) == (
        "photo-0777"
    )


def test_filmstrip_click_selects_stable_photo_id_without_model_reset(qapp) -> None:
    models = _models(20)
    photo_ids = list(models)
    preview = PreviewWidget()
    preview.resize(1_000, 700)
    preview.show()
    preview.load_photo("photo-0001", lambda: list(photo_ids), models.get)
    qapp.processEvents()
    resets = 0

    def count_reset() -> None:
        nonlocal resets
        resets += 1

    preview._filmstrip_model.modelReset.connect(count_reset)
    target = preview._filmstrip_model.index(12, 0)
    preview._filmstrip.scrollTo(target)
    qapp.processEvents()
    QTest.mouseClick(
        preview._filmstrip.viewport(),
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
        preview._filmstrip.visualRect(target).center(),
    )
    qapp.processEvents()

    assert preview.current_photo_id() == "photo-0012"
    assert resets == 0


def test_mark_and_thumbnail_updates_are_precise_for_shared_filmstrip_model(
    qapp,
) -> None:
    models = _models(5)
    photo_ids = list(models)
    shared_model = PhotoListModel(models.get)
    shared_model.set_photo_ids(photo_ids)
    preview = PreviewWidget()
    preview.load_photo(
        "photo-0002",
        lambda: list(photo_ids),
        models.get,
        photo_list_model=shared_model,
    )
    changes: list[tuple[int, list[int]]] = []
    shared_model.dataChanged.connect(
        lambda first, _last, roles: changes.append(
            (first.row(), list(roles))
        )
    )

    models["photo-0002"].mark = "入选"
    shared_model.update_photo(
        "photo-0002",
        [PhotoRoles.MARK, PhotoRoles.THUMBNAIL],
    )

    assert changes == [
        (
            2,
            [int(PhotoRoles.MARK), int(PhotoRoles.THUMBNAIL)],
        )
    ]


@pytest.mark.parametrize(
    ("delegate_type", "mark", "expected_color"),
    (
        (FilmstripDelegate, "保留", SUCCESS),
        (FilmstripDelegate, "入选", SUCCESS),
        (_CompareDelegate, "舍弃", DANGER),
        (_CompareDelegate, "淘汰", DANGER),
    ),
)
def test_preview_delegates_paint_current_and_legacy_mark_stripes(
    qapp,
    delegate_type,
    mark: str,
    expected_color,
) -> None:
    models = {
        "photo": ImageFileModel("/fake/photo.jpg", photo_id="photo", mark=mark)
    }
    model = PhotoListModel(models.get)
    model.set_photo_ids(["photo"])
    image = QImage(220, 180, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 200, 160)
    painter = QPainter(image)

    delegate_type().paint(painter, option, model.index(0, 0))
    painter.end()

    assert image.pixelColor(3, 20).name() == expected_color.name()


def test_preview_keyboard_left_and_right_navigate_without_rebuild(qapp) -> None:
    models = _models(5)
    photo_ids = list(models)
    preview = PreviewWidget()
    preview.load_photo("photo-0002", lambda: list(photo_ids), models.get)
    resets = 0

    def count_reset() -> None:
        nonlocal resets
        resets += 1

    preview._filmstrip_model.modelReset.connect(count_reset)
    preview.show()
    preview.setFocus()

    QTest.keyClick(preview, Qt.Key.Key_Right)
    qapp.processEvents()
    assert preview.current_photo_id() == "photo-0003"
    QTest.keyClick(preview, Qt.Key.Key_Left)
    qapp.processEvents()

    assert preview.current_photo_id() == "photo-0002"
    assert resets == 0
    preview.close()


def test_hot_navigation_never_copies_photo_ids_from_provider(qapp) -> None:
    models = _models()
    photo_ids = list(models)
    shared_model = PhotoListModel(models.get)
    shared_model.set_photo_ids(photo_ids)
    provider_calls = 0

    def photo_ids_provider() -> list[str]:
        nonlocal provider_calls
        provider_calls += 1
        return list(photo_ids)

    preview = PreviewWidget()
    preview.load_photo(
        "photo-0500",
        photo_ids_provider,
        models.get,
        photo_list_model=shared_model,
    )
    resets = 0

    def count_reset() -> None:
        nonlocal resets
        resets += 1

    shared_model.modelReset.connect(count_reset)
    for _ in range(500):
        preview.navigate(1)
        preview.navigate(-1)
        preview.current_photo_id()

    assert provider_calls == 0
    assert resets == 0
    assert preview.current_photo_id() == "photo-0500"


def test_external_model_removal_selects_neighbor_and_renders_immediately(
    qapp,
) -> None:
    models = _models(5)
    shared_model = PhotoListModel(models.get)
    shared_model.set_photo_ids(list(models))
    preview = PreviewWidget()
    preview.load_photo(
        "photo-0002",
        lambda: list(models),
        models.get,
        photo_list_model=shared_model,
    )

    shared_model.set_photo_ids(
        ["photo-0000", "photo-0001", "photo-0003", "photo-0004"]
    )

    assert preview.current_photo_id() == "photo-0003"
    assert preview._top_filename.text() == "photo-0003.jpg"
    assert preview._top_position.text() == "3 / 4"
    assert preview._filmstrip.currentIndex().data(int(PhotoRoles.ID)) == (
        "photo-0003"
    )


def test_external_filter_reset_keeps_stable_id_and_updates_position(qapp) -> None:
    models = _models(8)
    shared_model = PhotoListModel(models.get)
    shared_model.set_photo_ids(list(models))
    preview = PreviewWidget()
    preview.load_photo(
        "photo-0005",
        lambda: list(models),
        models.get,
        photo_list_model=shared_model,
    )

    shared_model.set_photo_ids(
        ["photo-0007", "photo-0005", "photo-0003"]
    )

    assert preview.current_photo_id() == "photo-0005"
    assert preview._top_position.text() == "2 / 3"
    assert preview._filmstrip.currentIndex().row() == 1


def test_external_reset_without_current_uses_previous_row_neighbor(qapp) -> None:
    models = _models(8)
    shared_model = PhotoListModel(models.get)
    shared_model.set_photo_ids(list(models))
    preview = PreviewWidget()
    preview.load_photo(
        "photo-0005",
        lambda: list(models),
        models.get,
        photo_list_model=shared_model,
    )

    shared_model.set_photo_ids(
        ["photo-0000", "photo-0001", "photo-0002"]
    )

    assert preview.current_photo_id() == "photo-0002"
    assert preview._top_position.text() == "3 / 3"


def test_external_insert_updates_position_without_rerendering_photo(qapp) -> None:
    models = _models(5)
    shared_model = PhotoListModel(models.get)
    shared_model.set_photo_ids(["photo-0000", "photo-0001"])
    preview = PreviewWidget()
    preview.load_photo(
        "photo-0001",
        lambda: list(models),
        models.get,
        photo_list_model=shared_model,
    )

    shared_model.set_photo_ids(
        ["photo-0000", "photo-0001", "photo-0002"]
    )

    assert preview.current_photo_id() == "photo-0001"
    assert preview._top_position.text() == "2 / 3"
