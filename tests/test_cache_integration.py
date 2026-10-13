from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage

from src.xuanpian.cache.thumbnail_cache import (
    ThumbnailCache,
    ThumbnailCacheError,
)
from src.xuanpian.engine.exif_reader import ExifData
from src.xuanpian.tasks.io_coordinator import (
    IoCoordinator,
    MetadataRequest,
    MetadataResult,
    load_metadata,
)


def _exif(path: str) -> ExifData:
    return ExifData(path, camera_model=Path(path).name)


def test_metadata_cache_skips_decode_until_source_fingerprint_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"first")
    cache = ThumbnailCache(tmp_path / "cache", memory_limit_bytes=1_000_000)
    decode_calls: list[tuple[str, tuple[int, int]]] = []
    exif_calls: list[str] = []

    def decode(path: str, size: tuple[int, int]) -> QImage:
        decode_calls.append((path, size))
        image = QImage(size[0], size[1], QImage.Format.Format_RGB888)
        image.fill(Qt.GlobalColor.red)
        return image

    def read_metadata(path: str) -> ExifData:
        exif_calls.append(path)
        return _exif(path)

    request = MetadataRequest(
        project_id=uuid4(),
        job_id=uuid4(),
        photo_id="photo-1",
        path=str(source.resolve()),
        thumbnail_size=(80, 60),
    )

    first = load_metadata(
        request,
        thumbnail_cache=cache,
        thumbnail_decoder=decode,
        exif_loader=read_metadata,
    )
    second = load_metadata(
        request,
        thumbnail_cache=cache,
        thumbnail_decoder=decode,
        exif_loader=read_metadata,
    )

    assert first.qimage is not None
    assert second.qimage is not None
    assert decode_calls == [(str(source.resolve()), (80, 60))]
    assert exif_calls == [str(source.resolve()), str(source.resolve())]
    assert second.exif.camera_model == "photo.jpg"

    source.write_bytes(b"second version")
    changed = load_metadata(
        request,
        thumbnail_cache=cache,
        thumbnail_decoder=decode,
        exif_loader=read_metadata,
    )

    assert changed.qimage is not None
    assert decode_calls == [
        (str(source.resolve()), (80, 60)),
        (str(source.resolve()), (80, 60)),
    ]
    assert len(exif_calls) == 3


def test_io_coordinator_uses_injected_cache_at_worker_boundary(
    tmp_path: Path,
) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo")
    cache = ThumbnailCache(tmp_path / "cache", memory_limit_bytes=1_000_000)
    decode_calls: list[str] = []

    def decode(path: str, size: tuple[int, int]) -> QImage:
        decode_calls.append(path)
        image = QImage(size[0], size[1], QImage.Format.Format_RGB888)
        image.fill(Qt.GlobalColor.blue)
        return image

    coordinator = IoCoordinator(
        max_workers=1,
        thumbnail_cache=cache,
        thumbnail_decoder=decode,
        exif_loader=_exif,
    )
    project_id = uuid4()
    results: list[MetadataResult] = []
    try:
        for _ in range(2):
            request = MetadataRequest(
                project_id=project_id,
                job_id=uuid4(),
                photo_id="photo-1",
                path=str(source.resolve()),
                thumbnail_size=(80, 60),
            )
            handle = coordinator.submit_metadata(
                (request,),
                lambda event: (
                    results.append(event)
                    if isinstance(event, MetadataResult)
                    else None
                ),
            )
            assert handle.wait(2)
    finally:
        coordinator.shutdown()

    assert len(results) == 2
    assert decode_calls == [str(source.resolve())]


def test_cache_failure_falls_back_to_decoder_and_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo")
    decode_calls: list[str] = []

    class BrokenCache:
        def get_or_create(self, key, factory):
            raise ThumbnailCacheError("cache unavailable")

    def decode(path: str, size: tuple[int, int]) -> QImage:
        decode_calls.append(path)
        image = QImage(size[0], size[1], QImage.Format.Format_RGB888)
        image.fill(Qt.GlobalColor.green)
        return image

    request = MetadataRequest(
        project_id=uuid4(),
        job_id=uuid4(),
        photo_id="photo-1",
        path=str(source.resolve()),
        thumbnail_size=(80, 60),
    )

    result = load_metadata(
        request,
        thumbnail_cache=BrokenCache(),  # type: ignore[arg-type]
        thumbnail_decoder=decode,
        exif_loader=_exif,
    )

    assert result.qimage is not None
    assert not result.qimage.isNull()
    assert result.error == ""
    assert decode_calls == [str(source.resolve())]
    assert "cache unavailable" in caplog.text


def test_cache_write_failure_returns_generated_image_without_second_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo")
    cache = ThumbnailCache(tmp_path / "cache", memory_limit_bytes=0)
    decode_calls = 0

    def decode(path: str, size: tuple[int, int]) -> QImage:
        nonlocal decode_calls
        decode_calls += 1
        image = QImage(size[0], size[1], QImage.Format.Format_RGB888)
        image.fill(Qt.GlobalColor.blue)
        return image

    def fail_put(key, image) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(cache, "put", fail_put)
    request = MetadataRequest(
        project_id=uuid4(),
        job_id=uuid4(),
        photo_id="photo-1",
        path=str(source.resolve()),
        thumbnail_size=(80, 60),
    )

    result = load_metadata(
        request,
        thumbnail_cache=cache,
        thumbnail_decoder=decode,
        exif_loader=_exif,
    )

    assert result.qimage is not None
    assert not result.qimage.isNull()
    assert result.error == ""
    assert decode_calls == 1
    assert "disk full" in caplog.text


@pytest.mark.parametrize("empty_kind", ["none", "null"])
def test_empty_decode_result_is_not_retried_as_cache_failure(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    empty_kind: str,
) -> None:
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo")
    cache = ThumbnailCache(tmp_path / "cache", memory_limit_bytes=0)
    decode_calls = 0

    def decode(path: str, size: tuple[int, int]) -> QImage | None:
        nonlocal decode_calls
        decode_calls += 1
        return None if empty_kind == "none" else QImage()

    request = MetadataRequest(
        project_id=uuid4(),
        job_id=uuid4(),
        photo_id="photo-1",
        path=str(source.resolve()),
        thumbnail_size=(80, 60),
    )

    result = load_metadata(
        request,
        thumbnail_cache=cache,
        thumbnail_decoder=decode,
        exif_loader=_exif,
    )

    assert result.qimage is None or result.qimage.isNull()
    assert "thumbnail decode returned no image" in result.error
    assert decode_calls == 1
    assert "Thumbnail cache unavailable" not in caplog.text
