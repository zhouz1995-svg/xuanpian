from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from PyQt6.QtCore import QThread
from PyQt6.QtGui import QColor, QImage

from src.xuanpian.ui.preview_widget import preview_decode_reaper_count


def _image(color: str = "red") -> QImage:
    image = QImage(24, 16, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def _process_until(qapp, predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
    assert predicate()


def test_loader_coalesces_pending_and_emits_latest_on_owner_thread(qapp) -> None:
    from src.xuanpian.ui.async_preview_loader import BoundedPreviewLoader

    first_started = threading.Event()
    release_first = threading.Event()
    decoded_paths: list[str] = []

    def decoder(path: str, _max_edge: int) -> QImage:
        decoded_paths.append(path)
        if path == "/fake/first.jpg":
            first_started.set()
            assert release_first.wait(3)
            return _image("red")
        return _image("green")

    loader = BoundedPreviewLoader(decoder=decoder)
    delivered: list[tuple[str, QThread]] = []
    loader.decoded.connect(
        lambda photo_id, _image: delivered.append(
            (photo_id, QThread.currentThread())
        )
    )
    loader.request("first", "/fake/first.jpg", 512)
    assert first_started.wait(2)
    loader.request("second", "/fake/second.jpg", 512)
    loader.request("last", "/fake/last.jpg", 512)

    assert loader.pending_count == 1
    assert loader.unfinished_count == 2
    release_first.set()
    _process_until(qapp, lambda: delivered and delivered[-1][0] == "last")

    assert decoded_paths == ["/fake/first.jpg", "/fake/last.jpg"]
    assert delivered == [("last", loader.thread())]
    loader.close()


def test_invalidate_prevents_blocked_result_delivery(qapp) -> None:
    from src.xuanpian.ui.async_preview_loader import BoundedPreviewLoader

    started = threading.Event()
    release = threading.Event()

    def decoder(_path: str, _max_edge: int) -> QImage:
        started.set()
        assert release.wait(3)
        return _image()

    loader = BoundedPreviewLoader(decoder=decoder)
    delivered: list[str] = []
    loader.decoded.connect(lambda photo_id, _image: delivered.append(photo_id))
    loader.request("photo", "/fake/photo.jpg", 512)
    assert started.wait(2)

    loader.invalidate()
    release.set()
    _process_until(qapp, lambda: loader.unfinished_count == 0)

    assert delivered == []
    loader.close()


def test_close_during_blocked_decode_releases_widget_and_reaper_drains(
    qapp,
) -> None:
    from src.xuanpian.ui.async_preview_loader import BoundedPreviewLoader

    baseline = preview_decode_reaper_count()
    started = threading.Event()
    release = threading.Event()
    decoder_finished = threading.Event()

    def decoder(_path: str, _max_edge: int) -> QImage:
        started.set()
        try:
            assert release.wait(3)
            return _image()
        finally:
            decoder_finished.set()

    loader = BoundedPreviewLoader(decoder=decoder)
    delivered: list[str] = []
    loader.decoded.connect(lambda photo_id, _image: delivered.append(photo_id))
    loader.request("photo", "/fake/photo.jpg", 512)
    assert started.wait(2)

    try:
        loader.close()
        loader.deleteLater()
        assert loader.unfinished_count == 0
        assert preview_decode_reaper_count() == baseline + 1
    finally:
        release.set()

    assert decoder_finished.wait(2)
    _process_until(qapp, lambda: preview_decode_reaper_count() == baseline)
    assert delivered == []


def test_blocked_loader_close_exits_subprocess_without_qthread_warning(
    tmp_path: Path,
) -> None:
    code = textwrap.dedent(
        """
        import threading
        import time
        from PyQt6.QtGui import QImage
        from PyQt6.QtWidgets import QApplication
        from src.xuanpian.ui.async_preview_loader import BoundedPreviewLoader
        from src.xuanpian.ui.preview_widget import preview_decode_reaper_count

        app = QApplication([])
        started = threading.Event()
        release = threading.Event()

        def decoder(_path, _max_edge):
            started.set()
            try:
                release.wait(3)
                return QImage(8, 8, QImage.Format.Format_RGB32)
            finally:
                release.set()

        loader = BoundedPreviewLoader(decoder=decoder)
        loader.request("photo", "/fake/photo.jpg", 64)
        while not started.wait(0.001):
            app.processEvents()
        try:
            loader.close()
            loader.deleteLater()
        finally:
            release.set()
        deadline = time.monotonic() + 2
        while preview_decode_reaper_count() and time.monotonic() < deadline:
            app.processEvents()
        app.processEvents()
        raise SystemExit(0 if preview_decode_reaper_count() == 0 else 2)
        """
    )
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PYTHONPATH"] = str(Path.cwd())

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "QThread: Destroyed while thread" not in completed.stderr
