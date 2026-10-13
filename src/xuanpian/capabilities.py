from dataclasses import dataclass
from importlib.util import find_spec

import cv2


@dataclass(frozen=True)
class CapabilityReport:
    saliency: bool
    raw: bool
    face: bool
    heic: bool


def detect_capabilities() -> CapabilityReport:
    return CapabilityReport(
        saliency=hasattr(cv2, "saliency"),
        raw=find_spec("rawpy") is not None,
        face=find_spec("mediapipe") is not None,
        heic=find_spec("pillow_heif") is not None,
    )
