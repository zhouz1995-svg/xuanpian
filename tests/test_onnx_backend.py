"""Exercise the actual offline CPU sessions and the service boundary."""

import socket

import numpy as np
import pytest

from src.xuanpian.engine.vision_backend import OnnxSubjectBackend, VisionCapabilityError


def test_offline_models_run_with_cpu_and_reopen_after_close(monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("runtime must not access the network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    backend = OnnxSubjectBackend()
    image = np.zeros((96, 128, 3), np.uint8)
    assert backend.detect_faces(image) == ()
    assert backend.detect_objects(image) == ()
    backend.close()
    assert backend.detect_faces(image) == ()


def test_missing_face_asset_does_not_prevent_object_inference():
    from src.xuanpian.engine.model_assets import verified_model_path

    def resolver(name):
        if name == "face_detector":
            raise FileNotFoundError("missing face")
        return verified_model_path(name)

    backend = OnnxSubjectBackend(model_resolver=resolver)
    with pytest.raises(VisionCapabilityError, match="missing face"):
        backend.detect_faces(np.zeros((32, 32, 3), np.uint8))
    assert backend.detect_objects(np.zeros((32, 32, 3), np.uint8)) == ()


def test_invalid_session_outputs_cannot_enter_scene_clear_fallback(monkeypatch):
    from types import SimpleNamespace

    from src.xuanpian.domain.technical_analysis import FocusConclusion
    from src.xuanpian.engine.analysis_context import AnalysisContext
    from src.xuanpian.engine.focus_analyzer import FocusAnalyzer
    from src.xuanpian.engine.subject_locator import SubjectLocator

    backend = OnnxSubjectBackend()
    sessions = {
        name: backend._session(name) for name in ("face_detector", "object_detector")
    }

    def corrupt_session(name):
        session = sessions[name]
        return SimpleNamespace(
            get_inputs=session.get_inputs,
            get_outputs=session.get_outputs,
            run=lambda *args: [
                np.full(output.shape, np.nan, np.float32)
                for output in session.get_outputs()
            ],
        )

    monkeypatch.setattr(backend, "_session", corrupt_session)
    gray = ((np.indices((200, 200)).sum(axis=0) // 4) % 2 * 255).astype(np.uint8)
    bgr = np.repeat(gray[:, :, None], 3, axis=2)
    context = AnalysisContext("fixture.jpg", bgr, gray, bgr, gray, 1.0, 1.0)
    subjects = SubjectLocator(backend).locate(context)
    assert subjects.failed_sources == frozenset({"face", "object"})
    assert len(subjects.diagnostics) == 2
    result = FocusAnalyzer().analyze(context, subjects)
    assert result.conclusion is FocusConclusion.REVIEW
