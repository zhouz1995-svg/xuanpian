"""Tests for packaged offline vision model assets."""

from __future__ import annotations

import hashlib

import pytest

from src.xuanpian.engine.model_assets import (
    ModelAssetError,
    verified_model_path,
    verify_model,
)


def test_modified_model_is_rejected(tmp_path) -> None:
    model = tmp_path / "model.tflite"
    model.write_bytes(b"tampered")

    with pytest.raises(ModelAssetError, match="SHA-256"):
        verify_model(model, "0" * 64)


def test_matching_model_is_accepted(tmp_path) -> None:
    model = tmp_path / "model.tflite"
    model.write_bytes(b"verified model")
    expected_sha256 = hashlib.sha256(model.read_bytes()).hexdigest()

    assert verify_model(model, expected_sha256) == model


def test_missing_model_is_rejected(tmp_path) -> None:
    with pytest.raises(ModelAssetError, match="缺失"):
        verify_model(tmp_path / "missing.task", "0" * 64)


@pytest.mark.parametrize(
    ("name", "filename"),
    [
        ("face_detector", "face_detector.tflite"),
        ("pose_landmarker", "pose_landmarker.task"),
        ("object_detector", "object_detector.tflite"),
    ],
)
def test_shared_subject_models_remain_verified(name: str, filename: str) -> None:
    path = verified_model_path(name)

    assert path.name == filename


def test_face_landmarker_is_not_a_verified_model_key() -> None:
    with pytest.raises(ModelAssetError, match="未知模型资源"):
        verified_model_path("face_landmarker")
