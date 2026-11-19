from __future__ import annotations

import colorsys
import hashlib
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageStat

from app.schemas import (
    ColorGrading,
    ColorGradingWheel,
    EditingParametersV1,
    HslAdjustment,
    NoiseReduction,
)
from app.services.preview import render_preview


def _neutral_parameters() -> EditingParametersV1:
    return EditingParametersV1().scaled(0.0)


def _make_color_tone_fixture(path: Path) -> None:
    width, height = 192, 120
    pixels: list[tuple[int, int, int]] = []
    for y in range(height):
        value = 0.12 + (0.8 * y / (height - 1))
        for x in range(width):
            hue = x / width
            red, green, blue = colorsys.hsv_to_rgb(hue, 0.55, value)
            noise = 8 if (x + y) % 2 else -8
            pixels.append(
                tuple(
                    max(0, min(255, round(channel * 255) + noise))
                    for channel in (red, green, blue)
                )
            )
    image = Image.new("RGB", (width, height))
    image.putdata(pixels)
    image.save(path, format="PNG")


def _make_luminance_gradient_fixture(path: Path) -> None:
    width, height = 128, 120
    image = Image.new("L", (width, height))
    image.putdata(
        [
            round(18 + (219 * y / (height - 1)))
            for y in range(height)
            for _x in range(width)
        ]
    )
    image.convert("RGB").save(path, format="PNG")


def _make_luminance_noise_fixture(path: Path) -> None:
    width, height = 128, 96
    image = Image.new("L", (width, height))
    image.putdata(
        [64 if (x + y) % 2 else 192 for y in range(height) for x in range(width)]
    )
    image.convert("RGB").save(path, format="PNG")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mean_luminance(image: Image.Image) -> float:
    return ImageStat.Stat(image.convert("L")).mean[0]


def _difference_energy(first: Image.Image, second: Image.Image) -> float:
    difference = ImageChops.difference(first.convert("RGB"), second.convert("RGB"))
    return sum(ImageStat.Stat(difference).mean) / 3


def test_zero_adjustments_are_deterministic_and_visually_neutral(tmp_path: Path) -> None:
    original = tmp_path / "fixture.png"
    first = tmp_path / "neutral-first.jpg"
    second = tmp_path / "neutral-second.jpg"
    _make_color_tone_fixture(original)

    parameters = _neutral_parameters()
    render_preview(original, first, parameters)
    render_preview(original, second, parameters)

    assert _sha256(first) == _sha256(second)
    with Image.open(original) as source, Image.open(first) as rendered:
        assert rendered.size == source.size
        assert _difference_energy(source, rendered) < 8.0


def test_highlights_and_shadows_change_their_tonal_regions(tmp_path: Path) -> None:
    original = tmp_path / "fixture.png"
    neutral_path = tmp_path / "neutral.jpg"
    adjusted_path = tmp_path / "tonal.jpg"
    _make_luminance_gradient_fixture(original)

    neutral = _neutral_parameters()
    adjusted = neutral.model_copy(
        update={"highlights": -100, "shadows": 100},
        deep=True,
    )
    render_preview(original, neutral_path, neutral)
    render_preview(original, adjusted_path, adjusted)

    assert _sha256(neutral_path) != _sha256(adjusted_path)
    with Image.open(neutral_path) as baseline, Image.open(adjusted_path) as result:
        upper = (0, 0, baseline.width, baseline.height // 3)
        lower = (0, baseline.height * 2 // 3, baseline.width, baseline.height)
        assert _mean_luminance(result.crop(upper)) > _mean_luminance(
            baseline.crop(upper)
        )
        assert _mean_luminance(result.crop(lower)) < _mean_luminance(
            baseline.crop(lower)
        )


@pytest.mark.parametrize(
    "adjustment",
    [
        HslAdjustment(hue=100),
        HslAdjustment(saturation=-100),
        HslAdjustment(luminance=100),
    ],
    ids=["hue", "saturation", "luminance"],
)
def test_each_hsl_control_changes_the_preview(
    tmp_path: Path,
    adjustment: HslAdjustment,
) -> None:
    original = tmp_path / "fixture.png"
    neutral_path = tmp_path / "neutral.jpg"
    adjusted_path = tmp_path / "hsl.jpg"
    _make_color_tone_fixture(original)

    neutral = _neutral_parameters()
    adjusted = neutral.model_copy(update={"hsl": {"red": adjustment}}, deep=True)
    render_preview(original, neutral_path, neutral)
    render_preview(original, adjusted_path, adjusted)

    assert _sha256(neutral_path) != _sha256(adjusted_path)


def test_color_grading_uses_hue_saturation_and_luminance(tmp_path: Path) -> None:
    original = tmp_path / "fixture.png"
    neutral_path = tmp_path / "neutral.jpg"
    red_dark_path = tmp_path / "red-dark.jpg"
    cyan_dark_path = tmp_path / "cyan-dark.jpg"
    red_light_path = tmp_path / "red-light.jpg"
    _make_color_tone_fixture(original)

    neutral = _neutral_parameters()
    red_dark = neutral.model_copy(
        update={
            "color_grading": ColorGrading(
                shadows=ColorGradingWheel(hue=0, saturation=100, luminance=-60)
            )
        },
        deep=True,
    )
    cyan_dark = neutral.model_copy(
        update={
            "color_grading": ColorGrading(
                shadows=ColorGradingWheel(hue=180, saturation=100, luminance=-60)
            )
        },
        deep=True,
    )
    red_light = neutral.model_copy(
        update={
            "color_grading": ColorGrading(
                shadows=ColorGradingWheel(hue=0, saturation=100, luminance=60)
            )
        },
        deep=True,
    )

    for output, parameters in (
        (neutral_path, neutral),
        (red_dark_path, red_dark),
        (cyan_dark_path, cyan_dark),
        (red_light_path, red_light),
    ):
        render_preview(original, output, parameters)

    hashes = {_sha256(path) for path in (neutral_path, red_dark_path, cyan_dark_path)}
    assert len(hashes) == 3
    assert _sha256(red_dark_path) != _sha256(red_light_path)

    with Image.open(red_dark_path) as darker, Image.open(red_light_path) as lighter:
        upper = (0, 0, darker.width, darker.height // 3)
        assert _mean_luminance(darker.crop(upper)) < _mean_luminance(
            lighter.crop(upper)
        )


def test_color_grading_weights_shadow_midtone_and_highlight_wheels(
    tmp_path: Path,
) -> None:
    original = tmp_path / "fixture.png"
    neutral_path = tmp_path / "neutral.jpg"
    _make_color_tone_fixture(original)
    neutral = _neutral_parameters()
    render_preview(original, neutral_path, neutral)

    variants = {
        "shadows": ColorGrading(
            shadows=ColorGradingWheel(hue=0, saturation=100, luminance=-30)
        ),
        "midtones": ColorGrading(
            midtones=ColorGradingWheel(hue=120, saturation=100, luminance=30)
        ),
        "highlights": ColorGrading(
            highlights=ColorGradingWheel(hue=240, saturation=100, luminance=30)
        ),
    }

    with Image.open(neutral_path) as baseline_source:
        baseline = baseline_source.convert("RGB")
    third = baseline.height // 3
    regions = {
        "shadows": (0, 0, baseline.width, third),
        "midtones": (0, third, baseline.width, third * 2),
        "highlights": (0, third * 2, baseline.width, baseline.height),
    }

    for name, grading in variants.items():
        output_path = tmp_path / f"{name}.jpg"
        parameters = neutral.model_copy(update={"color_grading": grading}, deep=True)
        render_preview(original, output_path, parameters)
        with Image.open(output_path) as output_source:
            output = output_source.convert("RGB")
        own_energy = _difference_energy(
            baseline.crop(regions[name]),
            output.crop(regions[name]),
        )
        other_energies = [
            _difference_energy(baseline.crop(box), output.crop(box))
            for region_name, box in regions.items()
            if region_name != name
        ]
        assert own_energy > min(other_energies)


def test_luminance_noise_reduction_changes_and_smooths_the_preview(
    tmp_path: Path,
) -> None:
    original = tmp_path / "noise.png"
    neutral_path = tmp_path / "neutral.jpg"
    reduced_path = tmp_path / "reduced.jpg"
    _make_luminance_noise_fixture(original)

    neutral = _neutral_parameters()
    reduced = neutral.model_copy(
        update={"noise_reduction": NoiseReduction(luminance=100, color=0)},
        deep=True,
    )
    render_preview(original, neutral_path, neutral)
    render_preview(original, reduced_path, reduced)

    assert _sha256(neutral_path) != _sha256(reduced_path)
    with Image.open(neutral_path) as baseline, Image.open(reduced_path) as result:
        baseline_variance = ImageStat.Stat(baseline.convert("L")).var[0]
        reduced_variance = ImageStat.Stat(result.convert("L")).var[0]
        assert reduced_variance < baseline_variance * 0.5
