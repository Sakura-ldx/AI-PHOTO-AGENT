"""Pillow based approximate preview renderer (``pillow_preview_v5``).

The renderer is intentionally not a colour-managed RAW pipeline; it is a fast,
deterministic approximation of what Lightroom will do with the same parameters.
Version 4 fixes the "washed out / grey / dirty" look of v3 by replacing every
operation that used to composite towards pure white/black with proper tonal
mapping:

* all luminance operations are merged into a single monotone 256 entry LUT that
  is applied ratio preserving, so hue and saturation survive tonal changes and
  true black stays true black;
* white balance is a linear light per channel gain that preserves luminance;
* HSL and colour grading use bounded masked blends / additive split toning
  instead of repainting a region towards an extreme;
* clarity/texture are local contrast filters instead of global sharpness.

Version 5 brings the ``AdvancedParameters`` sliders into the same pipeline, in
Lightroom panel order: parametric tone curve and per channel point curves inside
the tonal LUT, black & white conversion with the eight grey mixers, lens
vignetting, defringe, post crop vignetting, film grain and the noise reduction
detail modifiers.  Every new stage is a strict no-op at its neutral value, so a
zero parameter render is byte for byte identical to v4.

The public entry points are :func:`render_preview` (file in, JPEG preview out)
and :func:`render_image` (already decoded RGB in, RGB out) so that later phases
can run closed loop verification on the exact same parameter pipeline.
"""

from __future__ import annotations

import colorsys
import hashlib
import math
import os
import random
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

from app.imaging import flatten_to_rgb
from app.schemas import AdvancedParameters, EditingParametersV1, ToneCurvePoint

PREVIEW_RENDERER_VERSION = "pillow_preview_v5"

_GAMMA = 2.2
_INVERSE_GAMMA = 1.0 / _GAMMA

# Rec.601 luma weights, matching ``ImageOps.grayscale``.
_LUMA_RED = 0.299
_LUMA_GREEN = 0.587
_LUMA_BLUE = 0.114

_HSL_HUE_CENTERS = (
    ("red", 0),
    ("orange", 21),
    ("yellow", 43),
    ("green", 85),
    ("aqua", 128),
    ("blue", 170),
    ("purple", 192),
    ("magenta", 213),
)
_HSL_HUE_MAX_SHIFT = 21

# --- tonal controls -------------------------------------------------------
# Normalised S-curve strength for |contrast| = 100.  Keeping it below 1.0 keeps
# the composed curve strictly monotone.
_CONTRAST_STRENGTH = 0.85
# Maximum number of levels a full strength shadows/highlights/whites/blacks
# slider may add or remove.  Deliberately small: v3 pushed whole regions
# towards white/black which caused the washed out look.
_SHADOW_LIFT = 12.0
_HIGHLIGHT_PULL = 16.0
_WHITE_END_LIFT = 15.0
_BLACK_END_LIFT = 12.0
# Dehaze: a very gentle mid tone contrast plus a slight black point drop.
_DEHAZE_CONTRAST = 0.15
_DEHAZE_BLACK_DROP = 8.0

# --- HSL target headroom --------------------------------------------------
# Positive adjustments blend towards ``level + (255 - level) * headroom`` and
# negative adjustments towards ``level * scale`` instead of 0/255.
_HSL_SATURATION_HEADROOM = 0.5
_HSL_SATURATION_SCALE = 0.5
_HSL_LUMINANCE_HEADROOM = 0.35
_HSL_LUMINANCE_SCALE = 0.65

# --- white balance --------------------------------------------------------
# Log gains in linear light for the full slider range.  Green is derived so the
# Rec.601 luminance of a neutral patch is preserved, then all gains are
# normalised exactly.
_WB_TEMPERATURE_LOG_GAIN = 0.10
_WB_TINT_LOG_GAIN = 0.055

# --- colour grading -------------------------------------------------------
_GRADE_TINT_LEVELS = 64.0
_GRADE_LUMA_LEVELS = 0.10 * 255.0

# --- parametric tone curve (v5) -------------------------------------------
# Maximum number of encoded levels a full strength parametric slider adds in
# its region.  Each region is a sin^2 bump that is exactly zero at both edges,
# so the overlay never lifts true black or pushes true white.
_PARAMETRIC_CURVE_GAIN = 30.0

# --- black & white grey mixers (v5) ---------------------------------------
# Bounded blend headroom/scale: 128 in the interpolated mixer LUT is neutral, a
# full strength slider only moves a fully saturated pixel part way.
_GRAY_MIXER_HEADROOM = 0.7
_GRAY_MIXER_SCALE = 0.3

# --- lens vignetting (v5) -------------------------------------------------
# Normalised radius at which the falloff starts, for midpoint = 0..100.
_LENS_VIGNETTE_START_MIN = 0.15
_LENS_VIGNETTE_START_RANGE = 0.55

# --- post crop vignetting (v5) --------------------------------------------
# The superellipse mask is generated once per setting triple on a small grid
# and resized per render, so the hot path only pays for a resize + composite.
_VIGNETTE_MASK_SIZE = 96

# --- grain (v5) -----------------------------------------------------------
# One noise tile is generated per render at this resolution, then shaped,
# blurred by ``grain_size`` and scaled to the image; nothing is per pixel.
_GRAIN_BASE_SIZE = 512
_GRAIN_MAX_LEVELS = 22.0


def render_preview(
    original_path: Path,
    output_path: Path,
    parameters: EditingParametersV1,
    max_size: tuple[int, int] = (1600, 1600),
) -> Path:
    """Render ``original_path`` with ``parameters`` into ``output_path``.

    The file writing semantics are load bearing: the preview is written to a
    hidden temporary file next to the destination and atomically moved into
    place, so a failed render never leaves a partial preview behind.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        with Image.open(original_path) as source:
            image = flatten_to_rgb(ImageOps.exif_transpose(source))
            image.thumbnail(max_size)
            image = render_image(image, parameters)
            image.save(temp_path, format="JPEG", quality=90, optimize=True)
        os.replace(temp_path, output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return output_path


def render_image(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    """Apply the preview parameter pipeline to an already decoded RGB image.

    No file IO and no resizing happens here; callers that need a thumbnail must
    do it themselves.  The pipeline order follows the Lightroom panel order:
    white balance -> tonal LUT (parametric + point curve inside) -> per channel
    curves -> black & white -> saturation/vibrance -> HSL -> colour grading ->
    clarity/texture -> noise reduction -> lens vignette -> defringe -> post crop
    vignette -> grain -> sharpening.  Every v5 stage is a no-op when its
    parameter is neutral, which keeps the zero parameter render identical to v4.
    """
    if image.mode != "RGB":
        image = image.convert("RGB")
    image = _apply_white_balance(image, parameters)
    image = _apply_tonal_lut(image, parameters)
    image = _apply_channel_curves(image, parameters)
    image = _apply_black_and_white(image, parameters)
    if parameters.advanced.black_and_white:
        # Lightroom removes Vibrance/Saturation and disables HSL / Colour Grading
        # while a photo is treated as black and white, so the preview must not
        # colourise the greyscale it just produced.
        grayscale = parameters.model_copy(update={"vibrance": 0, "saturation": 0})
        image = _apply_presence(image, grayscale)
    else:
        image = _apply_presence(image, parameters)
        image = _apply_hsl_adjustments(image, parameters)
        image = _apply_color_grading(image, parameters)
    image = _apply_clarity_and_texture(image, parameters)
    image = _apply_noise_reduction(image, parameters)
    image = _apply_lens_vignette(image, parameters)
    image = _apply_defringe(image, parameters)
    image = _apply_post_crop_vignette(image, parameters)
    image = _apply_grain(image, parameters)
    image = _apply_sharpening(image, parameters)
    return image


# ---------------------------------------------------------------------------
# Tone: one combined luminance LUT applied ratio preserving
# ---------------------------------------------------------------------------


def _apply_tonal_lut(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    lut = _build_tonal_lut(parameters)
    if all(lut[level] == level for level in range(256)):
        return image

    luminance = ImageOps.grayscale(image)
    dark_lut: list[int] = []
    lift_lut: list[int] = []
    for level in range(256):
        if level == 0:
            # A pixel whose Rec.601 luma is zero is pure black: it must stay
            # black no matter what the tone controls do.
            dark_lut.append(0)
            lift_lut.append(0)
            continue
        ratio = min(2.0, lut[level] / level)
        if ratio <= 1.0:
            dark_lut.append(round(255 * ratio))
            lift_lut.append(0)
        else:
            dark_lut.append(255)
            lift_lut.append(round(255 * (ratio - 1.0)))

    dark_ratio = luminance.point(dark_lut)
    lift_ratio = luminance.point(lift_lut)
    channels = []
    for channel in image.split():
        # level * min(ratio, 1) + level * max(ratio - 1, 0) == level * ratio,
        # which keeps each pixel's channel ratios (hue and saturation) intact.
        darkened = ImageChops.multiply(channel, dark_ratio)
        lifted = ImageChops.multiply(channel, lift_ratio)
        channels.append(ImageChops.add(darkened, lifted))
    return Image.merge("RGB", channels)


def _build_tonal_lut(parameters: EditingParametersV1) -> list[int]:
    exposure_gain = 2.0 ** float(parameters.exposure)
    contrast_strength = (parameters.contrast / 100.0) * _CONTRAST_STRENGTH
    whites_fraction = parameters.whites / 100.0
    blacks_fraction = parameters.blacks / 100.0
    shadows_fraction = parameters.shadows / 100.0
    highlights_fraction = parameters.highlights / 100.0
    dehaze_fraction = parameters.dehaze / 100.0

    pre_curve_levels: list[float] = []
    for value in range(256):
        linear = (value / 255.0) ** _GAMMA
        linear *= exposure_gain
        encoded = linear**_INVERSE_GAMMA if linear > 0.0 else 0.0
        position = min(1.0, max(0.0, encoded))

        # Contrast: smooth S-curve around mid grey, not a linear scale.
        position += contrast_strength * position * (1.0 - position) * (
            2.0 * position - 1.0
        )

        # Whites / blacks / dehaze: bounded endpoint shaping.  Every term
        # vanishes at true black.
        endpoint_delta = _endpoint_delta(
            position,
            whites_fraction,
            blacks_fraction,
            dehaze_fraction,
        )
        level = min(255.0, max(0.0, position * 255.0 + endpoint_delta))

        # Shadows / highlights: additive lift/pull in the encoded level space.
        level += _shadow_highlight_delta(
            level,
            shadows_fraction,
            highlights_fraction,
        )
        pre_curve_levels.append(level)

    # Parametric (four region) tone curve: a smooth additive overlay in encoded
    # level space.  Each region is a sin^2 bump that is exactly zero at its
    # edges, so the overlay cannot lift true black or shift true white.
    advanced = parameters.advanced
    if _parametric_curve_active(advanced):
        splits = _parametric_splits(advanced)
        pre_curve_levels = [
            level + _parametric_level_delta(level, advanced, splits)
            for level in pre_curve_levels
        ]

    # The automatic tonal stages are designed to be monotone; enforce it so a
    # degenerate combination can never invert contrast.  This also keeps the
    # parametric overlay monotone after its own bumps are added.
    previous = 0.0
    for index, level in enumerate(pre_curve_levels):
        if level < previous:
            pre_curve_levels[index] = previous
        previous = pre_curve_levels[index]

    lut = [
        max(0, min(255, int(round(_tone_curve_value(parameters.tone_curve, level)))))
        for level in pre_curve_levels
    ]
    # Hard requirement: the rendered LUT never lifts true black.
    lut[0] = 0
    return lut


def _parametric_curve_active(advanced: AdvancedParameters) -> bool:
    return any(
        (
            advanced.parametric_shadows,
            advanced.parametric_darks,
            advanced.parametric_lights,
            advanced.parametric_highlights,
        )
    )


def _parametric_splits(advanced: AdvancedParameters) -> tuple[float, float, float]:
    """Region boundaries in encoded levels, forced into increasing order."""

    shadows = advanced.parametric_shadow_split / 100.0 * 255.0
    midtones = max(shadows, advanced.parametric_midtone_split / 100.0 * 255.0)
    highlights = max(midtones, advanced.parametric_highlight_split / 100.0 * 255.0)
    return shadows, midtones, highlights


def _parametric_level_delta(
    level: float,
    advanced: AdvancedParameters,
    splits: tuple[float, float, float],
) -> float:
    """Additive encoded level shift from the four parametric curve regions."""

    shadow_split, midtone_split, highlight_split = splits
    regions = (
        (advanced.parametric_shadows / 100.0, 0.0, shadow_split),
        (advanced.parametric_darks / 100.0, shadow_split, midtone_split),
        (advanced.parametric_lights / 100.0, midtone_split, highlight_split),
        (advanced.parametric_highlights / 100.0, highlight_split, 255.0),
    )
    delta = 0.0
    for fraction, low, high in regions:
        if fraction == 0.0 or high <= low:
            continue
        position = (level - low) / (high - low)
        if position <= 0.0 or position >= 1.0:
            continue
        delta += fraction * _PARAMETRIC_CURVE_GAIN * math.sin(math.pi * position) ** 2
    return delta


def _endpoint_delta(
    position: float,
    whites_fraction: float,
    blacks_fraction: float,
    dehaze_fraction: float,
) -> float:
    """Return the required shift in encoded levels for the endpoint controls."""
    delta = 0.0
    if whites_fraction != 0.0:
        weight = _smoothstep_weight((position - 0.5) * 2.0) * 2.0
        delta += whites_fraction * _WHITE_END_LIFT * (min(1.0, weight) ** 1.5)
    if blacks_fraction != 0.0:
        # Bump in the deep shadows that is exactly zero for true black.
        ramp_in = min(1.0, position / 0.10)
        ramp_out = 1.0 - min(1.0, max(0.0, position - 0.15) / 0.45)
        delta += blacks_fraction * _BLACK_END_LIFT * ramp_in * ramp_out
    if dehaze_fraction != 0.0:
        # Mid tone contrast plus a slight black point drop, both bounded.
        contrast_term = (
            dehaze_fraction
            * _DEHAZE_CONTRAST
            * position
            * (1.0 - position)
            * (2.0 * position - 1.0)
            * 255.0
        )
        drop_ramp_in = min(1.0, position / 0.12)
        drop_ramp_out = 1.0 - min(1.0, max(0.0, position / 0.55))
        black_term = (
            -dehaze_fraction
            * _DEHAZE_BLACK_DROP
            * drop_ramp_in
            * drop_ramp_out
        )
        delta += contrast_term + black_term
    return delta


def _shadow_highlight_delta(
    level: float,
    shadows_fraction: float,
    highlights_fraction: float,
) -> float:
    delta = 0.0
    if shadows_fraction != 0.0:
        # Rise from 0 at true black to full strength around L=20, then fade
        # back to zero by L~110 so mid tones are untouched.
        rise = _smoothstep_weight(level / 20.0)
        decay = 1.0 - _smoothstep_weight((level - 20.0) / 90.0)
        delta += shadows_fraction * _SHADOW_LIFT * rise * decay
    if highlights_fraction != 0.0:
        # Nothing below L~140, full strength above L~170.
        delta += (
            highlights_fraction
            * _HIGHLIGHT_PULL
            * _smoothstep_weight((level - 140.0) / 30.0)
        )
    return delta


def _tone_curve_value(points, level: float) -> float:
    clamped = min(255.0, max(0.0, level))
    for start, end in zip(points, points[1:]):
        if clamped <= end.x:
            distance = end.x - start.x
            if distance == 0:
                return float(end.y)
            ratio = (clamped - start.x) / distance
            return start.y + (end.y - start.y) * ratio
    return float(points[-1].y)


# ---------------------------------------------------------------------------
# White balance: linear light luminance preserving channel gains
# ---------------------------------------------------------------------------


def _white_balance_gains(temperature: int, tint: int) -> tuple[float, float, float]:
    warm = temperature / 100.0
    magenta = tint / 100.0
    log_red = _WB_TEMPERATURE_LOG_GAIN * warm + _WB_TINT_LOG_GAIN * magenta
    log_blue = -_WB_TEMPERATURE_LOG_GAIN * warm + _WB_TINT_LOG_GAIN * magenta
    log_green = -(_LUMA_RED * log_red + _LUMA_BLUE * log_blue) / _LUMA_GREEN
    gains = [math.exp(log_red), math.exp(log_green), math.exp(log_blue)]
    luminance = (
        _LUMA_RED * gains[0] + _LUMA_GREEN * gains[1] + _LUMA_BLUE * gains[2]
    )
    if luminance <= 0.0:
        return (1.0, 1.0, 1.0)
    return (
        gains[0] / luminance,
        gains[1] / luminance,
        gains[2] / luminance,
    )


def _apply_white_balance(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    if parameters.temperature == 0 and parameters.tint == 0:
        return image
    gains = _white_balance_gains(parameters.temperature, parameters.tint)
    channels = []
    for channel, gain in zip(image.split(), gains):
        # Decode gamma, multiply in linear light, re-encode.
        channel_lut = [
            _clamp_channel(((level / 255.0) ** _GAMMA * gain) ** _INVERSE_GAMMA * 255.0)
            for level in range(256)
        ]
        channels.append(channel.point(channel_lut))
    return Image.merge("RGB", channels)


# ---------------------------------------------------------------------------
# Global saturation / vibrance
# ---------------------------------------------------------------------------


def _apply_presence(image: Image.Image, parameters: EditingParametersV1) -> Image.Image:
    if parameters.saturation == 0 and parameters.vibrance == 0:
        return image
    if parameters.saturation != 0:
        factor = max(0.0, 1.0 + parameters.saturation / 100.0)
        image = ImageEnhance.Color(image).enhance(factor)
    if parameters.vibrance != 0:
        # Vibrance protects already saturated colours: blend the boosted image
        # in with a mask that fades out as chroma rises.
        factor = max(0.0, 1.0 + parameters.vibrance / 100.0 * 0.45)
        target = ImageEnhance.Color(image).enhance(factor)
        _hue, saturation, _value = image.convert("HSV").split()
        chroma_influence = saturation.point(
            [min(255, level * 4) for level in range(256)]
        )
        strength = abs(parameters.vibrance) / 100.0
        vibrance_mask = chroma_influence.point(
            [round((255 - level) * strength) for level in range(256)]
        )
        image = Image.composite(target, image, vibrance_mask)
    return image


# ---------------------------------------------------------------------------
# HSL: hue interpolated, bounded masked channel blends
# ---------------------------------------------------------------------------


def _apply_hsl_adjustments(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    if not any(
        adjustment.hue != 0
        or adjustment.saturation != 0
        or adjustment.luminance != 0
        for adjustment in parameters.hsl.values()
    ):
        return image

    hue, saturation, value = image.convert("HSV").split()
    chroma_influence = saturation.point([min(255, level * 4) for level in range(256)])

    hue_adjustments = _interpolated_hsl_lut(parameters, "hue")
    saturation_adjustments = _interpolated_hsl_lut(parameters, "saturation")
    luminance_adjustments = _interpolated_hsl_lut(parameters, "luminance")

    shifted_hue = hue.point(
        [
            (level + round(adjustment * _HSL_HUE_MAX_SHIFT / 100)) % 256
            for level, adjustment in enumerate(hue_adjustments)
        ]
    )
    adjusted_saturation = _apply_signed_channel_adjustment(
        saturation,
        hue,
        saturation_adjustments,
        chroma_influence,
        positive_headroom=_HSL_SATURATION_HEADROOM,
        negative_scale=_HSL_SATURATION_SCALE,
    )
    adjusted_value = _apply_signed_channel_adjustment(
        value,
        hue,
        luminance_adjustments,
        chroma_influence,
        positive_headroom=_HSL_LUMINANCE_HEADROOM,
        negative_scale=_HSL_LUMINANCE_SCALE,
    )
    return Image.merge("HSV", (shifted_hue, adjusted_saturation, adjusted_value)).convert(
        "RGB"
    )


def _apply_signed_channel_adjustment(
    channel: Image.Image,
    hue: Image.Image,
    adjustments: list[int],
    influence: Image.Image,
    *,
    positive_headroom: float,
    negative_scale: float,
) -> Image.Image:
    """Blend ``channel`` towards a *bounded* target where the hue mask asks for it.

    v3 composited towards pure white/black which destroyed detail; here the
    target keeps a fixed fraction of the available headroom and the mask is the
    hue interpolated adjustment multiplied by the chroma influence.
    """
    positive_mask = hue.point(
        [round(max(0, adjustment) / 100 * 255) for adjustment in adjustments]
    )
    negative_mask = hue.point(
        [round(max(0, -adjustment) / 100 * 255) for adjustment in adjustments]
    )
    positive_mask = ImageChops.multiply(positive_mask, influence)
    negative_mask = ImageChops.multiply(negative_mask, influence)

    positive_target = channel.point(
        [round(level + (255 - level) * positive_headroom) for level in range(256)]
    )
    negative_target = channel.point(
        [round(level * negative_scale) for level in range(256)]
    )
    adjusted = Image.composite(positive_target, channel, positive_mask)
    return Image.composite(negative_target, adjusted, negative_mask)


def _interpolated_hsl_lut(parameters: EditingParametersV1, field: str) -> list[int]:
    points = [
        (
            center,
            getattr(parameters.hsl.get(channel), field)
            if parameters.hsl.get(channel) is not None
            else 0,
        )
        for channel, center in _HSL_HUE_CENTERS
    ]
    points.append((256, points[0][1]))

    values: list[int] = []
    segment = 0
    for hue in range(256):
        while segment < len(points) - 2 and hue > points[segment + 1][0]:
            segment += 1
        left_position, left_value = points[segment]
        right_position, right_value = points[segment + 1]
        distance = right_position - left_position
        ratio = 0.0 if distance == 0 else (hue - left_position) / distance
        values.append(round(left_value + ((right_value - left_value) * ratio)))
    return values


# ---------------------------------------------------------------------------
# Colour grading: additive split toning through the tonal masks
# ---------------------------------------------------------------------------


def _apply_color_grading(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    wheels = (
        ("shadows", parameters.color_grading.shadows),
        ("midtones", parameters.color_grading.midtones),
        ("highlights", parameters.color_grading.highlights),
    )
    if all(wheel.saturation == 0 and wheel.luminance == 0 for _, wheel in wheels):
        return image

    tonal_masks = _color_grading_tonal_masks(image, parameters)
    result = image
    for region, wheel in wheels:
        if wheel.saturation == 0 and wheel.luminance == 0:
            continue
        mask = tonal_masks[region]
        saturation_fraction = wheel.saturation / 100.0
        luminance_delta = wheel.luminance / 100.0 * _GRADE_LUMA_LEVELS
        if wheel.saturation > 0:
            tint = colorsys.hsv_to_rgb((wheel.hue % 360) / 360.0, 1.0, 1.0)
        else:
            tint = (0.0, 0.0, 0.0)

        positive_channels: list[Image.Image] = []
        negative_channels: list[Image.Image] = []
        for tint_component in tint:
            tint_levels = saturation_fraction * tint_component * _GRADE_TINT_LEVELS
            delta_levels = tint_levels + luminance_delta
            # Per level of the tonal mask this is what gets added (positive) or
            # subtracted (negative).  Staying strictly additive keeps the
            # underlying image detail instead of repainting the region.
            positive_channels.append(
                mask.point(
                    [max(0, round(level / 255.0 * delta_levels)) for level in range(256)]
                )
            )
            negative_channels.append(
                mask.point(
                    [
                        max(0, round(-level / 255.0 * delta_levels))
                        for level in range(256)
                    ]
                )
            )
        result = ImageChops.add(result, Image.merge("RGB", positive_channels))
        result = ImageChops.subtract(result, Image.merge("RGB", negative_channels))
    return result


def _color_grading_tonal_masks(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> dict[str, Image.Image]:
    luminance = ImageOps.grayscale(image)
    balance_shift = parameters.color_grading.balance * 0.64
    transition_width = 0.12 + (parameters.color_grading.blending / 100 * 0.26)
    midtone_half_width = 0.22 + (parameters.color_grading.blending / 100 * 0.2)

    shadow_lut: list[int] = []
    midtone_lut: list[int] = []
    highlight_lut: list[int] = []
    for level in range(256):
        position = max(0.0, min(1.0, (level + balance_shift) / 255))
        shadow_weight = _smoothstep_weight(
            (0.5 + (transition_width / 2) - position) / transition_width
        )
        highlight_weight = _smoothstep_weight(
            (position - (0.5 - (transition_width / 2))) / transition_width
        )
        midtone_weight = _smoothstep_weight(
            1.0 - (abs(position - 0.5) / midtone_half_width)
        )
        shadow_lut.append(round(shadow_weight * 255))
        midtone_lut.append(round(midtone_weight * 255))
        highlight_lut.append(round(highlight_weight * 255))

    return {
        "shadows": luminance.point(shadow_lut),
        "midtones": luminance.point(midtone_lut),
        "highlights": luminance.point(highlight_lut),
    }


# ---------------------------------------------------------------------------
# Local contrast: clarity and texture
# ---------------------------------------------------------------------------


def _apply_clarity_and_texture(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    if parameters.clarity != 0:
        if parameters.clarity > 0:
            amount = parameters.clarity / 100.0
            # Large radius = local contrast (midtone separation), not a global
            # sharpness boost.
            detail = image.filter(
                ImageFilter.UnsharpMask(
                    radius=24, percent=round(parameters.clarity * 0.6), threshold=0
                )
            )
            image = Image.blend(image, detail, 0.55 * amount)
        else:
            amount = min(1.0, -parameters.clarity / 100.0)
            softened = image.filter(ImageFilter.GaussianBlur(radius=0.5 + 2.5 * amount))
            image = Image.blend(image, softened, 0.45 * amount)
    if parameters.texture != 0:
        if parameters.texture > 0:
            image = image.filter(
                ImageFilter.UnsharpMask(
                    radius=2,
                    percent=round(parameters.texture * 0.5),
                    threshold=3,
                )
            )
        else:
            amount = min(1.0, -parameters.texture / 100.0)
            softened = image.filter(ImageFilter.GaussianBlur(radius=0.4 + 0.8 * amount))
            image = Image.blend(image, softened, 0.5 * amount)
    return image


# ---------------------------------------------------------------------------
# Noise reduction and sharpening
# ---------------------------------------------------------------------------


def _apply_noise_reduction(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    luminance_amount = parameters.noise_reduction.luminance
    colour_amount = parameters.noise_reduction.color
    if luminance_amount <= 0 and colour_amount <= 0:
        return image

    advanced = parameters.advanced
    # Detail / contrast / smoothness modifiers.  Every factor is exactly 1.0 at
    # the AdvancedParameters defaults (50 / 0 / 50) and the 0.85 base strength
    # clamp keeps the v4 result untouched at those defaults.
    luminance_radius_factor = (
        1.0 + (50 - advanced.noise_luminance_detail) / 100.0 * 0.8
    )
    luminance_strength_factor = (
        1.0 + (50 - advanced.noise_luminance_detail) / 100.0 * 0.6
    )
    luminance_radius_factor *= 1.0 + advanced.noise_luminance_contrast / 100.0 * 0.25
    colour_radius_factor = (
        1.0
        + (50 - advanced.noise_color_detail) / 100.0 * 0.7
        + (advanced.noise_color_smoothness - 50) / 100.0 * 0.8
    )
    colour_strength_factor = (
        1.0
        + (advanced.noise_color_smoothness - 50) / 100.0 * 0.5
        - (50 - advanced.noise_color_detail) / 100.0 * 0.3
    )

    luminance, blue_difference, red_difference = image.convert("YCbCr").split()
    if luminance_amount > 0:
        radius = (
            0.35 + (luminance_amount / 100 * 1.8)
        ) * luminance_radius_factor
        filtered_luminance = luminance.filter(ImageFilter.GaussianBlur(radius=radius))
        strength = min(
            0.92,
            min(0.85, luminance_amount / 100 * 0.85) * luminance_strength_factor,
        )
        luminance = Image.blend(luminance, filtered_luminance, strength)
    if colour_amount > 0:
        colour_radius = (
            0.4 + (colour_amount / 100 * 2.2)
        ) * colour_radius_factor
        colour_strength = min(
            0.92,
            min(0.85, colour_amount / 100 * 0.85) * colour_strength_factor,
        )
        blue_difference = Image.blend(
            blue_difference,
            blue_difference.filter(ImageFilter.GaussianBlur(radius=colour_radius)),
            colour_strength,
        )
        red_difference = Image.blend(
            red_difference,
            red_difference.filter(ImageFilter.GaussianBlur(radius=colour_radius)),
            colour_strength,
        )
    return Image.merge(
        "YCbCr",
        (luminance, blue_difference, red_difference),
    ).convert("RGB")


def _apply_sharpening(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    amount = parameters.sharpening.amount
    if amount <= 0:
        return image
    radius = max(0.5, min(2.0, parameters.sharpening.radius))
    percent = max(0, min(150, amount))
    if percent == 0:
        return image
    return image.filter(
        ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=4)
    )


# ---------------------------------------------------------------------------
# Point curves and black & white grey mixers (v5)
# ---------------------------------------------------------------------------

_GRAY_MIXER_FIELDS = (
    "gray_mixer_red",
    "gray_mixer_orange",
    "gray_mixer_yellow",
    "gray_mixer_green",
    "gray_mixer_aqua",
    "gray_mixer_blue",
    "gray_mixer_purple",
    "gray_mixer_magenta",
)


def _curve_is_identity(curve: list[ToneCurvePoint]) -> bool:
    """Return True when a point curve leaves every level unchanged."""
    if len(curve) < 2:
        return True
    return all(point.x == point.y for point in curve)


def _build_curve_lut(points: list[ToneCurvePoint]) -> list[int]:
    """Expand point curve control points into a 256 entry lookup table."""
    anchors = sorted(
        ((float(point.x), float(point.y)) for point in points),
        key=lambda anchor: anchor[0],
    )
    if len(anchors) < 2:
        return list(range(256))

    lut: list[int] = []
    segment = 0
    for level in range(256):
        while segment < len(anchors) - 2 and level >= anchors[segment + 1][0]:
            segment += 1
        x0, y0 = anchors[segment]
        x1, y1 = anchors[segment + 1]
        if x1 <= x0:
            value = y1
        else:
            ratio = max(0.0, min(1.0, (level - x0) / (x1 - x0)))
            value = y0 + (y1 - y0) * ratio
        lut.append(max(0, min(255, int(round(value)))))
    return lut


def _apply_channel_curves(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    """Apply the optional per channel point curves (``None`` means identity)."""
    advanced = parameters.advanced
    curves = (advanced.curve_red, advanced.curve_green, advanced.curve_blue)
    if all(curve is None for curve in curves):
        return image

    channels: list[Image.Image] = []
    changed = False
    for channel, curve in zip(image.split(), curves):
        if curve is not None and not _curve_is_identity(curve):
            lut = _build_curve_lut(curve)
            if any(value != level for level, value in enumerate(lut)):
                channel = channel.point(lut)
                changed = True
        channels.append(channel)
    if not changed:
        return image
    return Image.merge("RGB", channels)


def _gray_mixer_lut(advanced: AdvancedParameters) -> list[int]:
    """Hue -> signed brightness bias for the eight grey mixers.

    Values are linearly interpolated around the hue circle between the same
    colour band centres the HSL panel uses.  128 means "leave the pixel alone".
    """
    values = [getattr(advanced, field) for field in _GRAY_MIXER_FIELDS]
    centers = [center for _name, center in _HSL_HUE_CENTERS]
    anchors = [(center - 256, value) for center, value in zip(centers, values)]
    anchors += list(zip(centers, values))
    anchors += [(center + 256, value) for center, value in zip(centers, values)]

    lut: list[int] = []
    segment = 0
    for hue in range(256):
        while segment < len(anchors) - 2 and hue >= anchors[segment + 1][0]:
            segment += 1
        x0, y0 = anchors[segment]
        x1, y1 = anchors[segment + 1]
        ratio = (hue - x0) / (x1 - x0)
        value = y0 + (y1 - y0) * ratio
        lut.append(max(1, min(255, 128 + int(round(value / 100.0 * 127.0)))))
    return lut


def _apply_black_and_white(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    """Convert to grey, reweighting brightness per hue band with the mixers."""
    advanced = parameters.advanced
    if not advanced.black_and_white:
        return image

    luminance = ImageOps.grayscale(image)
    mixer_values = [getattr(advanced, field) for field in _GRAY_MIXER_FIELDS]
    if any(mixer_values):
        hue, saturation, _value = image.convert("HSV").split()
        signed = hue.point(_gray_mixer_lut(advanced))
        positive = signed.point([max(0, level - 128) * 2 for level in range(256)])
        negative = signed.point([max(0, 128 - level) * 2 for level in range(256)])
        positive = ImageChops.multiply(positive, saturation).point(
            [round(level * _GRAY_MIXER_HEADROOM) for level in range(256)]
        )
        negative = ImageChops.multiply(negative, saturation).point(
            [round(level * _GRAY_MIXER_SCALE) for level in range(256)]
        )
        lighten = ImageChops.multiply(ImageOps.invert(luminance), positive)
        darken = ImageChops.multiply(luminance, negative)
        luminance = ImageChops.subtract(ImageChops.add(luminance, lighten), darken)
    return Image.merge("RGB", (luminance, luminance, luminance))


# ---------------------------------------------------------------------------
# Lens / post crop vignetting, defringe and grain (v5)
# ---------------------------------------------------------------------------


def _apply_lens_vignette(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    """Darken (amount < 0) or lighten (amount > 0) the corners radially."""
    advanced = parameters.advanced
    amount = advanced.lens_vignette_amount
    if amount == 0:
        return image

    radius = Image.radial_gradient("L").resize(
        image.size, Image.Resampling.BILINEAR
    )
    start = (
        _LENS_VIGNETTE_START_MIN
        + advanced.lens_vignette_midpoint / 100.0 * _LENS_VIGNETTE_START_RANGE
    )
    span = max(1e-6, 1.0 - start)
    falloff_lut = [
        0
        if level / 255.0 <= start
        else round(255 * _smoothstep_weight((level / 255.0 - start) / span))
        for level in range(256)
    ]
    strength = min(0.85, abs(amount) / 100.0)
    mask = radius.point(falloff_lut).point(
        [round(level * strength) for level in range(256)]
    )
    target = Image.new(
        "RGB", image.size, (0, 0, 0) if amount < 0 else (255, 255, 255)
    )
    return Image.composite(target, image, mask)


@lru_cache(maxsize=16)
def _post_crop_vignette_grid(
    midpoint: int,
    roundness: int,
    feather: int,
) -> bytes:
    """Build a small superellipse vignette mask (top-left origin, 255 = edge)."""
    size = _VIGNETTE_MASK_SIZE
    roundness_fraction = roundness / 100.0
    if roundness_fraction >= 0.0:
        exponent = 2.0 - 0.8 * roundness_fraction
    else:
        exponent = 2.0 + 5.0 * (-roundness_fraction)
    corner_radius = 2.0 ** (1.0 / exponent)
    start = 0.18 + midpoint / 100.0 * 0.60
    span = max(1e-6, 0.06 + feather / 100.0 * 0.70)
    half = size / 2.0

    grid = bytearray(size * size)
    for row in range(size):
        y = (row + 0.5 - half) / half
        for column in range(size):
            x = (column + 0.5 - half) / half
            radius = (abs(x) ** exponent + abs(y) ** exponent) ** (
                1.0 / exponent
            )
            radius /= corner_radius
            grid[row * size + column] = round(
                255 * _smoothstep_weight((radius - start) / span)
            )
    return bytes(grid)


def _apply_post_crop_vignette(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    """Darken (amount < 0) or lighten (amount > 0) the crop corners."""
    advanced = parameters.advanced
    amount = advanced.post_crop_vignette_amount
    if amount == 0:
        return image

    grid = _post_crop_vignette_grid(
        advanced.post_crop_vignette_midpoint,
        advanced.post_crop_vignette_roundness,
        advanced.post_crop_vignette_feather,
    )
    mask = Image.frombytes(
        "L", (_VIGNETTE_MASK_SIZE, _VIGNETTE_MASK_SIZE), grid
    ).resize(image.size, Image.Resampling.BILINEAR)
    strength = min(0.9, abs(amount) / 100.0)
    mask = mask.point([round(level * strength) for level in range(256)])
    target = Image.new(
        "RGB", image.size, (0, 0, 0) if amount < 0 else (255, 255, 255)
    )
    return Image.composite(target, image, mask)


def _defringe_hue_lut(
    amount: int,
    low: float,
    high: float,
) -> list[int]:
    """Hue -> desaturation blend amount, softly weighted around ``low..high``."""
    width = max(4.0, (high - low) * 0.25)
    lut: list[int] = []
    for hue in range(256):
        if hue < low:
            distance = low - hue
        elif hue > high:
            distance = hue - high
        else:
            distance = 0.0
        weight = _smoothstep_weight(1.0 - distance / width)
        lut.append(round(weight * amount / 100.0 * 255.0))
    return lut


def _defringe_band_bounds(
    center: int,
    low_slider: int,
    high_slider: int,
) -> tuple[float, float]:
    low = max(0.0, min(255.0, center + (low_slider - 50) * 1.5))
    high = max(0.0, min(255.0, center + (high_slider - 50) * 1.5))
    if high < low:
        low, high = high, low
    return low, high


def _apply_defringe(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    """Desaturate the purple / green fringe hue bands (approximation)."""
    advanced = parameters.advanced
    purple_amount = advanced.defringe_purple_amount
    green_amount = advanced.defringe_green_amount
    if purple_amount <= 0 and green_amount <= 0:
        return image

    hue, _saturation, _value = image.convert("HSV").split()
    mask = Image.new("L", image.size, 0)
    if purple_amount > 0:
        low, high = _defringe_band_bounds(
            192,
            advanced.defringe_purple_hue_lo,
            advanced.defringe_purple_hue_hi,
        )
        purple_mask = hue.point(_defringe_hue_lut(purple_amount, low, high))
        mask = ImageChops.lighter(mask, purple_mask)
    if green_amount > 0:
        low, high = _defringe_band_bounds(
            85,
            advanced.defringe_green_hue_lo,
            advanced.defringe_green_hue_hi,
        )
        green_mask = hue.point(_defringe_hue_lut(green_amount, low, high))
        mask = ImageChops.lighter(mask, green_mask)

    grey = ImageOps.grayscale(image)
    desaturated = Image.merge("RGB", (grey, grey, grey))
    return Image.composite(desaturated, image, mask)


def _grain_shape_lut(roughness: int, amount: int) -> list[int]:
    """Zero centred signed grain shape, biased by 128 for use with add."""
    amplitude = amount / 100.0 * _GRAIN_MAX_LEVELS
    gamma = 1.5 - roughness / 100.0
    lut: list[int] = []
    for level in range(256):
        signed = (level - 128) / 127.0
        shaped = math.copysign(abs(signed) ** gamma, signed)
        lut.append(max(0, min(255, 128 + int(round(shaped * amplitude)))))
    return lut


def _apply_grain(
    image: Image.Image,
    parameters: EditingParametersV1,
) -> Image.Image:
    """Add deterministic monochrome film grain.

    The seed is derived from the serialised parameters so the same parameter set
    always renders the same grain; ``grain_size`` only changes the blur radius
    of the noise tile, never the random draw.
    """
    advanced = parameters.advanced
    amount = advanced.grain_amount
    if amount <= 0:
        return image

    digest = hashlib.sha256(parameters.model_dump_json().encode("utf-8")).digest()
    generator = random.Random(int.from_bytes(digest[:8], "big"))
    tile_width = max(1, min(image.width, _GRAIN_BASE_SIZE))
    tile_height = max(1, min(image.height, _GRAIN_BASE_SIZE))
    tile = Image.frombytes(
        "L", (tile_width, tile_height), generator.randbytes(tile_width * tile_height)
    )
    # The tile is generated small and scaled up, so the noise stays cheap and
    # the same size slider gives the same visual grain scale at any resolution.
    scale = max(image.width / tile_width, image.height / tile_height)
    blur_radius = (0.3 + advanced.grain_size / 100.0 * 2.0) / scale
    tile = tile.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    tile = tile.point(_grain_shape_lut(advanced.grain_roughness, amount))
    if tile.size != image.size:
        tile = tile.resize(image.size, Image.Resampling.BILINEAR)
    noise = Image.merge("RGB", (tile, tile, tile))
    return ImageChops.add(image, noise, 1.0, -128)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _smoothstep_weight(value: float) -> float:
    clamped = max(0.0, min(1.0, value))
    return clamped * clamped * (3.0 - (2.0 * clamped))


def _clamp_channel(value: float) -> int:
    return int(max(0, min(255, round(value))))
