from dataclasses import dataclass
from importlib.util import find_spec


@dataclass(frozen=True)
class CapabilityReport:
    saliency: bool
    raw: bool
    face: bool
    heic: bool


def detect_capabilities() -> CapabilityReport:
    return CapabilityReport(
        saliency=False,
        raw=find_spec("rawpy") is not None,
        face=find_spec("onnxruntime") is not None,
        heic=find_spec("pillow_heif") is not None,
    )
