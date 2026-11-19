from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from app.services.measurements import (
    BLACK_CLIP_LEVEL,
    MEASUREMENT_MAX_SIZE,
    describe_for_prompt,
    measure_file,
    measure_image,
)


def _gradient(width: int = 256, height: int = 8) -> Image.Image:
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for x in range(width):
        level = round(x * 255 / (width - 1))
        draw.line([(x, 0), (x, height - 1)], fill=(level, level, level))
    return image


def test_solid_grey_measures_as_neutral() -> None:
    measurements = measure_image(Image.new("RGB", (64, 64), (128, 128, 128)))

    assert measurements.luma.mean == pytest.approx(128, abs=1)
    assert measurements.luma.std == pytest.approx(0, abs=0.01)
    assert measurements.luma.p50 == 128
    assert measurements.clipped_shadows_share == 0.0
    assert measurements.clipped_highlights_share == 0.0
    assert measurements.neutral_red_ratio == pytest.approx(1.0, abs=0.02)
    assert measurements.neutral_blue_ratio == pytest.approx(1.0, abs=0.02)
    assert measurements.neutral_coverage == pytest.approx(1.0, abs=0.01)
    assert measurements.saturation_mean == pytest.approx(0, abs=0.5)
    # A flat patch is legitimately reported as low-range, but it must not be
    # reported as having a colour cast.
    assert not any("偏暖" in note or "偏冷" in note for note in measurements.notes)


def test_ramp_percentiles_follow_the_gradient() -> None:
    measurements = measure_image(_gradient())

    assert measurements.luma.p01 <= 4
    assert measurements.luma.p05 == pytest.approx(13, abs=3)
    assert measurements.luma.p50 == pytest.approx(128, abs=2)
    assert measurements.luma.p95 == pytest.approx(242, abs=3)
    assert measurements.luma.range_90 == pytest.approx(229, abs=5)
    assert measurements.deep_shadow_share < 0.08
    assert measurements.bright_highlight_share < 0.08


def test_crushed_black_is_counted() -> None:
    image = Image.new("RGB", (64, 64), (128, 128, 128))
    ImageDraw.Draw(image).rectangle([0, 0, 31, 63], fill=(0, 0, 0))

    measurements = measure_image(image)

    assert measurements.clipped_shadows_share == pytest.approx(0.5, abs=0.01)
    assert any("纯黑" in note for note in measurements.notes)


def test_detects_warm_cast_when_no_neutral_surface_exists() -> None:
    measurements = measure_image(Image.new("RGB", (64, 64), (190, 140, 100)))

    assert measurements.neutral_red_ratio == pytest.approx(1.357, abs=0.02)
    assert measurements.neutral_coverage == 0.0
    assert measurements.warm_bias > 1.12
    assert any("偏暖" in note for note in measurements.notes)


def test_neutral_grey_patch_drives_the_white_balance_estimate() -> None:
    # A colourful frame with a small neutral card in the corner: the card wins
    # over the average colour, which is what a grey-world estimate should do.
    image = Image.new("RGB", (100, 100), (60, 130, 40))
    ImageDraw.Draw(image).rectangle([0, 0, 19, 19], fill=(150, 150, 150))

    measurements = measure_image(image)

    assert 0.0 < measurements.neutral_coverage < 0.1
    assert measurements.neutral_red_ratio == pytest.approx(1.0, abs=0.02)


def test_mid_tone_ev_delta_signs_and_magnitude() -> None:
    neutral = measure_image(Image.new("RGB", (32, 32), (128, 128, 128)))
    dark = measure_image(Image.new("RGB", (32, 32), (40, 40, 40)))

    assert -0.5 < neutral.mid_tone_ev_delta < 0.0
    assert 3.0 < dark.mid_tone_ev_delta < 3.8


def test_flat_image_is_reported_as_low_range() -> None:
    measurements = measure_image(Image.new("RGB", (64, 64), (90, 90, 90)))

    assert measurements.luma.range_90 == 0
    assert measurements.flat_share == pytest.approx(1.0, abs=0.01)
    assert any("发闷" in note or "发灰" in note for note in measurements.notes)


def test_prompt_block_carries_the_measured_numbers() -> None:
    measurements = measure_image(Image.new("RGB", (64, 64), (128, 128, 128)))
    block = describe_for_prompt(measurements)

    assert "图像量化测量" in block
    assert "中位 128" in block
    assert "0.00%" in block
    assert "18% 灰" in block


def test_measure_file_downscales_before_measuring(tmp_path) -> None:
    path = tmp_path / "big.png"
    Image.new("RGB", (2048, 1024), (100, 120, 140)).save(path)

    measurements = measure_file(path)

    assert max(measurements.width, measurements.height) == MEASUREMENT_MAX_SIZE[0]
    assert (measurements.width, measurements.height) == (512, 256)


def test_clip_levels_are_low_so_real_photos_still_report_something() -> None:
    assert BLACK_CLIP_LEVEL == 2
