from src.xuanpian import capabilities

from src.xuanpian.capabilities import detect_capabilities


def test_detect_capabilities_reports_onnx_face_support(monkeypatch):
    monkeypatch.setattr(capabilities, "find_spec", lambda name: object() if name == "onnxruntime" else None)
    report = detect_capabilities()
    assert report.face is True
    assert report.raw is False
    assert report.saliency is False


def test_detect_capabilities_reports_missing_runtime(monkeypatch):
    monkeypatch.setattr(capabilities, "find_spec", lambda name: None)
    assert detect_capabilities().face is False
