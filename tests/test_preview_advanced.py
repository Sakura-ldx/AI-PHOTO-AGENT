"""Behaviour tests for the ``pillow_preview_v5`` advanced parameter stages.

Every fixture here is generated deterministically inside the test, so the
assertions are stable across machines and Pillow versions.  The tolerances are
written explicitly next to each assertion.
"""

from __future__ import annotations

import colorsys

from PIL import Image, ImageChops, ImageOps, ImageStat

from app.schemas import AdvancedParameters, EditingParametersV1, ToneCurvePoint
from app.services.preview import (
    PREVIEW_RENDERER_VERSION,
    _build_tonal_lut,
    render_image,
)


def _neutral_parameters(**advanced: object) -> EditingParametersV1:
    """Neutral parameters with the given advanced fields overridden."""
    base = EditingParametersV1().scaled(0.0)
    return base.model_copy(
        update={"advanced": AdvancedParameters(**advanced)}, deep=True
    )


def _colour_card(width: int = 96, height: int = 72) -> Image.Image:
    """Deterministic saturated hue sweep with a vertical saturation ramp."""
    image = Image.new("RGB", (width, height))
    pixels = []
    for row in range(height):
        saturation = 0.55 + 0.35 * (row / max(1, height - 1))
        for column in range(width):
            hue = column / max(1, width - 1)
            red, green, blue = colorsys.hsv_to_rgb(hue, saturation, 0.92)
            pixels.append(
                (round(red * 255), round(green * 255), round(blue * 255))
            )
    image.putdata(pixels)
    return image


def _gray_ramp(width: int = 256, height: int = 8) -> Image.Image:
    """Horizontal 0..255 grey ramp (exact 0..255 when width == 256)."""
    ramp = Image.new("L", (width, height))
    values = [round(column * 255 / max(1, width - 1)) for column in range(width)]
    ramp.putdata(values * height)
    return ramp.convert("RGB")


def _flat_gray(size: tuple[int, int] = (160, 120), level: int = 128) -> Image.Image:
    return Image.new("RGB", size, (level, level, level))


def _region_mean(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    means = ImageStat.Stat(image.crop(box)).mean
    return sum(means) / len(means)


def _channel_spread(image: Image.Image) -> int:
    return max(
        max(pixel) - min(pixel)
        for y in range(image.height)
        for x in range(image.width)
        if isinstance(pixel := image.getpixel((x, y)), tuple)
    )


def test_preview_renderer_version_is_v5() -> None:
    assert PREVIEW_RENDERER_VERSION == "pillow_preview_v5"


def test_zero_parameters_render_is_identity() -> None:
    image = _gray_ramp()
    rendered = render_image(image, EditingParametersV1().scaled(0.0))

    assert ImageChops.difference(rendered, image).getbbox() is None
    assert rendered.size == image.size


def test_post_crop_vignette_darkens_corners_and_keeps_center() -> None:
    image = _flat_gray()
    rendered = render_image(
        image, _neutral_parameters(post_crop_vignette_amount=-60)
    )

    # Explicit tolerance: an 8x8 corner region must be at least 20 levels
    # darker than the 128 source level (the mask composites towards black).
    corner_mean = _region_mean(rendered, (0, 0, 8, 8))
    assert corner_mean < 108.0

    # Explicit tolerance: the centre pixel may shift by at most 1 level.
    for original, rendered_channel in zip(
        image.getpixel((80, 60)), rendered.getpixel((80, 60)), strict=True
    ):
        assert abs(original - rendered_channel) <= 1


def test_lens_vignette_darkens_corners_and_keeps_center() -> None:
    image = _flat_gray()
    rendered = render_image(
        image, _neutral_parameters(lens_vignette_amount=-60)
    )

    corner_mean = _region_mean(rendered, (0, 0, 8, 8))
    assert corner_mean < 108.0

    for original, rendered_channel in zip(
        image.getpixel((80, 60)), rendered.getpixel((80, 60)), strict=True
    ):
        assert abs(original - rendered_channel) <= 1


def test_grain_is_deterministic_and_raises_variance() -> None:
    image = _flat_gray()
    parameters = _neutral_parameters(grain_amount=50, grain_size=25)

    first = render_image(image, parameters)
    second = render_image(image, parameters)
    assert ImageChops.difference(first, second).getbbox() is None

    flat_variance = ImageStat.Stat(ImageOps.grayscale(image)).var[0]
    grain_variance = ImageStat.Stat(ImageOps.grayscale(first)).var[0]
    # Explicit tolerance: grain variance must exceed the flat source by > 1.0.
    assert grain_variance > flat_variance + 1.0


def test_grain_amount_zero_produces_no_pixel_change() -> None:
    image = _colour_card()
    neutral = render_image(image, EditingParametersV1().scaled(0.0))
    rendered = render_image(
        image, _neutral_parameters(grain_amount=0, grain_size=100)
    )
    assert ImageChops.difference(rendered, neutral).getbbox() is None


def test_black_and_white_has_no_channel_spread() -> None:
    image = _colour_card()
    rendered = render_image(image, _neutral_parameters(black_and_white=True))

    # Explicit tolerance: all three channels must be exactly equal (spread 0).
    assert _channel_spread(rendered) == 0
    assert ImageChops.difference(rendered, image).getbbox() is not None

    # The grey mixers keep the output neutral as well.
    mixed = render_image(
        image,
        _neutral_parameters(
            black_and_white=True,
            gray_mixer_red=-80,
            gray_mixer_orange=40,
            gray_mixer_blue=60,
            gray_mixer_magenta=-20,
        ),
    )
    assert _channel_spread(mixed) == 0
    assert ImageChops.difference(mixed, rendered).getbbox() is not None


def test_parametric_shadows_only_changes_low_levels() -> None:
    ramp = _gray_ramp()
    neutral = render_image(ramp, EditingParametersV1().scaled(0.0))
    shadowed = render_image(ramp, _neutral_parameters(parametric_shadows=-80))

    def level(image: Image.Image, column: int) -> int:
        return image.getpixel((column, 0))[0]

    # The shadow bump is active around the shadow split (25% -> level ~64).
    assert abs(level(shadowed, 32) - level(neutral, 32)) >= 3
    # Explicit tolerance: levels above the highlight split (75% -> 191) may
    # differ by at most 1 level, i.e. effectively unchanged.
    for column in range(192, 256):
        assert abs(level(shadowed, column) - level(neutral, column)) <= 1


def test_red_channel_curve_only_affects_red() -> None:
    image = _colour_card()
    neutral = render_image(image, EditingParametersV1().scaled(0.0))
    curve = [
        ToneCurvePoint(x=0, y=0),
        ToneCurvePoint(x=128, y=170),
        ToneCurvePoint(x=255, y=255),
    ]
    rendered = render_image(image, _neutral_parameters(curve_red=curve))

    red_difference = ImageChops.difference(
        rendered.getchannel("R"), neutral.getchannel("R")
    )
    assert red_difference.getbbox() is not None

    for channel in ("G", "B"):
        difference = ImageChops.difference(
            rendered.getchannel(channel), neutral.getchannel(channel)
        )
        assert difference.getbbox() is None


def test_defringe_desaturates_only_the_purple_band() -> None:
    image = Image.new("RGB", (64, 32))
    purple = (96, 48, 160)
    red = (200, 40, 40)
    image.paste(purple, (0, 0, 32, 32))
    image.paste(red, (32, 0, 64, 32))

    rendered = render_image(
        image, _neutral_parameters(defringe_purple_amount=100)
    )

    # The purple patch is flat, so compare its chroma (max - min) directly.
    before_purple = max(purple) - min(purple)
    after_purple = max(rendered.getpixel((16, 16))) - min(
        rendered.getpixel((16, 16))
    )
    # Explicit tolerance: at least 20 levels less chroma in the purple band.
    assert before_purple - after_purple >= 20

    red_before = image.getpixel((48, 16))
    red_after = rendered.getpixel((48, 16))
    # The red patch is outside the purple hue band and must stay unchanged.
    for original, result in zip(red_before, red_after, strict=True):
        assert abs(original - result) <= 1


def test_parametric_curve_lut_stays_monotone_and_keeps_true_black() -> None:
    """The parametric overlay is smoothed and clamped inside the tonal LUT."""
    neutral = EditingParametersV1().scaled(0.0)
    configurations = [
        {"parametric_shadows": -100},
        {"parametric_shadows": 100, "parametric_darks": 100},
        {"parametric_lights": -100, "parametric_highlights": 100},
        {
            "parametric_shadows": 100,
            "parametric_darks": -100,
            "parametric_lights": -100,
            "parametric_highlights": 100,
            "parametric_shadow_split": 80,
            "parametric_midtone_split": 20,
            "parametric_highlight_split": 90,
        },
        {"parametric_highlights": -100, "parametric_highlight_split": 25},
    ]
    for advanced in configurations:
        parameters = neutral.model_copy(
            update={"advanced": AdvancedParameters(**advanced)}, deep=True
        )
        lut = _build_tonal_lut(parameters)
        assert len(lut) == 256
        assert lut[0] == 0
        assert all(lut[index] <= lut[index + 1] for index in range(255))


def test_black_and_white_ignores_colour_panels():
    # Lightroom removes Vibrance/Saturation and disables HSL and Colour Grading in
    # black and white mode; the preview must stay neutral grey even when the model
    # also asked for colour work.
    parameters = EditingParametersV1.model_validate(
        {
            **EditingParametersV1().model_dump(),
            "saturation": 60,
            "vibrance": 40,
            "hsl": {
                "red": {"hue": 0, "saturation": 80, "luminance": 0},
                "blue": {"hue": 0, "saturation": 80, "luminance": 0},
            },
            "color_grading": {
                "shadows": {"hue": 200, "saturation": 60, "luminance": 0},
                "midtones": {"hue": 0, "saturation": 0, "luminance": 0},
                "highlights": {"hue": 0, "saturation": 0, "luminance": 0},
                "blending": 50,
                "balance": 0,
            },
            "advanced": {"black_and_white": True},
        }
    )
    image = Image.new("RGB", (64, 64))
    image.putdata([(200, 60, 40)] * 64 * 64)
    rendered = render_image(image, parameters)
    red, green, blue = rendered.getpixel((10, 10))
    assert max(red, green, blue) - min(red, green, blue) <= 1
