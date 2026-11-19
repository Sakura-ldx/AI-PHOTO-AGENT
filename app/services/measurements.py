"""Quantitative image measurements used to ground editing decisions.

The vision model sees a downscaled JPEG and returns slider values, but it never
measures a histogram. Every number it produces is therefore a guess, and the
guesses are systematically timid. This module supplies the missing evidence:
exposure level, tonal distribution, clipping, white balance cast and
saturation, all computed with Pillow only (the project has no numpy).

Two consumers:

* :mod:`app.services.edit_resolver` uses the measurements to add a base
  correction on top of the model's intent, and to verify the rendered result.
* :mod:`app.adapters.vision_models` embeds :func:`describe_for_prompt` so the
  model reasons about numbers instead of impressions.

Everything is 8-bit 0-255 because the renderer and the Lightroom mapping both
speak that scale. Measurements are gathered on a 512px copy so the closed loop
stays cheap enough to run once per style.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

from app.imaging import flatten_to_rgb


MEASUREMENT_VERSION = "image_measurements_v1"
MEASUREMENT_MAX_SIZE = (512, 512)

# A pixel at 0/1/2 is treated as crushed black; 253+ as blown white. Anything
# stricter reports 0% on almost every real photo and stops being informative.
BLACK_CLIP_LEVEL = 2
WHITE_CLIP_LEVEL = 253

# Pixels this desaturated are plausible neutral surfaces (grey concrete, white
# walls) and therefore usable for a grey-world white balance estimate.
NEUTRAL_MAX_SATURATION = 42
NEUTRAL_MIN_VALUE = 24
NEUTRAL_MAX_VALUE = 248

# Middle grey in linear light. Used to translate a luma percentile into an EV
# suggestion, not as a target the resolver must reach.
MID_GREY_LINEAR = 0.18

# Below this share of near-neutral pixels the grey-world estimate describes the
# whole scene rather than actual neutrals, so it is reported with lower trust.
WEAK_NEUTRAL_COVERAGE = 0.02


@dataclass(frozen=True)
class ChannelStats:
    """Histogram summary of a single 8-bit channel."""

    mean: float
    std: float
    p01: int
    p05: int
    p25: int
    p50: int
    p75: int
    p95: int
    p99: int
    clip_low: float
    clip_high: float

    @property
    def range_90(self) -> int:
        return self.p95 - self.p05


@dataclass(frozen=True)
class ImageMeasurements:
    """Measurement report for one image, suitable for prompts and resolution."""

    version: str
    width: int
    height: int
    luma: ChannelStats
    red: ChannelStats
    green: ChannelStats
    blue: ChannelStats
    saturation_mean: float
    chroma_mean: float
    neutral_red_ratio: float
    neutral_blue_ratio: float
    neutral_coverage: float
    deep_shadow_share: float
    bright_highlight_share: float
    flat_share: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clipped_shadows_share(self) -> float:
        return self.luma.clip_low

    @property
    def clipped_highlights_share(self) -> float:
        return self.luma.clip_high

    @property
    def is_low_key(self) -> bool:
        return self.luma.p50 < 60

    @property
    def is_high_key(self) -> bool:
        return self.luma.p50 > 165

    @property
    def mid_tone_ev_delta(self) -> float:
        """How many stops the mid tone sits from middle grey (positive = darker)."""

        mid_linear = (self.luma.p50 / 255.0) ** 2.2
        if mid_linear <= 1e-6:
            return 4.0
        return math.log2(MID_GREY_LINEAR / mid_linear)

    @property
    def warm_bias(self) -> float:
        """Positive means the neutral surfaces are warmer than neutral."""

        return self.neutral_red_ratio / max(self.neutral_blue_ratio, 1e-6)


def measure_file(
    path: Path,
    *,
    max_size: tuple[int, int] = MEASUREMENT_MAX_SIZE,
) -> ImageMeasurements:
    """Measure an image on disk, downscaling first to keep the cost bounded."""

    with Image.open(path) as source:
        image = flatten_to_rgb(ImageOps.exif_transpose(source))
        image.thumbnail(max_size)
        return measure_image(image)


def measure_image(image: Image.Image) -> ImageMeasurements:
    """Measure an already decoded image without modifying it."""

    rgb = image if image.mode == "RGB" else flatten_to_rgb(image)
    luma = ImageOps.grayscale(rgb)
    histogram = luma.histogram()
    total = max(1, rgb.width * rgb.height)

    luma_stats = _channel_stats(luma, clip_low=BLACK_CLIP_LEVEL, clip_high=WHITE_CLIP_LEVEL)
    red, green, blue = rgb.split()
    red_stats = _channel_stats(red, clip_low=BLACK_CLIP_LEVEL, clip_high=WHITE_CLIP_LEVEL)
    green_stats = _channel_stats(green, clip_low=BLACK_CLIP_LEVEL, clip_high=WHITE_CLIP_LEVEL)
    blue_stats = _channel_stats(blue, clip_low=BLACK_CLIP_LEVEL, clip_high=WHITE_CLIP_LEVEL)

    saturation_mean = float(ImageStat.Stat(rgb.convert("HSV")).mean[1])
    red_ratio, blue_ratio, neutral_coverage = _neutral_balance(rgb, green_stats.mean)

    notes = _build_notes(
        luma=luma_stats,
        red_ratio=red_ratio,
        blue_ratio=blue_ratio,
        saturation_mean=saturation_mean,
        clipped_shadows=luma_stats.clip_low,
        clipped_highlights=luma_stats.clip_high,
    )

    return ImageMeasurements(
        version=MEASUREMENT_VERSION,
        width=rgb.width,
        height=rgb.height,
        luma=luma_stats,
        red=red_stats,
        green=green_stats,
        blue=blue_stats,
        saturation_mean=saturation_mean,
        chroma_mean=_chroma_mean(rgb),
        neutral_red_ratio=red_ratio,
        neutral_blue_ratio=blue_ratio,
        neutral_coverage=neutral_coverage,
        deep_shadow_share=sum(histogram[:17]) / total,
        bright_highlight_share=sum(histogram[240:]) / total,
        flat_share=_flat_share(histogram, luma_stats.p50, total),
        notes=notes,
    )


def describe_for_prompt(measurements: ImageMeasurements) -> str:
    """Render measurements as a short deterministic block for the model prompt.

    The wording names the consequence ("偏暖", "暗部压死") as well as the number,
    because the model has to translate evidence into slider values and benefits
    from the conclusion being spelled out.
    """

    luma = measurements.luma
    lines = [
        "图像量化测量（由程序计算，可信；0-255 尺度）：",
        f"- 亮度：均值 {luma.mean:.1f}，中位 {luma.p50}，标准差 {luma.std:.1f}",
        f"- 影调分布：p1={luma.p01} p5={luma.p05} p25={luma.p25} p75={luma.p75} "
        f"p95={luma.p95} p99={luma.p99}",
        f"- 有效影调范围（p95-p05）：{luma.range_90}",
        f"- 剪切：黑场 {luma.clip_low * 100:.2f}%，白场 {luma.clip_high * 100:.2f}%",
        f"- 暗部占比（<=16）：{measurements.deep_shadow_share * 100:.1f}%；"
        f"高光占比（>=240）：{measurements.bright_highlight_share * 100:.1f}%",
        f"- 中性色偏：R/G={measurements.neutral_red_ratio:.2f}，"
        f"B/G={measurements.neutral_blue_ratio:.2f}"
        f"（覆盖 {measurements.neutral_coverage * 100:.0f}% 像素）",
        f"- 饱和度均值：{measurements.saturation_mean:.0f}/255",
        f"- 中调相对 18% 灰：{measurements.mid_tone_ev_delta:+.2f} EV（正值=偏暗，需要提亮）",
    ]
    for note in measurements.notes:
        lines.append(f"- 判断：{note}")
    return "\n".join(lines)


def _channel_stats(channel: Image.Image, *, clip_low: int, clip_high: int) -> ChannelStats:
    histogram = channel.histogram()
    total = max(1, sum(histogram))
    mean, std = _mean_std(histogram, total)
    return ChannelStats(
        mean=mean,
        std=std,
        p01=_percentile(histogram, total, 0.01),
        p05=_percentile(histogram, total, 0.05),
        p25=_percentile(histogram, total, 0.25),
        p50=_percentile(histogram, total, 0.50),
        p75=_percentile(histogram, total, 0.75),
        p95=_percentile(histogram, total, 0.95),
        p99=_percentile(histogram, total, 0.99),
        clip_low=sum(histogram[: clip_low + 1]) / total,
        clip_high=sum(histogram[clip_high:]) / total,
    )


def _mean_std(histogram: list[int], total: int) -> tuple[float, float]:
    mean = sum(level * count for level, count in enumerate(histogram)) / total
    variance = (
        sum((level - mean) ** 2 * count for level, count in enumerate(histogram)) / total
    )
    return mean, math.sqrt(max(0.0, variance))


def _percentile(histogram: list[int], total: int, fraction: float) -> int:
    """Return the level at ``fraction`` of the distribution (0-255)."""

    target = max(1, round(total * fraction))
    cumulative = 0
    for level, count in enumerate(histogram):
        cumulative += count
        if cumulative >= target:
            return level
    return 255


def _chroma_mean(rgb: Image.Image) -> float:
    """Mean (max-min) per pixel: how far pixels sit from grey on average."""

    red, green, blue = rgb.split()
    high = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    low = ImageChops.darker(ImageChops.darker(red, green), blue)
    return float(ImageStat.Stat(ImageChops.subtract(high, low)).mean[0])


def _neutral_balance(rgb: Image.Image, green_mean: float) -> tuple[float, float, float]:
    """Estimate the white balance cast from desaturated pixels.

    Returns ``(red_to_green, blue_to_green, coverage)``. When almost no pixel is
    plausibly neutral (a sunset, a neon-lit frame) the whole-image average is
    used instead and ``coverage`` is reported as 0 so callers know the estimate
    is weak.
    """

    _hue, saturation, value = rgb.convert("HSV").split()
    desaturated = saturation.point(lambda level: 255 if level <= NEUTRAL_MAX_SATURATION else 0)
    in_range = value.point(lambda level: 255 if NEUTRAL_MIN_VALUE <= level <= NEUTRAL_MAX_VALUE else 0)
    mask = ImageChops.darker(desaturated, in_range)
    coverage = _mask_coverage(mask)

    if coverage < WEAK_NEUTRAL_COVERAGE:
        red_mean, green_mean, blue_mean = ImageStat.Stat(rgb).mean
        coverage = 0.0
    else:
        red_mean, green_mean, blue_mean = ImageStat.Stat(rgb, mask.convert("L")).mean

    return (
        red_mean / max(green_mean, 1e-6),
        blue_mean / max(green_mean, 1e-6),
        coverage,
    )


def _mask_coverage(mask: Image.Image) -> float:
    histogram = mask.histogram()
    total = max(1, sum(histogram))
    return histogram[255] / total


def _flat_share(histogram: list[int], median: int, total: int) -> float:
    """Share of pixels concentrated in a narrow band around the median."""

    low = max(0, median - 8)
    high = min(255, median + 8)
    return sum(histogram[low : high + 1]) / total


def _build_notes(
    *,
    luma: ChannelStats,
    red_ratio: float,
    blue_ratio: float,
    saturation_mean: float,
    clipped_shadows: float,
    clipped_highlights: float,
) -> tuple[str, ...]:
    notes: list[str] = []
    if luma.p50 < 45:
        notes.append("整体偏暗（中位低于 45），优先提亮而不是继续压暗")
    elif luma.p50 > 175:
        notes.append("整体偏亮（中位高于 175），优先收敛高光")

    if luma.p95 < 120:
        notes.append("高光端没有到达亮部，画面发闷，需要抬高白点")
    if luma.p05 > 45:
        notes.append("暗部没有被压下去，黑场浮起，画面容易发灰")
    if luma.range_90 < 90:
        notes.append("影调范围被压缩，需要增加对比度")

    if red_ratio > 1.12:
        notes.append(f"中性色明显偏暖（R/G={red_ratio:.2f}），需要降低色温")
    elif red_ratio < 0.88:
        notes.append(f"中性色明显偏冷（R/G={red_ratio:.2f}），需要提高色温")
    if blue_ratio < 0.75:
        notes.append(f"蓝通道偏弱（B/G={blue_ratio:.2f}），阴影容易偏黄")
    elif blue_ratio > 1.3:
        notes.append(f"蓝通道偏强（B/G={blue_ratio:.2f}），阴影容易偏蓝")

    if clipped_shadows > 0.02:
        notes.append(f"有 {clipped_shadows * 100:.1f}% 像素已压成纯黑，提亮要克制")
    if clipped_highlights > 0.01:
        notes.append(f"有 {clipped_highlights * 100:.1f}% 像素已过曝，必须回收高光")

    if saturation_mean > 150:
        notes.append("饱和度整体偏高，注意肤色和橙色容易溢出")
    elif saturation_mean < 50:
        notes.append("饱和度整体偏低，可以适度提高鲜艳度")
    return tuple(notes)
