from PIL import Image
from PyQt6.QtGui import QPixmap

from src.xuanpian.ui.image_view import ImageView
from src.xuanpian.ui.thumbnail_loader import load_thumbnail


def test_image_view_fit_100_and_rotation(qapp) -> None:
    view = ImageView()
    view.resize(400, 300)
    view.set_pixmap(QPixmap(200, 100))

    view.zoom_to_100()

    assert view.zoom_level == 1.0
    assert view.rotate(90) == 90
    assert view.rotation == 90
    assert view.rotate(-180) == 270


def test_rotation_changes_view_state_only(qapp, tmp_path) -> None:
    source = tmp_path / "original.jpg"
    source.write_bytes(b"immutable")
    view = ImageView()
    view.set_pixmap(QPixmap(20, 10))

    view.rotate(90)

    assert source.read_bytes() == b"immutable"


def test_thumbnail_loader_applies_exif_orientation_before_resizing(tmp_path) -> None:
    source = tmp_path / "portrait-by-orientation.jpg"
    image = Image.new("RGB", (40, 20), "red")
    exif = image.getexif()
    exif[274] = 6
    image.save(source, exif=exif)

    loaded = load_thumbnail(str(source), (100, 100))

    assert loaded is not None
    assert (loaded.width(), loaded.height()) == (20, 40)
