import pytest

from src.xuanpian.domain.culling import PhotoMark
from src.xuanpian.services.import_service import (
    PhotoCandidate,
    candidate_to_photo_record,
)


def test_paired_candidate_prefers_jpeg_for_analysis() -> None:
    candidate = PhotoCandidate(
        photo_id="photo-1",
        display_name="DSC_0001.JPG",
        jpeg_path="/shoot/DSC_0001.JPG",
        raw_path="/shoot/DSC_0001.NEF",
    )

    assert candidate.analysis_path == "/shoot/DSC_0001.JPG"
    assert candidate.source_paths == (
        "/shoot/DSC_0001.JPG",
        "/shoot/DSC_0001.NEF",
    )
    assert candidate.is_raw_only is False


def test_raw_only_candidate_is_valid_fallback() -> None:
    candidate = PhotoCandidate(
        photo_id="photo-2",
        display_name="DSC_0002.NEF",
        raw_path="/shoot/DSC_0002.NEF",
    )

    assert candidate.analysis_path == "/shoot/DSC_0002.NEF"
    assert candidate.source_paths == ("/shoot/DSC_0002.NEF",)
    assert candidate.is_raw_only is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"photo_id": "", "display_name": "photo.jpg", "jpeg_path": "/photo.jpg"},
        {"photo_id": "photo-1", "display_name": "", "jpeg_path": "/photo.jpg"},
        {"photo_id": "photo-1", "display_name": "photo.jpg"},
    ],
)
def test_candidate_rejects_missing_identity_or_sources(kwargs) -> None:
    with pytest.raises(ValueError):
        PhotoCandidate(**kwargs)


def test_candidate_records_current_and_original_path(tmp_path) -> None:
    path = tmp_path / "a.jpg"
    path.write_bytes(b"jpeg")
    candidate = PhotoCandidate(
        photo_id="photo-1",
        display_name=path.name,
        jpeg_path=str(path),
    )

    record = candidate_to_photo_record(candidate)

    assert record.mark == PhotoMark.UNPROCESSED.value
    assert record.jpeg is not None
    assert record.jpeg.fingerprint.path == str(path.resolve())
    assert record.jpeg.origin_path == str(path.resolve())
