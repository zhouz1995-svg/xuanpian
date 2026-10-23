import numpy as np
import pytest
from PIL import Image

from src.xuanpian.engine.image_ops import bgr_to_gray, laplacian, sobel
from src.xuanpian.engine.image_reader import read_image_color


def test_bt601_bgr_primary_colors():
    values = np.array([[[255, 0, 0], [0, 255, 0], [0, 0, 255]]], np.uint8)
    assert bgr_to_gray(values).tolist() == [[29, 150, 76]]


def test_signed_laplacian_impulse_and_reflect_border():
    gray = np.zeros((5, 5), np.uint8)
    gray[2, 2] = 100
    result = laplacian(gray)
    assert result[2, 2] == -400
    assert result[2, 1] == 100
    assert result[1, 1] == 0
    assert laplacian(np.full((8, 8), 255, np.uint8)).max() == 0


def test_sobel_ramp_has_expected_energy_and_reflect_border():
    ramp = np.tile(np.arange(5, dtype=np.uint8), (5, 1))
    assert sobel(ramp, 1, 0)[2].tolist() == [0, 8, 8, 8, 0]
    assert sobel(ramp, 0, 1).max() == 0


def test_reader_orients_and_preserves_bgr_on_unicode_path(tmp_path):
    path = tmp_path / "照片.png"
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (4, 2), (250, 30, 10)).save(path, exif=exif)
    result = read_image_color(str(path))
    assert result.shape == (4, 2, 3)
    assert result[0, 0].tolist() == [10, 30, 250]


@pytest.mark.parametrize("suffix", [".tiff", ".png"])
def test_16bit_grayscale_preserves_full_range_for_analysis(tmp_path, suffix):
    path = tmp_path / ("levels" + suffix)
    values = np.array([[0, 16384, 32768, 49152, 65535]], np.uint16)
    Image.fromarray(values).save(path)
    result = read_image_color(str(path))
    assert result[0, :, 0].tolist() == [0, 64, 128, 192, 255]
    assert result[0, :, 1].tolist() == [0, 64, 128, 192, 255]
