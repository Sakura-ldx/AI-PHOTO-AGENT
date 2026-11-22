"""Single source of truth for the Lightroom parameter surface.

Every Lightroom Classic "Develop" slider that this project supports is described
here exactly once.  The domain model (``app.schemas.AdvancedParameters``), the
Lightroom develop-settings mapping, the XMP preset writer, the preview renderer
and the two UI parameter summaries all read this table, so a new slider only has
to be added in one place.

The ``lr_key`` / ``xmp_attr`` names follow Lightroom's Camera Raw namespace; see
https://exiv2.org/tags-xmp-crs.html for the public list.  Keys that are known to
exist in Lightroom but are missing from that public list are marked
``VERIFY_KEYS`` and are confirmed on a real machine by the plugin diagnostic
(``AI Photo Agent - 核对参数写入``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ParameterKind = Literal["int", "bool", "enum", "curve", "number"]
PreviewSupport = Literal["exact", "approx", "none"]

# Keys that the installed Lightroom is expected to accept but that are not in
# the public Camera Raw schema list.  They are reported by the plugin diagnostic
# so a version difference shows up as a named mismatch instead of silence.
VERIFY_KEYS = frozenset(
    {
        "GrainFrequency",
        "PerspectiveX",
        "PerspectiveY",
        "PerspectiveAspect",
        "PerspectiveRotate",
        "PerspectiveScale",
    }
)

IDENTITY_CURVE: tuple[tuple[int, int], ...] = ((0, 0), (128, 128), (255, 255))


@dataclass(frozen=True)
class ParameterSpec:
    key: str
    lr_key: str
    group: str
    kind: ParameterKind
    minimum: float
    maximum: float
    neutral: float
    preview: PreviewSupport
    enum_map: tuple[tuple[str, int], ...] | None = None
    label: str = ""

    @property
    def xmp_attr(self) -> str:
        return self.lr_key

    @property
    def needs_verification(self) -> bool:
        return self.lr_key in VERIFY_KEYS


def _int(
    key: str,
    lr_key: str,
    group: str,
    minimum: int,
    maximum: int,
    neutral: int,
    preview: PreviewSupport,
    label: str,
) -> ParameterSpec:
    return ParameterSpec(
        key=key,
        lr_key=lr_key,
        group=group,
        kind="int",
        minimum=minimum,
        maximum=maximum,
        neutral=neutral,
        preview=preview,
        label=label,
    )


def _bool(key: str, lr_key: str, group: str, preview: PreviewSupport, label: str) -> ParameterSpec:
    return ParameterSpec(
        key=key,
        lr_key=lr_key,
        group=group,
        kind="bool",
        minimum=0,
        maximum=1,
        neutral=0,
        preview=preview,
        label=label,
    )


def _curve(key: str, lr_key: str, label: str) -> ParameterSpec:
    return ParameterSpec(
        key=key,
        lr_key=lr_key,
        group="tone_curve",
        kind="curve",
        minimum=0,
        maximum=255,
        neutral=0,
        preview="exact",
        label=label,
    )


def _enum(
    key: str,
    lr_key: str,
    group: str,
    options: tuple[tuple[str, int], ...],
    label: str,
) -> ParameterSpec:
    return ParameterSpec(
        key=key,
        lr_key=lr_key,
        group=group,
        kind="enum",
        minimum=0,
        maximum=max(value for _, value in options),
        neutral=options[0][1],
        preview="none",
        enum_map=options,
        label=label,
    )


_GRAY_MIXER_LABELS = {
    "red": "灰阶红",
    "orange": "灰阶橙",
    "yellow": "灰阶黄",
    "green": "灰阶绿",
    "aqua": "灰阶青",
    "blue": "灰阶蓝",
    "purple": "灰阶紫",
    "magenta": "灰阶品红",
}


PARAMETER_SPECS: tuple[ParameterSpec, ...] = (
    # --- 基本：黑白转换与灰阶混色 -------------------------------------------
    _bool("black_and_white", "ConvertToGrayscale", "basic", "approx", "黑白"),
    *(
        _int(
            f"gray_mixer_{channel}",
            f"GrayMixer{channel.capitalize()}",
            "basic",
            -100,
            100,
            0,
            "approx",
            _GRAY_MIXER_LABELS[channel],
        )
        for channel in (
            "red",
            "orange",
            "yellow",
            "green",
            "aqua",
            "blue",
            "purple",
            "magenta",
        )
    ),
    # --- 校准 ---------------------------------------------------------------
    _int("shadow_tint", "ShadowTint", "calibration", -100, 100, 0, "none", "阴影色调"),
    _int("red_hue", "RedHue", "calibration", -100, 100, 0, "none", "红原色色相"),
    _int("red_saturation", "RedSaturation", "calibration", -100, 100, 0, "none", "红原色饱和"),
    _int("green_hue", "GreenHue", "calibration", -100, 100, 0, "none", "绿原色色相"),
    _int("green_saturation", "GreenSaturation", "calibration", -100, 100, 0, "none", "绿原色饱和"),
    _int("blue_hue", "BlueHue", "calibration", -100, 100, 0, "none", "蓝原色色相"),
    _int("blue_saturation", "BlueSaturation", "calibration", -100, 100, 0, "none", "蓝原色饱和"),
    # --- 色调曲线：参数曲线与分通道曲线 -------------------------------------
    _int("parametric_shadows", "ParametricShadows", "tone_curve", -100, 100, 0, "exact", "曲线阴影"),
    _int("parametric_darks", "ParametricDarks", "tone_curve", -100, 100, 0, "exact", "曲线暗部"),
    _int("parametric_lights", "ParametricLights", "tone_curve", -100, 100, 0, "exact", "曲线亮部"),
    _int(
        "parametric_highlights",
        "ParametricHighlights",
        "tone_curve",
        -100,
        100,
        0,
        "exact",
        "曲线高光",
    ),
    _int(
        "parametric_shadow_split",
        "ParametricShadowSplit",
        "tone_curve",
        0,
        100,
        25,
        "approx",
        "曲线阴影分割",
    ),
    _int(
        "parametric_midtone_split",
        "ParametricMidtoneSplit",
        "tone_curve",
        0,
        100,
        50,
        "approx",
        "曲线中间调分割",
    ),
    _int(
        "parametric_highlight_split",
        "ParametricHighlightSplit",
        "tone_curve",
        0,
        100,
        75,
        "approx",
        "曲线高光分割",
    ),
    _curve("curve_red", "ToneCurvePV2012Red", "红色曲线"),
    _curve("curve_green", "ToneCurvePV2012Green", "绿色曲线"),
    _curve("curve_blue", "ToneCurvePV2012Blue", "蓝色曲线"),
    # --- 细节：降噪细节修饰 -------------------------------------------------
    _int(
        "noise_luminance_detail",
        "LuminanceNoiseReductionDetail",
        "detail",
        0,
        100,
        50,
        "approx",
        "明亮度细节",
    ),
    _int(
        "noise_luminance_contrast",
        "LuminanceNoiseReductionContrast",
        "detail",
        0,
        100,
        0,
        "approx",
        "明亮度对比",
    ),
    _int(
        "noise_color_detail",
        "ColorNoiseReductionDetail",
        "detail",
        0,
        100,
        50,
        "approx",
        "颜色细节",
    ),
    _int(
        "noise_color_smoothness",
        "ColorNoiseReductionSmoothness",
        "detail",
        0,
        100,
        50,
        "approx",
        "颜色平滑度",
    ),
    # --- 镜头 ---------------------------------------------------------------
    _bool("auto_lateral_ca", "AutoLateralCA", "optics", "none", "移除色差"),
    _int(
        "lens_manual_distortion",
        "LensManualDistortionAmount",
        "optics",
        -100,
        100,
        0,
        "none",
        "畸变校正",
    ),
    _int(
        "lens_vignette_amount",
        "VignetteAmount",
        "optics",
        -100,
        100,
        0,
        "approx",
        "镜头暗角",
    ),
    _int(
        "lens_vignette_midpoint",
        "VignetteMidpoint",
        "optics",
        0,
        100,
        50,
        "approx",
        "镜头暗角中点",
    ),
    _int(
        "defringe_purple_amount",
        "DefringePurpleAmount",
        "optics",
        0,
        100,
        0,
        "approx",
        "紫边去除",
    ),
    _int("defringe_purple_hue_lo", "DefringePurpleHueLo", "optics", 0, 100, 30, "none", "紫边色相下界"),
    _int("defringe_purple_hue_hi", "DefringePurpleHueHi", "optics", 0, 100, 70, "none", "紫边色相上界"),
    _int(
        "defringe_green_amount",
        "DefringeGreenAmount",
        "optics",
        0,
        100,
        0,
        "approx",
        "绿边去除",
    ),
    _int("defringe_green_hue_lo", "DefringeGreenHueLo", "optics", 0, 100, 40, "none", "绿边色相下界"),
    _int("defringe_green_hue_hi", "DefringeGreenHueHi", "optics", 0, 100, 60, "none", "绿边色相上界"),
    # --- 变换 ---------------------------------------------------------------
    _enum(
        "perspective_upright",
        "PerspectiveUpright",
        "transform",
        (("off", 0), ("auto", 1), ("level", 2), ("vertical", 3), ("full", 4)),
        "Upright",
    ),
    _int(
        "perspective_vertical",
        "PerspectiveVertical",
        "transform",
        -100,
        100,
        0,
        "none",
        "垂直透视",
    ),
    _int(
        "perspective_horizontal",
        "PerspectiveHorizontal",
        "transform",
        -100,
        100,
        0,
        "none",
        "水平透视",
    ),
    ParameterSpec(
        key="perspective_rotate",
        lr_key="PerspectiveRotate",
        group="transform",
        # Lightroom accepts a fractional rotation; encoding it as an int used to
        # round 0.4 degrees down to "neutral" and silently drop the adjustment.
        kind="number",
        minimum=-10.0,
        maximum=10.0,
        neutral=0.0,
        preview="none",
        label="旋转",
    ),
    _int("perspective_aspect", "PerspectiveAspect", "transform", -100, 100, 0, "none", "长宽比"),
    _int("perspective_scale", "PerspectiveScale", "transform", 50, 150, 100, "none", "缩放"),
    _int("perspective_x", "PerspectiveX", "transform", -100, 100, 0, "none", "X 轴位移"),
    _int("perspective_y", "PerspectiveY", "transform", -100, 100, 0, "none", "Y 轴位移"),
    # --- 效果 ---------------------------------------------------------------
    _int(
        "post_crop_vignette_amount",
        "PostCropVignetteAmount",
        "effects",
        -100,
        100,
        0,
        "approx",
        "暗角强度",
    ),
    _int(
        "post_crop_vignette_midpoint",
        "PostCropVignetteMidpoint",
        "effects",
        0,
        100,
        50,
        "approx",
        "暗角中点",
    ),
    _int(
        "post_crop_vignette_roundness",
        "PostCropVignetteRoundness",
        "effects",
        -100,
        100,
        0,
        "approx",
        "暗角圆度",
    ),
    _int(
        "post_crop_vignette_feather",
        "PostCropVignetteFeather",
        "effects",
        0,
        100,
        50,
        "approx",
        "暗角羽化",
    ),
    _int(
        "post_crop_vignette_highlights",
        "PostCropVignetteHighlightContrast",
        "effects",
        0,
        100,
        0,
        "approx",
        "暗角高光",
    ),
    _enum(
        "post_crop_vignette_style",
        "PostCropVignetteStyle",
        "effects",
        (("highlight", 0), ("color", 1), ("paint", 2)),
        "暗角样式",
    ),
    _int("grain_amount", "GrainAmount", "effects", 0, 100, 0, "approx", "颗粒数量"),
    _int("grain_size", "GrainSize", "effects", 0, 100, 25, "approx", "颗粒大小"),
    _int("grain_roughness", "GrainFrequency", "effects", 0, 100, 50, "approx", "颗粒粗糙度"),
)

SPECS_BY_KEY: dict[str, ParameterSpec] = {spec.key: spec for spec in PARAMETER_SPECS}
SPECS_BY_LR_KEY: dict[str, ParameterSpec] = {spec.lr_key: spec for spec in PARAMETER_SPECS}
SCALAR_SPECS: tuple[ParameterSpec, ...] = tuple(
    spec for spec in PARAMETER_SPECS if spec.kind in {"int", "bool", "enum", "number"}
)
CURVE_SPECS: tuple[ParameterSpec, ...] = tuple(
    spec for spec in PARAMETER_SPECS if spec.kind == "curve"
)
GROUPS: tuple[str, ...] = (
    "basic",
    "calibration",
    "tone_curve",
    "detail",
    "optics",
    "transform",
    "effects",
)

# Keys the vision model may propose through the compact ``overrides`` channel.
# Geometric transforms are excluded on purpose: a vision model cannot infer a
# keystone correction from a photo and a wrong guess is destructive.
MODEL_OVERRIDE_KEYS: tuple[str, ...] = tuple(
    spec.key
    for spec in SPECS_BY_KEY.values()
    if spec.group in {"basic", "tone_curve", "detail", "optics", "effects"}
)


def advanced_defaults() -> dict[str, Any]:
    """Neutral value for every advanced parameter."""

    defaults: dict[str, Any] = {}
    for spec in PARAMETER_SPECS:
        if spec.kind == "bool":
            defaults[spec.key] = False
        elif spec.kind == "enum":
            defaults[spec.key] = spec.enum_map[0][0] if spec.enum_map else ""
        elif spec.kind == "curve":
            defaults[spec.key] = None
        elif spec.kind == "number":
            defaults[spec.key] = float(spec.neutral)
        else:
            defaults[spec.key] = int(spec.neutral)
    return defaults


def clamp_value(spec: ParameterSpec, value: float) -> float:
    if value < spec.minimum:
        return spec.minimum
    if value > spec.maximum:
        return spec.maximum
    return value


def encode_for_lightroom(spec: ParameterSpec, value: Any) -> int | float | str | list[int]:
    """Convert a domain value into what ``applyDevelopSettings``/XMP expects."""

    if spec.kind == "bool":
        return 1 if value else 0
    if spec.kind == "enum":
        if spec.enum_map is None:
            return 0
        mapping = dict(spec.enum_map)
        return mapping.get(str(value), spec.enum_map[0][1])
    if spec.kind == "curve":
        flattened: list[int] = []
        for x, y in _iter_curve(value):
            flattened.extend((int(x), int(y)))
        return flattened
    if spec.kind == "number":
        return round(float(value), 1)
    return round(float(value))


def _iter_curve(value: Any) -> list[tuple[int, int]]:
    if not value:
        return list(IDENTITY_CURVE)
    points: list[tuple[int, int]] = []
    for item in value:
        if isinstance(item, dict):
            points.append((int(item["x"]), int(item["y"])))
        elif hasattr(item, "x") and hasattr(item, "y"):
            points.append((int(item.x), int(item.y)))
        else:
            points.append((int(item[0]), int(item[1])))
    return points


def is_neutral(spec: ParameterSpec, value: Any) -> bool:
    if spec.kind == "bool":
        return bool(value) is False
    if spec.kind == "enum":
        return spec.enum_map is None or str(value) == spec.enum_map[0][0]
    if spec.kind == "curve":
        return not value or _iter_curve(value) == list(IDENTITY_CURVE)
    if spec.kind == "number":
        return abs(float(value) - spec.neutral) < 0.05
    return int(value) == int(spec.neutral)


def override_key_list_text(width: int = 96) -> str:
    """Comma separated model-facing key list, wrapped for the prompt."""

    words = ", ".join(MODEL_OVERRIDE_KEYS)
    lines: list[str] = []
    current = ""
    for word in words.split(", "):
        candidate = f"{current}, {word}" if current else word
        if len(candidate) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\n".join(f"  - {line}" for line in lines)
