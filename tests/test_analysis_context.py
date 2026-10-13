from __future__ import annotations

import json
import subprocess

import numpy as np
from PIL import Image

from src.xuanpian.domain.technical_analysis import Rect
from src.xuanpian.engine import af_metadata, analysis_context


def test_context_decodes_once_and_maps_locator_rect(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        analysis_context,
        "read_image_color",
        lambda path: calls.append(path) or np.zeros((2000, 4000, 3), np.uint8),
    )

    context = analysis_context.AnalysisContext.from_path(
        "a.jpg", locate_long_edge=1000
    )

    assert calls == ["a.jpg"]
    assert context.locate_bgr.shape[:2] == (500, 1000)
    assert context.to_source_rect(Rect(.1, .1, .2, .2)) == Rect(.1, .1, .2, .2)


def test_manually_constructed_context_defaults_to_an_unavailable_af_hint() -> None:
    image = np.zeros((2, 3, 3), np.uint8)

    context = analysis_context.AnalysisContext(
        path="manual.jpg",
        bgr=image,
        gray=np.zeros((2, 3), np.uint8),
        locate_bgr=image,
        locate_gray=np.zeros((2, 3), np.uint8),
        scale_x=1.0,
        scale_y=1.0,
    )

    assert context.af_hint.bounds is None
    assert context.af_hint.reason == "unavailable"


def test_context_maps_real_orientation_8_jpeg_af_region(
    monkeypatch, tmp_path
) -> None:
    photo = tmp_path / "oriented.jpg"
    exif = Image.Exif()
    exif[274] = 8
    Image.new("RGB", (400, 200), "white").save(photo, exif=exif)
    metadata = {
        "FileType": "JPEG",
        "Make": "NIKON CORPORATION",
        "Model": "NIKON Z50_2",
        "AFInfo2Version": "0402",
        "AFCoordinatesAvailable": 1,
        "AFImageWidth": 400,
        "AFImageHeight": 200,
        "AFAreaXPosition": 100,
        "AFAreaYPosition": 60,
        "AFAreaWidth": 40,
        "AFAreaHeight": 20,
        "AFPointsUsed": "00 00 00 00",
        "Orientation": 8,
    }
    monkeypatch.setattr(af_metadata.shutil, "which", lambda name: "/test/exiftool")
    monkeypatch.setattr(
        af_metadata.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps([metadata]), ""
        ),
    )

    context = analysis_context.AnalysisContext.from_path(str(photo))

    assert context.bgr.shape[:2] == (400, 200)
    assert context.af_hint.bounds == Rect(.25, .7, .1, .1)
    assert context.af_hint.reason == "available"


def test_context_rejects_non_jpeg_content_with_a_jpg_suffix(
    monkeypatch, tmp_path
) -> None:
    photo = tmp_path / "not-really-jpeg.jpg"
    Image.new("RGB", (400, 200), "white").save(photo, format="TIFF")
    metadata = {
        "FileType": "TIFF",
        "Make": "NIKON CORPORATION",
        "Model": "NIKON Z50_2",
        "AFInfo2Version": "0402",
        "AFCoordinatesAvailable": 1,
        "AFImageWidth": 400,
        "AFImageHeight": 200,
        "AFAreaXPosition": 100,
        "AFAreaYPosition": 60,
        "AFAreaWidth": 40,
        "AFAreaHeight": 20,
        "AFPointsUsed": "00 00 00 00",
        "Orientation": 1,
    }
    monkeypatch.setattr(af_metadata.shutil, "which", lambda name: "/test/exiftool")
    monkeypatch.setattr(
        af_metadata.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, json.dumps([metadata]), ""
        ),
    )

    context = analysis_context.AnalysisContext.from_path(str(photo))

    assert context.bgr.shape[:2] == (200, 400)
    assert context.af_hint.bounds is None
    assert context.af_hint.reason == "unsupported_file_type"
