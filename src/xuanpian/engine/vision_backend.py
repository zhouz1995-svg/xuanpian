"""Lazy, offline CPU inference using the two packaged ONNX models."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from threading import RLock
from typing import Any

import numpy as np

from src.xuanpian.engine.model_assets import verified_model_path
from src.xuanpian.engine.onnx_detection import (
    decode_yolox,
    decode_yunet,
    prepare_yolox,
    prepare_yunet,
)


class VisionCapabilityError(RuntimeError):
    """The packaged subject detector could not be loaded or executed."""


class OnnxSubjectBackend:
    def __init__(
        self, *, model_resolver: Callable[[str], Path] = verified_model_path
    ) -> None:
        self._model_resolver = model_resolver
        self._sessions: dict[str, Any] = {}
        self._lock = RLock()

    def _session(self, name: str) -> Any:
        with self._lock:
            if name not in self._sessions:
                import onnxruntime as ort

                options = ort.SessionOptions()
                options.intra_op_num_threads = 2
                options.inter_op_num_threads = 1
                self._sessions[name] = ort.InferenceSession(
                    str(self._model_resolver(name)),
                    sess_options=options,
                    providers=["CPUExecutionProvider"],
                )
            return self._sessions[name]

    def detect_faces(self, bgr: np.ndarray) -> tuple:
        try:
            session = self._session("face_detector")
            tensor, scale = prepare_yunet(bgr)
            outputs = session.run(None, {session.get_inputs()[0].name: tensor})
            named = dict(zip((item.name for item in session.get_outputs()), outputs))
            return decode_yunet(named, bgr.shape[:2], scale)
        except Exception as error:
            raise VisionCapabilityError(f"ONNX 人脸检测失败: {error}") from error

    def detect_objects(self, bgr: np.ndarray) -> tuple:
        try:
            session = self._session("object_detector")
            tensor, scale = prepare_yolox(bgr)
            outputs = session.run(None, {session.get_inputs()[0].name: tensor})
            return decode_yolox(outputs[0], bgr.shape[:2], scale)
        except Exception as error:
            raise VisionCapabilityError(f"ONNX 物体检测失败: {error}") from error

    def close(self) -> None:
        with self._lock:
            self._sessions.clear()
