"""Filesystem fingerprint and import-record conversion tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.xuanpian.domain.photo import (
    AssetKind,
    FileFingerprint,
    PhotoAssetRecord,
    PhotoRecord,
)
from src.xuanpian.services.import_service import PhotoCandidate


def test_fingerprint_uses_resolved_path_size_and_nanosecond_mtime(
    tmp_path: Path,
) -> None:
    from src.xuanpian.services.fingerprints import fingerprint

    target = tmp_path / "source.jpg"
    target.write_bytes(b"jpeg-data")
    requested_mtime_ns = 1_700_000_000_123_456_789
    os.utime(target, ns=(requested_mtime_ns, requested_mtime_ns))
    expected_stat = target.stat()

    assert fingerprint(target) == FileFingerprint(
        path=str(target.resolve(strict=True)),
        size=expected_stat.st_size,
        mtime_ns=expected_stat.st_mtime_ns,
    )


def test_fingerprint_rejects_missing_paths_and_directories(
    tmp_path: Path,
) -> None:
    from src.xuanpian.services.fingerprints import fingerprint

    with pytest.raises(FileNotFoundError):
        fingerprint(tmp_path / "gone.jpg")

    with pytest.raises(ValueError, match="regular file"):
        fingerprint(tmp_path)


def test_candidate_conversion_fingerprints_jpeg_and_raw_independently(
    tmp_path: Path,
) -> None:
    from src.xuanpian.services.import_service import candidate_to_photo_record

    jpeg = tmp_path / "DSC_0001.JPG"
    raw = tmp_path / "DSC_0001.NEF"
    jpeg.write_bytes(b"jpeg")
    raw.write_bytes(b"raw-data")
    candidate = PhotoCandidate(
        photo_id="photo-1",
        display_name=jpeg.name,
        jpeg_path=str(jpeg),
        raw_path=str(raw),
    )

    assert candidate_to_photo_record(candidate, mark="imported") == PhotoRecord(
        id="photo-1",
        filename=jpeg.name,
        mark="imported",
        jpeg=PhotoAssetRecord(
            kind=AssetKind.JPEG,
            fingerprint=FileFingerprint(
                path=str(jpeg.resolve()),
                size=jpeg.stat().st_size,
                mtime_ns=jpeg.stat().st_mtime_ns,
            ),
            origin_path=str(jpeg.resolve()),
        ),
        raw=PhotoAssetRecord(
            kind=AssetKind.RAW,
            fingerprint=FileFingerprint(
                path=str(raw.resolve()),
                size=raw.stat().st_size,
                mtime_ns=raw.stat().st_mtime_ns,
            ),
            origin_path=str(raw.resolve()),
        ),
    )


def test_candidate_conversion_treats_only_disappearing_assets_as_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.xuanpian.services import import_service

    jpeg = tmp_path / "DSC_0002.JPG"
    raw = tmp_path / "DSC_0002.NEF"
    jpeg.touch()
    raw.touch()
    candidate = PhotoCandidate(
        photo_id="photo-2",
        display_name=jpeg.name,
        jpeg_path=str(jpeg),
        raw_path=str(raw),
    )
    real_fingerprint = import_service.fingerprint

    def disappear_raw(path: Path) -> FileFingerprint:
        if path == raw:
            raise FileNotFoundError(path)
        return real_fingerprint(path)

    monkeypatch.setattr(import_service, "fingerprint", disappear_raw)
    record = import_service.candidate_to_photo_record(candidate)

    assert record.jpeg is not None
    assert record.raw is None

    def deny_raw(path: Path) -> FileFingerprint:
        if path == raw:
            raise PermissionError(path)
        return real_fingerprint(path)

    monkeypatch.setattr(import_service, "fingerprint", deny_raw)
    with pytest.raises(PermissionError):
        import_service.candidate_to_photo_record(candidate)


def test_candidate_conversion_keeps_identity_when_all_assets_disappear(
    tmp_path: Path,
) -> None:
    from src.xuanpian.services.import_service import candidate_to_photo_record

    candidate = PhotoCandidate(
        photo_id="photo-3",
        display_name="DSC_0003.JPG",
        jpeg_path=str(tmp_path / "gone.JPG"),
        raw_path=str(tmp_path / "gone.NEF"),
    )

    assert candidate_to_photo_record(candidate) == PhotoRecord(
        id="photo-3",
        filename="DSC_0003.JPG",
        mark="未处理",
        jpeg=None,
        raw=None,
    )
