from __future__ import annotations

import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtWidgets import QApplication

from src.xuanpian.ui.image_model import ImageFileModel, scan_images
from src.xuanpian.ui.thumbnail_browser import ThumbnailBrowser
from src.xuanpian.ui.thumbnail_loader import ThumbnailLoader, load_thumbnail


class TestScanToModelPipeline:
    """Test the full scan -> model creation pipeline."""

    def test_scan_produces_valid_models(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "photo.jpg").touch()
            (Path(tmp) / "image.png").touch()

            paths = scan_images(tmp)
            models = [ImageFileModel(path=p) for p in paths]

            assert len(models) == 2
            for model in models:
                assert isinstance(model, ImageFileModel)
                assert model.path.endswith((".jpg", ".png"))
                assert model.filename in ("photo.jpg", "image.png")
                assert model.thumbnail is None

    def test_scan_empty_folder_produces_empty_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = scan_images(tmp)
            models = [ImageFileModel(path=p) for p in paths]
            assert len(models) == 0

    def test_non_image_files_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "doc.txt").touch()
            (Path(tmp) / "data.pdf").touch()
            (Path(tmp) / "real.jpg").touch()

            paths = scan_images(tmp)
            assert len(paths) == 1
            assert paths[0].endswith(".jpg")


class TestThumbnailBrowser:
    """Test ThumbnailBrowser widget behavior."""

    def test_initial_empty_state(self, qapp: QApplication) -> None:
        browser = ThumbnailBrowser()
        assert not browser._empty_widget.isHidden()
        assert browser._view.isHidden()

    def test_set_models_shows_grid(self, qapp: QApplication) -> None:
        browser = ThumbnailBrowser()
        models = [
            ImageFileModel(path="/tmp/a.jpg"),
            ImageFileModel(path="/tmp/b.jpg"),
        ]
        browser.set_models(models)
        assert browser._empty_widget.isHidden()
        assert not browser._view.isHidden()
        assert browser._photo_list_model.rowCount() == 2

    def test_set_empty_models_shows_empty_state(self, qapp: QApplication) -> None:
        browser = ThumbnailBrowser()
        browser.set_models([])
        assert not browser._empty_widget.isHidden()
        assert browser._view.isHidden()

    def test_update_thumbnail_sets_icon(self, qapp: QApplication) -> None:
        browser = ThumbnailBrowser()
        model = ImageFileModel(path="/tmp/a.jpg")
        browser.set_models([model])

        qimage = QImage(100, 80, QImage.Format.Format_RGB888)
        qimage.fill(Qt.GlobalColor.red)
        browser.update_thumbnail(0, qimage)

        assert model.thumbnail is not None
        assert isinstance(model.thumbnail, QPixmap)

    def test_update_thumbnail_out_of_range(self, qapp: QApplication) -> None:
        browser = ThumbnailBrowser()
        model = ImageFileModel(path="/tmp/a.jpg")
        browser.set_models([model])

        qimage = QImage(100, 80, QImage.Format.Format_RGB888)
        browser.update_thumbnail(-1, qimage)
        browser.update_thumbnail(5, qimage)

        assert model.thumbnail is None


class TestThumbnailLoader:
    """Test ThumbnailLoader thread behavior."""

    def test_loader_stop(self, qapp: QApplication) -> None:
        loader = ThumbnailLoader(["/tmp/a.jpg"] * 100)
        loader.stop()
        assert loader._stopped is True

    @pytest.mark.parametrize("complete_before_start_returns", [False, True])
    def test_loader_generates_thumbnails(
        self, qapp: QApplication, qtbot, monkeypatch, complete_before_start_returns,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from PIL import Image

            img = Image.new("RGB", (60, 40), color="red")
            img_path = str(Path(tmp) / "test.png")
            img.save(img_path)

            loader = ThumbnailLoader([img_path])
            received: list[tuple[int, QImage]] = []

            def on_ready(index: int, qimage: QImage) -> None:
                received.append((index, qimage))

            loader.thumbnail_ready.connect(on_ready)
            if complete_before_start_returns:
                # Exercise the real work with deterministic immediate completion.
                monkeypatch.setattr(loader, "start", loader.run)
            try:
                with qtbot.waitSignal(loader.finished_loading, timeout=5000):
                    loader.start()
            finally:
                assert loader.wait(5000), "thumbnail worker did not finish"

            assert len(received) == 1
            assert received[0][0] == 0
            assert isinstance(received[0][1], QImage)
            assert not received[0][1].isNull()

    def test_raw_bitmap_thumbnail_is_converted_without_postprocess(
        self,
        qapp: QApplication,
        monkeypatch,
    ) -> None:
        bitmap_format = object()

        class FakeRaw:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def extract_thumb(self):
                return SimpleNamespace(
                    format=bitmap_format,
                    data=np.full((12, 16, 3), 127, dtype=np.uint8),
                )

            def postprocess(self, **kwargs):
                raise AssertionError("bitmap thumbnail must not postprocess RAW")

        fake_rawpy = SimpleNamespace(
            ThumbFormat=SimpleNamespace(JPEG=object(), BITMAP=bitmap_format),
            imread=lambda path: FakeRaw(),
        )
        monkeypatch.setitem(sys.modules, "rawpy", fake_rawpy)

        image = load_thumbnail("/fake/A.NEF", (20, 15))

        assert image is not None
        assert not image.isNull()
        assert (image.width(), image.height()) == (16, 12)
