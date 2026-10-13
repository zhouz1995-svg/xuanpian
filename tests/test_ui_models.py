from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from PyQt6.QtGui import QPixmap

from src.xuanpian.domain.analysis import AnalysisStatus
from src.xuanpian.engine.exif_reader import ExifData
from src.xuanpian.services.import_service import PhotoCandidate
from src.xuanpian.ui.image_model import ImageFileModel, scan_images


class TestImageFileModel:
    def test_create_from_path(self) -> None:
        model = ImageFileModel(path="/photos/img001.jpg")
        assert model.path == "/photos/img001.jpg"
        assert model.filename == "img001.jpg"
        assert model.thumbnail is None
        assert model.technical_status is AnalysisStatus.PENDING
        assert model.focus_conclusion == ""
        assert model.exposure_conclusion == ""
        assert model.face_count == 0
        assert model.portrait_assessed is False
        assert model.analysis_failures == {}

    def test_create_with_explicit_filename(self) -> None:
        model = ImageFileModel(path="/photos/img001.jpg", filename="自定义名称")
        assert model.filename == "自定义名称"

    def test_create_with_thumbnail(self, qapp) -> None:
        pixmap = QPixmap(100, 80)
        model = ImageFileModel(
            path="/photos/img001.jpg", filename="img001.jpg", thumbnail=pixmap
        )
        assert model.thumbnail is pixmap

    def test_model_applies_exif_and_normalizes_rotation(self) -> None:
        model = ImageFileModel("/fake/photo.jpg", rotation=450)

        model.apply_exif(
            ExifData(path=model.path, camera_model="D850", iso="400")
        )

        assert model.rotation == 90
        assert model.camera_model == "D850"
        assert model.iso == "400"
        assert model.exif_loaded is True
        assert model.exif_error == ""

    def test_model_preserves_exif_diagnostic(self) -> None:
        model = ImageFileModel("/fake/broken.jpg")

        model.apply_exif(
            ExifData(path=model.path, error="broken.jpg: decode failed")
        )

        assert model.exif_loaded is True
        assert model.exif_error == "broken.jpg: decode failed"

    def test_model_upsert_keeps_identity_and_switches_to_jpeg(self) -> None:
        model = ImageFileModel.from_candidate(
            PhotoCandidate(
                "photo-1",
                "DSC_1.NEF",
                raw_path="/shoot/DSC_1.NEF",
            )
        )
        model.mark = "入选"

        model.upsert_candidate(
            PhotoCandidate(
                "photo-1",
                "DSC_1.JPG",
                jpeg_path="/shoot/DSC_1.JPG",
                raw_path="/shoot/DSC_1.NEF",
            )
        )

        assert model.photo_id == "photo-1"
        assert model.path == "/shoot/DSC_1.JPG"
        assert model.analysis_path == "/shoot/DSC_1.JPG"
        assert model.source_paths == (
            "/shoot/DSC_1.JPG",
            "/shoot/DSC_1.NEF",
        )
        assert model.mark == "入选"

    def test_model_rejects_upsert_for_different_photo(self) -> None:
        model = ImageFileModel.from_candidate(
            PhotoCandidate("photo-1", "one.jpg", jpeg_path="/one.jpg")
        )

        with pytest.raises(ValueError, match="photo_id"):
            model.upsert_candidate(
                PhotoCandidate("photo-2", "two.jpg", jpeg_path="/two.jpg")
            )


class TestScanImages:
    def test_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_images(tmp)
            assert result == []

    def test_nonexistent_directory(self) -> None:
        result = scan_images("/nonexistent/path/foo")
        assert result == []

    def test_scans_supported_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.jpg", "b.jpeg", "c.png", "d.bmp", "e.tiff", "f.webp"):
                (Path(tmp) / name).touch()
            (Path(tmp) / "skip.txt").touch()

            result = scan_images(tmp)
            assert len(result) == 6
            filenames = [Path(p).name for p in result]
            for name in ("a.jpg", "b.jpeg", "c.png", "d.bmp", "e.tiff", "f.webp"):
                assert name in filenames
            assert "skip.txt" not in filenames

    def test_recursive_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "sub").mkdir()
            (Path(tmp) / "a.jpg").touch()
            (Path(tmp) / "sub" / "b.jpg").touch()

            result = scan_images(tmp)
            assert len(result) == 2

    def test_case_insensitive_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "A.JPG").touch()
            (Path(tmp) / "B.PNG").touch()

            result = scan_images(tmp)
            assert len(result) == 2

    def test_sorted_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "z.jpg").touch()
            (Path(tmp) / "a.jpg").touch()

            result = scan_images(tmp)
            assert Path(result[0]).name == "a.jpg"
            assert Path(result[1]).name == "z.jpg"
