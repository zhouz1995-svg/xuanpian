#!/usr/bin/env python3
"""Vendor fixed MediaPipe models for fully offline runtime inference."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen


MODEL_URLS = {
    "face_detector": {
        "filename": "face_detector.tflite",
        "url": "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
        "version": "blaze_face_short_range/float16/1",
    },
    "pose_landmarker": {
        "filename": "pose_landmarker.task",
        "url": "https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task",
        "version": "pose_landmarker_lite/float16/1",
    },
    "object_detector": {
        "filename": "object_detector.tflite",
        "url": "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float32/1/efficientdet_lite0.tflite",
        "version": "efficientdet_lite0/float32/1",
    },
}
LICENSE = "Apache-2.0"
MODEL_DIRECTORY = Path(__file__).resolve().parents[1] / "src/xuanpian/resources/models"


def _download(url: str, destination: Path) -> str:
    """Download to a temporary file, atomically replace, and return SHA-256."""
    with urlopen(url, timeout=60) as response:
        with NamedTemporaryFile(dir=destination.parent, delete=False) as temporary:
            while chunk := response.read(1024 * 1024):
                temporary.write(chunk)
            temporary_path = Path(temporary.name)
    try:
        digest = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
        os.replace(temporary_path, destination)
        return digest
    finally:
        temporary_path.unlink(missing_ok=True)


def vendor_models(model_directory: Path = MODEL_DIRECTORY) -> dict[str, object]:
    """Download all fixed models and atomically write their verification manifest."""
    model_directory.mkdir(parents=True, exist_ok=True)
    models: dict[str, object] = {}
    for name, details in MODEL_URLS.items():
        destination = model_directory / details["filename"]
        digest = _download(details["url"], destination)
        models[name] = {
            **details,
            "sha256": digest,
            "license": LICENSE,
        }

    manifest = {"schema_version": 1, "models": models}
    manifest_path = model_directory / "manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary_manifest, manifest_path)
    return manifest


if __name__ == "__main__":
    vendor_models()
