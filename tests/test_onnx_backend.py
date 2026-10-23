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
