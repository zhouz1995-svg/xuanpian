from __future__ import annotations

from PIL import ExifTags, Image
import pytest

from src.xuanpian.engine.exif_reader import (
    ExifData,
    _fmt_aperture,
    _fmt_flash,
    _fmt_focal,
    _fmt_shutter,
    read_exif,
)


def test_exif_defaults_are_missing_not_fabricated() -> None:
    data = ExifData(path="photo.jpg")
    assert data.camera_model == ""
    assert data.image_width == 0
    assert data.error == ""


@pytest.mark.parametrize(("value", "expected"), [(2.8, "f/2.8"), (None, "")])
def test_aperture_format(value: object, expected: str) -> None:
    assert _fmt_aperture(value) == expected


def test_shutter_and_focal_format() -> None:
    assert _fmt_shutter(0.002) == "1/500s"
    assert _fmt_shutter(1.0) == "1s"
    assert _fmt_focal(85.0) == "85mm"


@pytest.mark.parametrize(("bits", "expected"), [(0, ""), (1, "闪光"), (9, "闪光")])
def test_flash_uses_fired_bit(bits: int, expected: str) -> None:
    assert _fmt_flash(bits) == expected


def test_read_jpeg_dimensions_and_camera(tmp_path) -> None:
    path = tmp_path / "camera.jpg"
    image = Image.new("RGB", (100, 80), "blue")
    exif = image.getexif()
    exif[0x010F] = "Nikon"
    exif[0x0110] = "D850"
    image.save(path, exif=exif)

    data = read_exif(str(path))

    assert (data.image_width, data.image_height) == (100, 80)
    assert (data.camera_make, data.camera_model) == ("Nikon", "D850")
    assert data.error == ""


def test_read_jpeg_nested_exif_ifd_shooting_parameters(tmp_path) -> None:
    path = tmp_path / "nested-camera.jpg"
    image = Image.new("RGB", (120, 90), "blue")
    exif = Image.Exif()
    exif[0x010F] = "Nikon"
    exif[0x0110] = "D850"
    nested = exif.get_ifd(ExifTags.IFD.Exif)
    nested[0x829D] = 2.8
    nested[0x829A] = 0.002
    nested[0x8827] = 400
    nested[0x920A] = 85.0
    nested[0x9003] = "2026:07:22 10:30:00"
    nested[0x9209] = 1
    nested[0xA434] = "85mm f/1.8"
    exif[ExifTags.IFD.Exif] = nested
    image.save(path, exif=exif)

    data = read_exif(str(path))

    assert data.aperture == "f/2.8"
    assert data.shutter_speed == "1/500s"
    assert data.iso == "400"
    assert data.focal_length == "85mm"
    assert data.date_taken == "2026:07:22 10:30:00"
    assert data.flash == "闪光"
    assert data.lens_model == "85mm f/1.8"


def test_jpeg_without_exif_keeps_empty_optional_fields(tmp_path) -> None:
    path = tmp_path / "plain.jpg"
    Image.new("RGB", (20, 10)).save(path)

    data = read_exif(str(path))

    assert data.camera_model == ""
    assert data.error == ""


def test_malformed_nested_ifd_returns_path_qualified_diagnostic(
    monkeypatch, caplog
) -> None:
    path = "/photos/broken-exif.jpg"

    class MalformedExif:
        def items(self):
            return [(0x010F, "Nikon")]

        def get_ifd(self, ifd):
            raise ValueError("malformed nested Exif IFD")

    class MalformedImage:
        size = (120, 90)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def getexif(self):
            return MalformedExif()

    monkeypatch.setattr(Image, "open", lambda candidate: MalformedImage())

    data = read_exif(path)

    assert path in data.error
    assert "ValueError: malformed nested Exif IFD" in data.error
    assert path in caplog.text


def test_corrupt_file_returns_path_qualified_diagnostic(tmp_path, caplog) -> None:
    path = tmp_path / "broken.jpg"
    path.write_bytes(b"not an image")

    data = read_exif(str(path))

    assert str(path) in data.error
    assert "cannot identify image file" in data.error
    assert str(path) in caplog.text
