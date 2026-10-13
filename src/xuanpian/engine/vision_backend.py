"""MediaPipe Tasks adapter for offline subject detection models."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.xuanpian.engine.model_assets import verified_model_path


class VisionCapabilityError(RuntimeError):
    """Raised when the local MediaPipe Tasks capability cannot be started."""


class MediaPipeSubjectBackend:
    """Lazy MediaPipe Tasks adapter with all model paths resolved offline."""

    def __init__(
        self,
        *,
        mp_module: Any | None = None,
        model_resolver: Callable[[str], Path] = verified_model_path,
    ) -> None:
        self._mp = mp_module
        self._model_resolver = model_resolver
        self._face_detector: Any | None = None
        self._pose_landmarker: Any | None = None
        self._object_detector: Any | None = None

    def detect_faces(self, bgr: np.ndarray) -> tuple[Any, ...]:
        """Run the packaged face detector and return raw Tasks detections."""
        if self._face_detector is None:
            self._face_detector = self._create_face_detector()

        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        image = self._media_pipe().Image(
            image_format=self._media_pipe().ImageFormat.SRGB,
            data=rgb,
        )
        result = self._face_detector.detect(image)
        return tuple(result.detections or ())

    def detect_poses(self, bgr: np.ndarray) -> tuple[Any, ...]:
        """Run the packaged pose landmarker and return landmark groups."""
        if self._pose_landmarker is None:
            self._pose_landmarker = self._create_pose_landmarker()
        result = self._pose_landmarker.detect(self._image_from_bgr(bgr))
        return tuple(result.pose_landmarks or ())

    def detect_objects(self, bgr: np.ndarray) -> tuple[Any, ...]:
        """Run the packaged object detector and return raw Tasks detections."""
        if self._object_detector is None:
            self._object_detector = self._create_object_detector()
        result = self._object_detector.detect(self._image_from_bgr(bgr))
        return tuple(result.detections or ())

    def _create_face_detector(self) -> Any:
        mp = self._media_pipe()
        try:
            options = mp.tasks.vision.FaceDetectorOptions(
                base_options=mp.tasks.BaseOptions(
                    model_asset_path=str(self._model_resolver("face_detector")),
                    delegate=mp.tasks.BaseOptions.Delegate.CPU,
                ),
                min_detection_confidence=0.5,
            )
            return mp.tasks.vision.FaceDetector.create_from_options(options)
        except Exception as exc:
            raise VisionCapabilityError(f"MediaPipe Tasks 人脸模型加载失败: {exc}") from exc

    def _create_pose_landmarker(self) -> Any:
        mp = self._media_pipe()
        try:
            options = mp.tasks.vision.PoseLandmarkerOptions(
                base_options=self._base_options("pose_landmarker")
            )
            return mp.tasks.vision.PoseLandmarker.create_from_options(options)
        except Exception as exc:
            raise VisionCapabilityError(f"MediaPipe Tasks 姿态模型加载失败: {exc}") from exc

    def _create_object_detector(self) -> Any:
        mp = self._media_pipe()
        try:
            options = mp.tasks.vision.ObjectDetectorOptions(
                base_options=self._base_options("object_detector"),
                score_threshold=0.5,
            )
            return mp.tasks.vision.ObjectDetector.create_from_options(options)
        except Exception as exc:
            raise VisionCapabilityError(f"MediaPipe Tasks 物体模型加载失败: {exc}") from exc

    def _base_options(self, name: str) -> Any:
        mp = self._media_pipe()
        return mp.tasks.BaseOptions(
            model_asset_path=str(self._model_resolver(name)),
            delegate=mp.tasks.BaseOptions.Delegate.CPU,
        )

    def _image_from_bgr(self, bgr: np.ndarray) -> Any:
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        mp = self._media_pipe()
        return mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)

    def _media_pipe(self) -> Any:
        if self._mp is not None:
            return self._mp
        try:
            import mediapipe as mp
        except ImportError as exc:
            raise VisionCapabilityError("mediapipe 未安装，无法执行主体检测") from exc
        self._mp = mp
        return mp

    def close(self) -> None:
        """Release loaded task instances when their implementation supports it."""
        for task in (self._face_detector, self._pose_landmarker, self._object_detector):
            if task is not None and hasattr(task, "close"):
                task.close()
        self._face_detector = None
        self._pose_landmarker = None
        self._object_detector = None
