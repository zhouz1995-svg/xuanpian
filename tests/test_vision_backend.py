"""Tests for the MediaPipe Tasks-only vision adapter."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np

from src.xuanpian.engine.vision_backend import MediaPipeSubjectBackend


class _FakeTask:
    created_options: object | None = None

    @classmethod
    def create_from_options(cls, options):
        cls.created_options = options
        return cls()

    def detect(self, image):
        return SimpleNamespace(detections=())


class _FakePoseTask(_FakeTask):
    def detect(self, image):
        return SimpleNamespace(pose_landmarks=())


def _fake_mediapipe() -> SimpleNamespace:
    class _BaseOptions:
        class Delegate:
            CPU = "cpu"

        def __init__(self, *, model_asset_path: str, delegate: str) -> None:
            self.model_asset_path = model_asset_path
            self.delegate = delegate

    class _FaceDetectorOptions:
        def __init__(self, *, base_options, min_detection_confidence: float) -> None:
            self.base_options = base_options
            self.min_detection_confidence = min_detection_confidence

    class _PoseLandmarkerOptions:
        def __init__(self, *, base_options) -> None:
            self.base_options = base_options

    class _ObjectDetectorOptions:
        def __init__(self, *, base_options, score_threshold: float) -> None:
            self.base_options = base_options
            self.score_threshold = score_threshold

    class _Image:
        def __init__(self, *, image_format, data) -> None:
            self.image_format = image_format
            self.data = data

    return SimpleNamespace(
        Image=_Image,
        ImageFormat=SimpleNamespace(SRGB="srgb"),
        tasks=SimpleNamespace(
            BaseOptions=_BaseOptions,
            vision=SimpleNamespace(
                FaceDetector=_FakeTask,
                FaceDetectorOptions=_FaceDetectorOptions,
                PoseLandmarker=_FakePoseTask,
                PoseLandmarkerOptions=_PoseLandmarkerOptions,
                ObjectDetector=_FakeTask,
                ObjectDetectorOptions=_ObjectDetectorOptions,
            ),
        ),
    )


def test_face_backend_uses_tasks_api_without_solutions() -> None:
    fake_mediapipe = _fake_mediapipe()
    assert not hasattr(fake_mediapipe, "solutions")

    backend = MediaPipeSubjectBackend(
        mp_module=fake_mediapipe,
        model_resolver=lambda name: Path(f"/models/{name}.tflite"),
    )

    assert backend.detect_faces(np.zeros((32, 32, 3), np.uint8)) == ()
    assert _FakeTask.created_options.base_options.model_asset_path.endswith(
        "face_detector.tflite"
    )
    assert _FakeTask.created_options.base_options.delegate == "cpu"


def test_backend_resolves_all_task_models_from_the_offline_resolver() -> None:
    fake_mediapipe = _fake_mediapipe()
    requested_models: list[str] = []
    backend = MediaPipeSubjectBackend(
        mp_module=fake_mediapipe,
        model_resolver=lambda name: requested_models.append(name)
        or Path(f"/models/{name}.task"),
    )
    image = np.zeros((32, 32, 3), np.uint8)

    assert backend.detect_faces(image) == ()
    assert backend.detect_poses(image) == ()
    assert backend.detect_objects(image) == ()
    assert requested_models == ["face_detector", "pose_landmarker", "object_detector"]
