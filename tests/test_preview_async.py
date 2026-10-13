from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QColor, QImage

from src.xuanpian.ui import preview_widget
from src.xuanpian.ui.image_model import ImageFileModel
from src.xuanpian.ui.preview_widget import PreviewWidget


def _image(color: str = "red", width: int = 32, height: int = 24) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def _wait(qapp, predicate, timeout_ms: int = 3_000) -> None:
    deadline = __import__("time").monotonic() + timeout_ms / 1_000
    while not predicate() and __import__("time").monotonic() < deadline:
        qapp.processEvents()
        QThread.msleep(1)
    assert predicate()


def test_preview_decoder_runs_off_gui_thread(qapp) -> None:
    gui_thread = QThread.currentThread()
    decoder_threads: list[QThread] = []

    def decoder(_path: str, _max_edge: int) -> QImage:
        decoder_threads.append(QThread.currentThread())
        return _image()

    preview = PreviewWidget(preview_decoder=decoder)
    model = ImageFileModel("/fake/photo.jpg", photo_id="photo")
    loaded: list[tuple[str, int]] = []
    preview.preview_loaded.connect(lambda photo_id, token: loaded.append((photo_id, token)))

    preview.load_photo("photo", lambda: ["photo"], {"photo": model}.get)
    _wait(qapp, lambda: bool(loaded))

    assert decoder_threads
    assert decoder_threads[0] is not gui_thread
    preview.shutdown_preview_loader()


def test_preview_uses_public_bounded_preview_loader(qapp) -> None:
    from src.xuanpian.ui.async_preview_loader import BoundedPreviewLoader

    preview = PreviewWidget()

    assert isinstance(preview._preview_loader, BoundedPreviewLoader)
    assert not hasattr(preview, "_active_preview_thread")
    assert not hasattr(preview, "_pending_preview_request")
    preview.shutdown_preview_loader()


def test_rapid_navigation_discards_stale_result_and_coalesces_pending(qapp) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    decoded: list[str] = []

    def decoder(path: str, _max_edge: int) -> QImage:
        name = Path(path).stem
        decoded.append(name)
        if name == "first":
            first_started.set()
            assert release_first.wait(3)
        return _image({"first": "red", "second": "green", "last": "blue"}[name])

    models = {
        name: ImageFileModel(f"/fake/{name}.jpg", photo_id=name)
        for name in ("first", "second", "last")
    }
    preview = PreviewWidget(preview_decoder=decoder)
    loaded: list[str] = []
    preview.preview_loaded.connect(lambda photo_id, _token: loaded.append(photo_id))
    preview.load_photo("first", lambda: list(models), models.get)
    assert first_started.wait(2)

    preview._show_photo_id("second")
    preview._show_photo_id("last")
    assert preview.preview_unfinished_count <= 2
    assert preview.preview_pending_count <= 1
    release_first.set()
    _wait(qapp, lambda: loaded == ["last"])

    assert decoded == ["first", "last"]
    assert preview.loaded_photo_id == "last"
    assert preview._image_view._pixmap_item is not None
    assert preview._image_view._pixmap_item.pixmap().toImage().pixelColor(0, 0) == QColor("blue")
    preview.shutdown_preview_loader()


def test_preview_error_recovers_on_later_navigation(qapp) -> None:
    def decoder(path: str, _max_edge: int) -> QImage:
        if path.endswith("bad.jpg"):
            raise OSError("broken jpeg")
        return _image("green")

    models = {
        name: ImageFileModel(f"/fake/{name}.jpg", photo_id=name)
        for name in ("bad", "good")
    }
    preview = PreviewWidget(preview_decoder=decoder)
    errors: list[str] = []
    preview.preview_error.connect(
        lambda photo_id, _token, _message: errors.append(photo_id)
    )
    preview.load_photo("bad", lambda: list(models), models.get)
    _wait(qapp, lambda: errors == ["bad"])

    preview._show_photo_id("good")
    _wait(qapp, lambda: preview.loaded_photo_id == "good")

    assert preview.preview_state == "loaded"
    assert preview._image_view._pixmap_item is not None
    preview.shutdown_preview_loader()


def test_invalidated_request_cannot_update_closed_or_reset_preview(qapp) -> None:
    started = threading.Event()
    release = threading.Event()

    def decoder(_path: str, _max_edge: int) -> QImage:
        started.set()
        assert release.wait(3)
        return _image()

    model = ImageFileModel("/fake/photo.jpg", photo_id="photo")
    preview = PreviewWidget(preview_decoder=decoder)
    loaded: list[str] = []
    preview.preview_loaded.connect(lambda photo_id, _token: loaded.append(photo_id))
    preview.load_photo("photo", lambda: ["photo"], {"photo": model}.get)
    assert started.wait(2)

    preview.invalidate_preview_requests()
    release.set()
    _wait(qapp, lambda: preview.preview_unfinished_count == 0)

    assert loaded == []
    assert preview.loaded_photo_id is None
    preview.shutdown_preview_loader()


def test_model_reset_reloads_same_id_and_discards_old_path(qapp) -> None:
    first_started = threading.Event()
    release_first = threading.Event()
    decoded: list[str] = []

    def decoder(path: str, _max_edge: int) -> QImage:
        decoded.append(path)
        if path.endswith("old.jpg"):
            first_started.set()
            assert release_first.wait(3)
            return _image("red")
        return _image("green")

    model = ImageFileModel("/fake/old.jpg", photo_id="photo")
    preview = PreviewWidget(preview_decoder=decoder)
    loaded: list[str] = []
    preview.preview_loaded.connect(lambda photo_id, _token: loaded.append(photo_id))
    preview.load_photo("photo", lambda: ["photo"], {"photo": model}.get)
    assert first_started.wait(2)

    model.path = "/fake/new.jpg"
    preview._filmstrip_model.set_photo_ids(["missing-neighbor", "photo"])
    release_first.set()
    _wait(qapp, lambda: preview.loaded_photo_id == "photo")

    assert decoded == ["/fake/old.jpg", "/fake/new.jpg"]
    assert loaded == ["photo"]
    assert preview._image_view._pixmap_item is not None
    assert (
        preview._image_view._pixmap_item.pixmap().toImage().pixelColor(0, 0)
        == QColor("green")
    )
    preview.shutdown_preview_loader()


def test_close_waits_for_active_decoder_and_leaves_no_worker(qapp) -> None:
    started = threading.Event()
    release = threading.Event()

    def decoder(_path: str, _max_edge: int) -> QImage:
        started.set()
        assert release.wait(3)
        return _image()

    model = ImageFileModel("/fake/photo.jpg", photo_id="photo")
    preview = PreviewWidget(preview_decoder=decoder)
    preview.show()
    preview.load_photo("photo", lambda: ["photo"], {"photo": model}.get)
    assert started.wait(2)
    releaser = threading.Thread(target=release.set)
    releaser.start()

    assert preview.close()
    releaser.join()
    qapp.processEvents()

    assert preview.preview_unfinished_count == 0
    assert preview.loaded_photo_id is None
    assert preview.preview_state == "idle"


def test_close_returns_before_blocked_decoder_and_reaper_drains(qapp) -> None:
    started = threading.Event()
    release = threading.Event()
    close_returned = threading.Event()
    close_was_blocked = threading.Event()

    def decoder(_path: str, _max_edge: int) -> QImage:
        started.set()
        release.wait()
        return _image()

    model = ImageFileModel("/fake/photo.jpg", photo_id="photo")
    preview = PreviewWidget(preview_decoder=decoder)
    delivered: list[str] = []
    preview.preview_loaded.connect(
        lambda photo_id, _token: delivered.append(photo_id)
    )
    preview.show()
    preview.load_photo("photo", lambda: ["photo"], {"photo": model}.get)
    assert started.wait(2)

    def release_if_close_does_not_return() -> None:
        if not close_returned.wait(0.75):
            close_was_blocked.set()
        release.set()

    watchdog = threading.Thread(target=release_if_close_does_not_return)
    watchdog.start()
    assert preview.close()
    close_returned.set()
    watchdog.join(2)

    assert not close_was_blocked.is_set()
    assert delivered == []
    _wait(qapp, lambda: preview_widget.preview_decode_reaper_count() == 0)
    assert preview.preview_unfinished_count == 0

    # Shutdown is idempotent after ownership moved to the reaper.
    preview.shutdown_preview_loader()
    preview.shutdown_preview_loader()


def test_application_quit_with_blocked_decoder_exits_subprocess(
    tmp_path: Path,
) -> None:
    code = textwrap.dedent(
        """
        import threading
        from PyQt6.QtCore import QTimer
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QImage
        from src.xuanpian.ui.image_model import ImageFileModel
        from src.xuanpian.ui.preview_widget import PreviewWidget

        app = QApplication([])
        started = threading.Event()

        def decoder(_path, _max_edge):
            started.set()
            threading.Event().wait()
            return QImage()

        model = ImageFileModel("/fake/photo.jpg", photo_id="photo")
        preview = PreviewWidget(preview_decoder=decoder)
        preview.load_photo("photo", lambda: ["photo"], {"photo": model}.get)
        while not started.wait(0.001):
            app.processEvents()
        QTimer.singleShot(0, lambda: (preview.close(), app.quit()))
        raise SystemExit(app.exec())
        """
    )
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONPATH"] = str(Path.cwd())
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path.cwd(),
            env=environment,
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("application process did not exit with blocked preview decoder")

    assert completed.returncode == 0, completed.stderr


def test_real_jpeg_loads_and_tools_transform_without_redecode(qapp, tmp_path) -> None:
    source = tmp_path / "real.jpg"
    assert _image("magenta", 96, 64).save(str(source), "JPEG")
    calls = 0

    from src.xuanpian.ui.preview_widget import decode_preview_image

    def decoder(path: str, max_edge: int) -> QImage:
        nonlocal calls
        calls += 1
        return decode_preview_image(path, max_edge)

    model = ImageFileModel(
        str(source),
        photo_id="real",
        image_width=96,
        image_height=64,
        exif_loaded=True,
    )
    preview = PreviewWidget(preview_decoder=decoder)
    preview.resize(900, 600)
    preview.show()
    preview.load_photo("real", lambda: ["real"], {"real": model}.get)
    _wait(qapp, lambda: preview.loaded_photo_id == "real")

    assert preview._image_view._pixmap_item is not None
    initial_zoom = preview._image_view.zoom_level
    preview._tool_buttons["zoom_in"].click()
    assert preview._image_view.zoom_level > initial_zoom
    preview._tool_buttons["rotate_right"].click()
    assert preview._image_view.rotation == 90
    preview._tool_buttons["actual"].click()
    assert preview._image_view.zoom_level == 1.0
    assert preview._exif_values["dimensions"].text() == "96 × 64"
    assert preview._filename_value.text() == "real.jpg"
    assert calls == 1
    preview.shutdown_preview_loader()
