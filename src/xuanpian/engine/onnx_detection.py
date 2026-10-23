"""Pre/postprocessing for the pinned YuNet and YOLOX-Nano release models.

The 0.1.1rc0 release assets use raw BGR (see the release's breaking changes).
YuNet uses unnormalized BGR and stride-8/16/32 heads. No OpenCV is required.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from src.xuanpian.domain.technical_analysis import Keypoint, Rect, SubjectCandidate

COCO_CLASSES = (
    "person",
    "bicycle",
    "car",
    "motorcycle",
    "airplane",
    "bus",
    "train",
    "truck",
    "boat",
    "traffic light",
    "fire hydrant",
    "stop sign",
    "parking meter",
    "bench",
    "bird",
    "cat",
    "dog",
    "horse",
    "sheep",
    "cow",
    "elephant",
    "bear",
    "zebra",
    "giraffe",
    "backpack",
    "umbrella",
    "handbag",
    "tie",
    "suitcase",
    "frisbee",
    "skis",
    "snowboard",
    "sports ball",
    "kite",
    "baseball bat",
    "baseball glove",
    "skateboard",
    "surfboard",
    "tennis racket",
    "bottle",
    "wine glass",
    "cup",
    "fork",
    "knife",
    "spoon",
    "bowl",
    "banana",
    "apple",
    "sandwich",
    "orange",
    "broccoli",
    "carrot",
    "hot dog",
    "pizza",
    "donut",
    "cake",
    "chair",
    "couch",
    "potted plant",
    "bed",
    "dining table",
    "toilet",
    "tv",
    "laptop",
    "mouse",
    "remote",
    "keyboard",
    "cell phone",
    "microwave",
    "oven",
    "toaster",
    "sink",
    "refrigerator",
    "book",
    "clock",
    "vase",
    "scissors",
    "teddy bear",
    "hair drier",
    "toothbrush",
)


def _letterbox(bgr: np.ndarray, size: int, fill: int) -> tuple[np.ndarray, float]:
    if bgr.ndim != 3 or bgr.shape[2] != 3 or bgr.size == 0 or bgr.dtype != np.uint8:
        raise ValueError("Expected a nonempty uint8 BGR image")
    height, width = bgr.shape[:2]
    scale = min(size / height, size / width)
    resized = Image.fromarray(bgr).resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.BILINEAR,
    )
    padded = np.full((size, size, 3), fill, np.float32)
    array = np.asarray(resized)
    padded[: array.shape[0], : array.shape[1]] = array
    return padded, scale


def prepare_yolox(bgr: np.ndarray, size: int = 416) -> tuple[np.ndarray, float]:
    padded, scale = _letterbox(bgr, size, 114)
    return np.ascontiguousarray(padded.transpose(2, 0, 1)[None]), scale


def prepare_yunet(bgr: np.ndarray) -> tuple[np.ndarray, float]:
    padded, scale = _letterbox(bgr, 640, 0)
    return np.ascontiguousarray(padded.transpose(2, 0, 1)[None]), scale


def _rect(
    x0: float, y0: float, x1: float, y1: float, shape: tuple[int, int], scale: float
) -> Rect | None:
    height, width = shape
    left, right = np.clip([x0 / scale / width, x1 / scale / width], 0, 1)
    top, bottom = np.clip([y0 / scale / height, y1 / scale / height], 0, 1)
    if right <= left or bottom <= top:
        return None
    return Rect(float(left), float(top), float(right - left), float(bottom - top))


def _iou(first: Rect, second: Rect) -> float:
    intersection = max(
        0.0,
        min(first.x + first.width, second.x + second.width) - max(first.x, second.x),
    ) * max(
        0.0,
        min(first.y + first.height, second.y + second.height) - max(first.y, second.y),
    )
    union = first.width * first.height + second.width * second.height - intersection
    return intersection / union if union else 0.0


def _nms(
    candidates: list[SubjectCandidate], threshold: float
) -> tuple[SubjectCandidate, ...]:
    kept: list[SubjectCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True)[
        :1000
    ]:
        if not any(
            other.kind == candidate.kind
            and _iou(other.bounds, candidate.bounds) > threshold
            for other in kept
        ):
            kept.append(candidate)
    return tuple(kept)


def decode_yolox(
    output: np.ndarray, shape: tuple[int, int], scale: float
) -> tuple[SubjectCandidate, ...]:
    if output.shape != (1, 3549, 85):
        raise ValueError("Invalid YOLOX output shape")
    rows = output[0]
    grids, strides = [], []
    for stride in (8, 16, 32):
        y, x = np.mgrid[: 416 // stride, : 416 // stride]
        grids.append(np.stack((x, y), axis=-1).reshape(-1, 2))
        strides.extend([stride] * x.size)
    grid = np.concatenate(grids)
    stride_array = np.asarray(strides)[:, None]
    scores = rows[:, 4:5] * rows[:, 5:]
    classes = scores.argmax(axis=1)
    confidence = scores[np.arange(len(rows)), classes]
    valid = np.isfinite(rows).all(axis=1) & (confidence >= 0.5) & (confidence <= 1)
    valid &= (rows[:, 4:] >= 0).all(axis=1) & (rows[:, 4:] <= 1).all(axis=1)
    candidates = []
    for index in np.flatnonzero(valid):
        row = rows[index]
        center = (row[:2] + grid[index]) * stride_array[index]
        size = np.exp(np.clip(row[2:4], -20, 20)) * stride_array[index]
        bounds = _rect(*(center - size / 2), *(center + size / 2), shape, scale)
        if bounds is not None:
            candidates.append(
                SubjectCandidate(
                    COCO_CLASSES[classes[index]],
                    bounds,
                    float(confidence[index]),
                    bounds.width * bounds.height,
                )
            )
    return _nms(candidates, 0.45)


def decode_yunet(
    outputs: dict[str, np.ndarray], shape: tuple[int, int], scale: float
) -> tuple[SubjectCandidate, ...]:
    candidates = []
    for stride in (8, 16, 32):
        side = 640 // stride
        arrays = {}
        for name, dims in [("cls", 1), ("obj", 1), ("bbox", 4), ("kps", 10)]:
            array = outputs.get(f"{name}_{stride}")
            if array is None or array.shape != (1, side * side, dims):
                raise ValueError("Invalid YuNet output shape")
            arrays[name] = array[0]
        confidence = np.sqrt(
            np.clip(arrays["cls"][:, 0], 0, 1) * np.clip(arrays["obj"][:, 0], 0, 1)
        )
        valid = (confidence >= 0.7) & np.isfinite(confidence)
        valid &= np.isfinite(arrays["bbox"]).all(axis=1) & np.isfinite(
            arrays["kps"]
        ).all(axis=1)
        valid &= np.isfinite(arrays["cls"][:, 0]) & np.isfinite(arrays["obj"][:, 0])
        for index in np.flatnonzero(valid):
            grid = np.array([index % side, index // side])
            box = arrays["bbox"][index]
            center = (grid + box[:2]) * stride
            size = np.exp(np.clip(box[2:], -20, 20)) * stride
            bounds = _rect(*(center - size / 2), *(center + size / 2), shape, scale)
            if bounds is None:
                continue
            points = (arrays["kps"][index].reshape(5, 2) + grid) * stride / scale
            points /= np.array([shape[1], shape[0]])
            points = np.clip(points, 0, 1)
            score = float(confidence[index])
            candidates.append(
                SubjectCandidate(
                    "face",
                    bounds,
                    score,
                    bounds.width * bounds.height,
                    tuple(Keypoint(float(x), float(y), score) for x, y in points),
                )
            )
    return _nms(candidates, 0.3)
