"""Fidelity acceptance tests for the ``pillow_preview_v4`` renderer.

These pin the tonal and colour behaviour that made the old v3 preview look
washed out: the tonal sliders must map luminance through one combined LUT
instead of compositing toward white/black, white balance must operate in
linear light without changing overall luminance, and the colour tools must
stay bounded instead of pushing channels to 0/255.

All assertions run against :func:`app.services.preview.render_image` on
synthetic Pillow images, so they measure the renderer itself rather than JPEG
compression noise. A final integration test checks that ``render_preview`` is
implemented on top of ``render_image`` and keeps its file-writing contract.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from app.schemas import (
    ColorGrading,
    ColorGradingWheel,
    EditingParametersV1,
    HslAdjustment,
    NoiseReduction,
    Sharpening,
)
from app.services.preview import (
    PREVIEW_RENDERER_VERSION,
    _build_tonal_lut,
    render_image,
    render_preview,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _neutral(**updates) -> EditingParametersV1:
    """Identity parameters: every slider zero, including the default tone curve.

    A bare ``EditingParametersV1()`` is not neutral: it carries a contrasty
    default tone curve and noise reduction, so tests that want to observe one
    primitive in isolation start from ``.scaled(0.0)``.
    """

    return EditingParametersV1().scaled(0.0).model_copy(update=updates, deep=True)


def _solid(value: int, size: int = 32) -> Image.Image:
    return Image.new("RGB", (size, size), (value, value, value))


def _horizontal_ramp(width: int = 256, height: int = 8) -> Image.Image:
    image = Image.new("RGB", (width, height))
    image.putdata([(x, x, x) for _ in range(height) for x in range(width)])
    return image


def _texture(width: int = 96, height: int = 64) -> Image.Image:
    """Deterministic high-frequency grey texture with true black pixels."""

    image = Image.new("RGB", (width, height))
    image.putdata([((x * 37 + y * 11) % 256,) * 3 for y in range(height) for x in range(width)])
    return image


def _chroma_noise(width: int = 96, height: int = 64) -> Image.Image:
    """Noisy colour image used to observe chroma noise reduction."""

    image = Image.new("RGB", (width, height))
    image.putdata(
        [
            ((x * 37 + y * 11) % 256, (x * 53 + y * 7 + 40) % 256, (x * 29 + y * 19 + 90) % 256)
            for y in range(height)
            for x in range(width)
        ]
    )
    return image


def _luma(image: Image.Image) -> Image.Image:
    return image.convert("L")


def _level(image: Image.Image, x: int) -> int:
    """Read one horizontal-ramp level (channel R == channel G == channel B)."""

    return image.getpixel((x, 0))[0]


def _percentile(image: Image.Image, fraction: float) -> int:
    values = sorted(_luma(image).getdata())
    return values[int(len(values) * fraction)]


def _max_channel_diff(left: Image.Image, right: Image.Image) -> int:
    return max(ImageChops.difference(left, right).getextrema()[channel][1] for channel in range(3))


def _black_levels(image: Image.Image, source: Image.Image) -> list[int]:
    """Rendered levels of every pixel that was pure black in ``source``."""

    width = source.width
    return [
        image.getpixel((index % width, index // width))[0]
        for index, pixel in enumerate(source.getdata())
        if pixel == (0, 0, 0)
    ]


def _high_frequency_energy(image: Image.Image, radius: float) -> float:
    """Mean |image - blur| on luma: how much detail survives at ``radius``."""

    grey = _luma(image)
    blurred = grey.filter(ImageFilter.GaussianBlur(radius))
    return float(ImageStat.Stat(ImageChops.difference(grey, blurred)).mean[0])


# ---------------------------------------------------------------------------
# A. Tonal LUT
# ---------------------------------------------------------------------------


def test_renderer_version_is_pillow_preview_v5():
    assert PREVIEW_RENDERER_VERSION == "pillow_preview_v5"


def test_exposure_is_a_real_ev_gain_on_mid_grey():
    """+1 EV should lift mid grey 128 into the high 160s/low 180s, not +18%."""

    rendered = render_image(_solid(128), _neutral(exposure=1.0))
    assert 168 <= _level(rendered, 0) <= 192


def test_shadows_lift_preserves_black_black_and_midtones():
    """Shadows raise dark values a little, leave midtones alone and never
    lift true black (the old renderer composited darks toward white)."""

    ramp = _horizontal_ramp()
    rendered = render_image(ramp, _neutral(shadows=100))

    assert _level(rendered, 0) <= 1, "true black must stay black"
    assert _level(rendered, 20) - 20 <= 14, "L=20 may rise at most ~14 levels"
    assert abs(_level(rendered, 128) - 128) <= 2, "mid grey must be untouched"
    assert abs(_level(rendered, 110) - 110) <= 2, "lift must decay to zero by L~110"

    # Shape preserving: the underlying 256-entry LUT never goes backwards and
    # the ratio-quantised application wobbles by at most one rounding level.
    assert _build_tonal_lut(_neutral(shadows=100)) == sorted(
        _build_tonal_lut(_neutral(shadows=100))
    )
    levels = [_level(rendered, x) for x in range(256)]
    assert all(levels[i + 1] >= levels[i] - 1 for i in range(255))


def test_highlights_rolloff_spares_midtones_and_black():
    """-100 highlights only pulls the top end; below L~140 nothing changes."""

    ramp = _horizontal_ramp()
    rendered = render_image(ramp, _neutral(highlights=-100))

    assert _level(rendered, 0) == 0
    assert abs(_level(rendered, 128) - 128) <= 2
    assert abs(_level(rendered, 140) - 140) <= 2, "no effect below L~140"
    assert _level(rendered, 250) <= 250 - 8
    assert _level(rendered, 170) <= 170 - 8

    assert _build_tonal_lut(_neutral(highlights=-100)) == sorted(
        _build_tonal_lut(_neutral(highlights=-100))
    )
    levels = [_level(rendered, x) for x in range(256)]
    assert all(levels[i + 1] >= levels[i] - 1 for i in range(255))


def test_contrast_s_curve_keeps_median_and_expands_the_ends():
    """Contrast pivots around the median instead of scaling linearly."""

    ramp = _horizontal_ramp()
    neutral = render_image(ramp, _neutral())
    rendered = render_image(ramp, _neutral(contrast=50))

    assert abs(_percentile(rendered, 0.50) - _percentile(neutral, 0.50)) <= 3
    assert _percentile(rendered, 0.10) < _percentile(neutral, 0.10)
    assert _percentile(rendered, 0.90) > _percentile(neutral, 0.90)


def test_zero_parameters_are_an_identity_on_a_ramp():
    """No adjustments must reproduce the source, not merely come close."""

    ramp = _horizontal_ramp()
    rendered = render_image(ramp, _neutral())
    assert _max_channel_diff(ramp, rendered) <= 2


# ---------------------------------------------------------------------------
# B. White balance
# ---------------------------------------------------------------------------


def test_temperature_warming_is_luminance_preserving():
    """A neutral grey patch warms up but does not get brighter or darker."""

    grey = _solid(128)
    rendered = render_image(grey, _neutral(temperature=50))
    red, green, blue = rendered.getpixel((0, 0))

    baseline_ratio = 1.0
    assert red / blue > baseline_ratio, "temperature+ must be warmer (R up, B down)"
    assert red > green > blue

    luma_before = 128.0
    luma_after = 0.299 * red + 0.587 * green + 0.114 * blue
    assert abs(luma_after - luma_before) / luma_before <= 0.03

    # Full strength is visible but calibrated: R/B ratio changes by 8-15%.
    hot = render_image(grey, _neutral(temperature=100)).getpixel((0, 0))
    assert 1.08 <= hot[0] / hot[2] <= 1.15


def test_positive_tint_is_magenta_negative_tint_is_green():
    """Sign convention shared with the Lightroom temperature/tint mapping."""

    grey = _solid(100, size=4)
    positive = render_image(grey, _neutral(tint=100)).getpixel((0, 0))
    negative = render_image(grey, _neutral(tint=-100)).getpixel((0, 0))

    assert positive[0] > positive[1] and positive[2] > positive[1]
    assert negative[1] > negative[0] and negative[1] > negative[2]


# ---------------------------------------------------------------------------
# C. HSL / colour grading
# ---------------------------------------------------------------------------


def test_hsl_adjustments_are_bounded_blends_not_extremes():
    """HSL must shift a channel part-way toward a bound, never composite it to
    0/255 (the old renderer pushed channels to the extremes)."""

    red_patch = Image.new("RGB", (24, 24), (200, 30, 30))
    original = red_patch.getpixel((12, 12))

    saturated = render_image(
        red_patch, _neutral(hsl={"red": HslAdjustment(saturation=100)})
    ).getpixel((12, 12))
    desaturated = render_image(
        red_patch, _neutral(hsl={"red": HslAdjustment(saturation=-100)})
    ).getpixel((12, 12))
    brighter = render_image(
        red_patch, _neutral(hsl={"red": HslAdjustment(luminance=100)})
    ).getpixel((12, 12))
    darker = render_image(
        red_patch, _neutral(hsl={"red": HslAdjustment(luminance=-100)})
    ).getpixel((12, 12))

    assert saturated != original and desaturated != original
    assert brighter != original and darker != original
    # Bounded: no channel is forced to a 0/255 extreme.
    for result in (saturated, desaturated, brighter, darker):
        assert 0 < result[0] < 255
        assert result[1] > 0 and result[2] > 0
    assert saturated[1] < original[1] < desaturated[1], "saturation moves G both ways"
    assert brighter[0] > original[0] > darker[0], "luminance moves the red channel"


def test_colour_grading_is_additive_split_toning():
    """A shadow wheel adds a tint inside the shadow mask but must not repaint
    the region to a flat colour or blow it out."""

    dark_patch = Image.new("RGB", (24, 24), (30, 30, 30))
    grading = _neutral(
        color_grading=ColorGrading(
            shadows=ColorGradingWheel(hue=0, saturation=100, luminance=0),
        )
    )
    rendered = render_image(dark_patch, grading).getpixel((12, 12))

    assert rendered != (30, 30, 30)
    assert rendered[0] > rendered[1], "red wheel should add red only"
    assert rendered[0] - rendered[1] >= 20, "tint must be clearly visible"
    assert rendered[0] < 180, "additive tint must stay bounded"


# ---------------------------------------------------------------------------
# D. Local contrast, texture, dehaze, sharpening
# ---------------------------------------------------------------------------


def test_clarity_is_large_radius_local_contrast():
    texture = _texture()
    rendered = render_image(texture, _neutral(clarity=100))

    assert _max_channel_diff(texture, rendered) > 0
    assert _high_frequency_energy(rendered, radius=24) > _high_frequency_energy(
        texture, radius=24
    ) + 5, "clarity must boost broad local contrast"
    assert set(_black_levels(rendered, texture)) == {0}, "clarity must not lift black"


def test_texture_is_small_radius_detail_boost():
    texture = _texture()
    rendered = render_image(texture, _neutral(texture=100))

    assert _max_channel_diff(texture, rendered) > 0
    assert _high_frequency_energy(rendered, radius=2) > _high_frequency_energy(
        texture, radius=2
    ) + 5, "texture must boost fine detail"
    assert set(_black_levels(rendered, texture)) == {0}, "texture must not lift black"


def test_dehaze_is_gentle_and_never_lifts_black():
    texture = _texture()
    rendered = render_image(texture, _neutral(dehaze=100))

    assert _max_channel_diff(texture, rendered) > 0
    assert _max_channel_diff(texture, rendered) <= 20, "dehaze must stay a small amplitude"
    assert set(_black_levels(rendered, texture)) == {0}, "dehaze must not lift black"


def test_sharpening_increases_local_contrast():
    texture = _texture()
    rendered = render_image(
        texture, _neutral(sharpening=Sharpening(amount=100, radius=1.0))
    )
    baseline_std = ImageStat.Stat(_luma(texture)).stddev[0]
    rendered_std = ImageStat.Stat(_luma(rendered)).stddev[0]
    assert rendered_std > baseline_std + 1.0


# ---------------------------------------------------------------------------
# E. Noise reduction
# ---------------------------------------------------------------------------


def test_chroma_noise_reduction_smooths_colour_channels():
    noisy = _chroma_noise()
    rendered = render_image(
        noisy, _neutral(noise_reduction=NoiseReduction(luminance=0, color=100))
    )

    def chroma_std(image: Image.Image) -> tuple[float, float]:
        cb, cr = image.convert("YCbCr").split()[1:]
        return float(ImageStat.Stat(cb).stddev[0]), float(ImageStat.Stat(cr).stddev[0])

    before_cb, before_cr = chroma_std(noisy)
    after_cb, after_cr = chroma_std(rendered)
    assert after_cb < before_cb * 0.5
    assert after_cr < before_cr * 0.5


# ---------------------------------------------------------------------------
# render_preview integration contract
# ---------------------------------------------------------------------------


def test_render_preview_builds_on_render_image(tmp_path: Path):
    """render_preview must load/thumbnail and then hand off to render_image."""

    source_path = tmp_path / "source.png"
    ramp = _horizontal_ramp(width=320, height=64)
    ramp.save(source_path)
    output_path = tmp_path / "nested" / "preview.jpg"
    parameters = EditingParametersV1().scaled(0.0)

    returned = render_preview(source_path, output_path, parameters)

    assert returned == output_path
    assert output_path.exists()
    assert not list(output_path.parent.glob("*.tmp"))

    with Image.open(output_path) as preview:
        assert preview.mode == "RGB"
        assert preview.size == ramp.size
        expected = render_image(ramp, parameters)
        # JPEG q90 round trip: smooth ramp should be within a couple of levels.
        assert _max_channel_diff(expected.convert("RGB"), preview.convert("RGB")) <= 4
