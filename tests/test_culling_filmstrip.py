from __future__ import annotations

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtWidgets import QListView, QStyleOptionViewItem

from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.photo_list_model import PhotoRoles


def _models(count: int = 1_000) -> dict[str, ImageFileModel]:
    return {
        f"photo-{row:04d}": ImageFileModel(
            f"/not-read/photo-{row:04d}.jpg",
            photo_id=f"photo-{row:04d}",
        )
        for row in range(count)
    }


def test_filmstrip_uses_one_list_view_for_thousand_stable_ids(qapp) -> None:
    from src.xuanpian.ui.culling_filmstrip import CullingFilmstrip

    models = _models()
    strip = CullingFilmstrip(models.get)

    strip.set_photo_ids(models)

    assert strip.findChildren(QListView) == [strip._view]
    assert strip._view.model() is strip._model
    assert strip._model.rowCount() == 1_000
    assert strip._view.flow() is QListView.Flow.TopToBottom
    assert strip._view.uniformItemSizes()


def test_activation_emits_stable_photo_id_instead_of_path(qapp) -> None:
    from src.xuanpian.ui.culling_filmstrip import CullingFilmstrip

    models = _models(3)
    strip = CullingFilmstrip(models.get)
    strip.set_photo_ids(models)
    activated: list[str] = []
    strip.photo_activated.connect(activated.append)

    strip._view.activated.emit(strip._model.index(1, 0))

    assert activated == ["photo-0001"]
    assert activated[0] != models["photo-0001"].path


def test_selection_survives_reset_by_id_and_clears_when_id_disappears(
    qapp,
) -> None:
    from src.xuanpian.ui.culling_filmstrip import CullingFilmstrip

    models = _models(4)
    strip = CullingFilmstrip(models.get)
    strip.set_photo_ids(models)
    strip.select_photo_id("photo-0002")

    strip.set_photo_ids(["photo-0003", "photo-0002", "photo-0000"])

    assert strip._view.currentIndex().data(int(PhotoRoles.ID)) == "photo-0002"
    assert strip._view.selectionModel().isSelected(strip._model.index(1, 0))

    strip.set_photo_ids(["photo-0003", "photo-0000"])

    assert not strip._view.currentIndex().isValid()
    assert not strip._view.selectionModel().hasSelection()


def test_selecting_unknown_id_clears_existing_selection(qapp) -> None:
    from src.xuanpian.ui.culling_filmstrip import CullingFilmstrip

    models = _models(2)
    strip = CullingFilmstrip(models.get)
    strip.set_photo_ids(models)
    strip.select_photo_id("photo-0000")
    assert strip._view.selectionModel().isSelected(strip._model.index(0, 0))

    strip.select_photo_id("missing")

    assert not strip._view.currentIndex().isValid()
    assert not strip._view.selectionModel().hasSelection()


def test_reset_filters_disappeared_provider_id_and_clears_selection(qapp) -> None:
    from src.xuanpian.ui.culling_filmstrip import CullingFilmstrip

    models = _models(2)
    strip = CullingFilmstrip(models.__getitem__)
    strip.set_photo_ids(models)
    strip.select_photo_id("photo-0001")
    del models["photo-0001"]

    strip.set_photo_ids(["photo-0000", "photo-0001"])

    assert strip._model.photo_ids() == ["photo-0000"]
    assert strip.current_photo_id() is None
    assert not strip._view.selectionModel().hasSelection()


def test_delegate_paint_fails_closed_when_provider_disappears(qapp) -> None:
    from src.xuanpian.ui.culling_filmstrip import CullingFilmstrip

    models = _models(1)
    strip = CullingFilmstrip(models.__getitem__)
    strip.set_photo_ids(models)
    index = strip._model.index(0, 0)
    del models["photo-0000"]
    image = QImage(120, 100, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 110, 90)
    painter = QPainter(image)
    try:
        strip._view.itemDelegate().paint(painter, option, index)
    finally:
        painter.end()

    assert image.size().width() == 120
