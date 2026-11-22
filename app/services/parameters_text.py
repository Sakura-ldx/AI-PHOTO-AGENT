"""Human readable parameter summaries shared by the web UI and the Lightroom plugin.

Both front ends need to tell the user which values will actually be applied. Keeping
the wording here means a newly supported Lightroom slider shows up everywhere at
once instead of drifting between JavaScript and Lua copies.
"""

from __future__ import annotations

from app.schemas import EditingParametersV1
from app.services.parameter_specs import (
    GROUPS,
    PARAMETER_SPECS,
    SPECS_BY_KEY,
    is_neutral,
)

SCALAR_FIELDS: tuple[tuple[str, str, str, int], ...] = (
    ("exposure", "曝光", " EV", 2),
    ("contrast", "对比度", "", 0),
    ("highlights", "高光", "", 0),
    ("shadows", "阴影", "", 0),
    ("whites", "白色", "", 0),
    ("blacks", "黑色", "", 0),
    ("temperature", "色温", "", 0),
    ("tint", "色调", "", 0),
    ("vibrance", "自然饱和度", "", 0),
    ("saturation", "饱和度", "", 0),
    ("clarity", "清晰度", "", 0),
    ("dehaze", "去雾", "", 0),
    ("texture", "纹理", "", 0),
)

GROUP_TITLES: dict[str, str] = {
    "basic": "黑白",
    "calibration": "校准",
    "tone_curve": "曲线",
    "detail": "降噪细节",
    "optics": "镜头",
    "transform": "变换",
    "effects": "效果",
}


def _number(value: float, digits: int) -> str:
    if digits <= 0:
        return f"{round(value):+d}"
    return f"{value:+.{digits}f}"


def _curve_is_custom(parameters: EditingParametersV1) -> bool:
    """A curve is neutral when every point sits on the identity diagonal."""

    return any(point.x != point.y for point in parameters.tone_curve)


def _hsl_channels(parameters: EditingParametersV1) -> int:
    count = 0
    for adjustment in parameters.hsl.values():
        if adjustment.hue or adjustment.saturation or adjustment.luminance:
            count += 1
    return count


def _grading_used(parameters: EditingParametersV1) -> bool:
    grading = parameters.color_grading
    if grading.balance:
        return True
    return any(
        wheel.saturation or wheel.luminance
        for wheel in (grading.shadows, grading.midtones, grading.highlights)
    )


def _advanced_sections(parameters: EditingParametersV1) -> list[str]:
    sections: list[str] = []
    for group in GROUPS:
        labels = [
            SPECS_BY_KEY[spec.key].label or spec.key
            for spec in PARAMETER_SPECS
            if spec.group == group
            and not is_neutral(spec, getattr(parameters.advanced, spec.key))
        ]
        if labels:
            sections.append(f"{GROUP_TITLES.get(group, group)}：{'、'.join(labels)}")
    return sections


def describe_parameters(parameters: EditingParametersV1) -> str:
    """Return a compact one line summary of the non neutral adjustments."""

    parts: list[str] = []
    for field, label, unit, digits in SCALAR_FIELDS:
        value = getattr(parameters, field)
        if value:
            parts.append(f"{label} {_number(value, digits)}{unit}")

    if _curve_is_custom(parameters):
        parts.append("色调曲线")
    hsl_count = _hsl_channels(parameters)
    if hsl_count:
        parts.append(f"HSL {hsl_count} 个色相")
    if _grading_used(parameters):
        parts.append("色彩分级")
    if parameters.sharpening.amount:
        parts.append(f"锐化 {round(parameters.sharpening.amount)}")
    if parameters.noise_reduction.luminance or parameters.noise_reduction.color:
        parts.append(
            f"降噪 {round(parameters.noise_reduction.luminance)}"
            f"/{round(parameters.noise_reduction.color)}"
        )

    advanced = _advanced_sections(parameters)
    if advanced:
        parts.append("附加 " + "；".join(advanced))

    return " · ".join(parts) if parts else "没有需要应用的调整"
