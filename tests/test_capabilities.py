import cv2

from src.xuanpian.capabilities import detect_capabilities


def test_detect_capabilities_reports_saliency(monkeypatch):
    monkeypatch.setattr(cv2, "saliency", object(), raising=False)
    report = detect_capabilities()
    assert report.saliency is True


def test_detect_capabilities_reports_missing_saliency(monkeypatch):
    monkeypatch.delattr(cv2, "saliency", raising=False)
    assert detect_capabilities().saliency is False
