"""Tests for RAW image support."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import numpy as np
from PIL import Image
from PyQt6.QtGui import QImage

from src.xuanpian.engine import image_reader
from src.xuanpian.ui.image_model import ALL_SUPPORTED, ImageFileModel, scan_images
from src.xuanpian.ui import async_preview_loader
from src.xuanpian.ui import preview_widget
from src.xuanpian.ui.preview_widget import decode_preview_image


def _make_test_image(tmp_path, filename: str, w: int = 100, h: int = 75) -> str:
    path = tmp_path / filename
    img = Image.new("RGB", (w, h), color=(128, 128, 128))
    img.save(path)
    return str(path)


class TestImageFileModelRaw:
    def test_is_raw_detection(self):
        model = ImageFileModel(path="/fake/photo.cr2", filename="photo.cr2")
        # is_raw may be True if rawpy is available, False otherwise
        assert isinstance(model.is_raw, bool)

    def test_jpeg_is_not_raw(self):
        model = ImageFileModel(path="/fake/photo.jpg", filename="photo.jpg")
        assert model.is_raw is False

    def test_all_supported_includes_standard_formats(self):
        assert ".jpg" in ALL_SUPPORTED
        assert ".png" in ALL_SUPPORTED

    def test_new_fields_exist(self):
        model = ImageFileModel(path="/fake/test.jpg")
        assert hasattr(model, "technical_status")
        assert hasattr(model, "focus_conclusion")
        assert hasattr(model, "exposure_conclusion")
        assert hasattr(model, "face_count")
        assert hasattr(model, "portrait_assessed")
        assert hasattr(model, "is_raw")


class TestScanImages:
    def test_handles_mixed_files(self, tmp_path):
        _make_test_image(tmp_path, "photo_a.jpg")
        _make_test_image(tmp_path, "photo_b.png")
        # Create a dummy RAW file
        raw_path = tmp_path / "photo_c.cr2"
        raw_path.write_bytes(b"fake raw data")
        (tmp_path / "notes.txt").write_text("not an image")

        paths = scan_images(str(tmp_path))
        # JPEG and PNG always included
        assert any("photo_a.jpg" in p for p in paths)
        assert any("photo_b.png" in p for p in paths)
        # CR2 may or may not be included depending on rawpy availability


def test_embedded_thumbnail_failure_is_logged_before_fallback(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    path = tmp_path / "broken.cr2"

    def fail_read(_path: str):
        raise RuntimeError("embedded preview unavailable")

    monkeypatch.setitem(sys.modules, "rawpy", SimpleNamespace(imread=fail_read))

    with caplog.at_level(logging.WARNING, logger=image_reader.__name__):
        result = image_reader._try_extract_thumb(str(path))

    assert result is None
    assert str(path) in caplog.text
    assert "embedded preview unavailable" in caplog.text


def test_preview_raw_failure_reaches_async_error_path(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "broken.cr2"

    def fail_read(_path: str):
        raise RuntimeError("preview decode failed")

    monkeypatch.setitem(sys.modules, "rawpy", SimpleNamespace(imread=fail_read))

    with pytest.raises(RuntimeError, match="preview decode failed"):
        decode_preview_image(str(path))


class _FakeRaw:
    def __init__(self, thumb, *, postprocess_result=None, postprocess_error=None):
        self._thumb = thumb
        self._postprocess_result = postprocess_result
        self._postprocess_error = postprocess_error
        self.postprocess_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def extract_thumb(self):
        return self._thumb

    def postprocess(self, **_kwargs):
        self.postprocess_calls += 1
        if self._postprocess_error is not None:
            raise self._postprocess_error
        return self._postprocess_result


def _install_fake_rawpy(monkeypatch, raw: _FakeRaw) -> None:
    monkeypatch.setitem(
        sys.modules,
        "rawpy",
        SimpleNamespace(
            imread=lambda _path: raw,
            ThumbFormat=SimpleNamespace(JPEG="jpeg", BITMAP="bitmap"),
        ),
    )


@pytest.mark.parametrize(
    "thumb",
    [
        SimpleNamespace(format="jpeg", data=b"not a jpeg"),
        SimpleNamespace(format="bitmap", data=object()),
    ],
)
def test_preview_raw_embedded_decode_failure_falls_back_to_postprocess(
    tmp_path: Path, monkeypatch, thumb
) -> None:
    path = tmp_path / "recoverable.cr2"
    raw = _FakeRaw(
        thumb,
        postprocess_result=np.full((12, 16, 3), 80, dtype=np.uint8),
    )
    _install_fake_rawpy(monkeypatch, raw)

    image = decode_preview_image(str(path))

    assert not image.isNull()
    assert image.size().width() == 16
    assert image.size().height() == 12
    assert raw.postprocess_calls == 1


def test_preview_raw_empty_embedded_qimage_falls_back_to_postprocess(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "empty-embedded.cr2"
    raw = _FakeRaw(
        SimpleNamespace(format="bitmap", data=np.zeros((4, 6, 3), dtype=np.uint8)),
        postprocess_result=np.full((10, 14, 3), 120, dtype=np.uint8),
    )
    _install_fake_rawpy(monkeypatch, raw)
    real_convert = async_preview_loader._pil_preview_to_qimage
    calls = 0

    def first_empty_then_real(image, max_edge):
        nonlocal calls
        calls += 1
        if calls == 1:
            return QImage()
        return real_convert(image, max_edge)

    monkeypatch.setattr(
        async_preview_loader,
        "_pil_preview_to_qimage",
        first_empty_then_real,
    )

    image = decode_preview_image(str(path))

    assert not image.isNull()
    assert image.size().width() == 14
    assert raw.postprocess_calls == 1


def test_preview_raw_embedded_and_postprocess_failure_reports_final_error(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "unrecoverable.cr2"
    raw = _FakeRaw(
        SimpleNamespace(format="jpeg", data=b"not a jpeg"),
        postprocess_error=RuntimeError("sensor decode failed"),
    )
    _install_fake_rawpy(monkeypatch, raw)

    with pytest.raises(RuntimeError, match="sensor decode failed"):
        decode_preview_image(str(path))

    assert raw.postprocess_calls == 1
