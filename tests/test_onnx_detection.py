import numpy as np
import pytest

from src.xuanpian.engine.onnx_detection import (
    decode_yolox,
    decode_yunet,
    prepare_yolox,
    prepare_yunet,
)


def test_yolox_release_uses_raw_bgr_and_bottom_padding():
    bgr = np.full((208, 416, 3), (0, 0, 255), np.uint8)
    tensor, scale = prepare_yolox(bgr)
    assert tensor.shape == (1, 3, 416, 416)
    assert tensor.dtype == np.float32
    assert scale == 1.0
    assert tensor[0, :, 0, 0].tolist() == [0, 0, 255]
    assert tensor[0, :, 300, 0].tolist() == [114, 114, 114]


def test_yolox_grid_decode_coordinates_and_class_aware_nms():
    rows = np.zeros((1, 3549, 85), np.float32)
    # First grid cell, stride 8; cx=208 cy=104 width=104 height=104.
    rows[0, 0, :5] = [26, 13, np.log(13), np.log(13), 0.9]
    rows[0, 0, 5] = 0.9
    rows[0, 1, :5] = [25, 13, np.log(13), np.log(13), 0.8]
    rows[0, 1, 5] = 0.9
    subjects = decode_yolox(rows, (208, 416), 1.0)
    assert len(subjects) == 1
    person = subjects[0]
    assert person.kind == "person"
    assert person.confidence == pytest.approx(0.81)
    assert (
        person.bounds.x,
        person.bounds.y,
        person.bounds.width,
        person.bounds.height,
    ) == pytest.approx((0.375, 0.25, 0.25, 0.5))


def test_invalid_yolox_output_is_rejected_instead_of_reported_as_empty():
    with pytest.raises(ValueError, match="YOLOX"):
        decode_yolox(np.zeros((1, 4, 6)), (100, 100), 1)


def test_yunet_preserves_bgr_without_normalization():
    tensor, scale = prepare_yunet(np.full((320, 320, 3), (1, 2, 3), np.uint8))
    assert tensor.shape == (1, 3, 640, 640)
    assert scale == 2
    assert tensor[0, :, 0, 0].tolist() == [1, 2, 3]


def test_yunet_decodes_box_landmarks_and_suppresses_duplicates():
    outputs = {}
    for stride in (8, 16, 32):
        n = (640 // stride) ** 2
        for name, dims in [("cls", 1), ("obj", 1), ("bbox", 4), ("kps", 10)]:
            outputs[f"{name}_{stride}"] = np.zeros((1, n, dims), np.float32)
    outputs["cls_8"][0, 0, 0] = 0.9
    outputs["obj_8"][0, 0, 0] = 0.9
    outputs["bbox_8"][0, 0] = [20, 20, np.log(20), np.log(20)]
    outputs["kps_8"][0, 0] = [15, 16, 25, 16, 20, 20, 16, 25, 24, 25]
    subjects = decode_yunet(outputs, (320, 320), 2)
    assert len(subjects) == 1
    face = subjects[0]
    assert face.kind == "face"
    assert face.bounds.x == pytest.approx(0.125)
    assert face.bounds.width == pytest.approx(0.25)
    assert len(face.keypoints) == 5
    assert face.keypoints[0].x == pytest.approx(0.1875)
    assert face.keypoints[2].y == pytest.approx(0.25)


def test_yunet_malformed_output_is_a_capability_error():
    with pytest.raises(ValueError, match="YuNet"):
        decode_yunet({}, (320, 320), 2)


def test_nonfinite_yolox_rows_cannot_become_subjects():
    rows = np.zeros((1, 3549, 85), np.float32)
    rows[0, 0] = np.nan
    with pytest.raises(ValueError, match="YOLOX"):
        decode_yolox(rows, (208, 416), 1)


def test_negative_model_scores_cannot_multiply_into_a_valid_detection():
    rows = np.zeros((1, 3549, 85), np.float32)
    rows[0, 0, :6] = [10, 10, 2, 2, -1, -1]
    with pytest.raises(ValueError, match="YOLOX"):
        decode_yolox(rows, (208, 416), 1)


def test_float_sigmoid_roundoff_in_unused_class_does_not_drop_real_person():
    rows = np.zeros((1, 3549, 85), np.float32)
    rows[0, 0, :6] = [26, 13, np.log(13), np.log(13), 0.9, 0.9]
    rows[0, 0, 6] = -5.960464477539063e-8
    subjects = decode_yolox(rows, (208, 416), 1)
    assert len(subjects) == 1
    assert subjects[0].kind == "person"


def test_yunet_nonfinite_class_score_is_not_clipped_into_a_face():
    outputs = {}
    for stride in (8, 16, 32):
        n = (640 // stride) ** 2
        for name, dims in [("cls", 1), ("obj", 1), ("bbox", 4), ("kps", 10)]:
            outputs[f"{name}_{stride}"] = np.zeros((1, n, dims), np.float32)
    outputs["cls_8"][0, 0, 0] = np.inf
    outputs["obj_8"][0, 0, 0] = 1
    outputs["bbox_8"][0, 0] = [20, 20, 2, 2]
    with pytest.raises(ValueError, match="YuNet"):
        decode_yunet(outputs, (320, 320), 2)
