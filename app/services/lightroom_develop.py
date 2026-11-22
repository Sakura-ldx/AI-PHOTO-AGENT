from __future__ import annotations

from typing import Any

from app.schemas import DEVELOP_SETTINGS_VERSION, HSL_CHANNELS, EditingParametersV1
from app.services.parameter_specs import (
    PARAMETER_SPECS,
    encode_for_lightroom,
    is_neutral,
)

# These keys are accepted by LrPhoto:applyDevelopSettings (Lightroom SDK 6+).
# The tone curve is represented as a flat array: { x1, y1, x2, y2, ... }.
LIGHTROOM_PROCESS_VERSION = "15.4"


def build_lightroom_develop_settings(
    parameters: EditingParametersV1,
) -> dict[str, Any]:
    settings: dict[str, Any] = {
        "ProcessVersion": LIGHTROOM_PROCESS_VERSION,
    }
    if not parameters.has_adjustments():
        return settings

    settings.update(
        {
            "Exposure2012": round(parameters.exposure, 2),
            "Contrast2012": round(parameters.contrast),
            "Highlights2012": round(parameters.highlights),
            "Shadows2012": round(parameters.shadows),
            "Whites2012": round(parameters.whites),
            "Blacks2012": round(parameters.blacks),
            "IncrementalTemperature": round(parameters.temperature),
            "IncrementalTint": round(parameters.tint),
            "Vibrance": round(parameters.vibrance),
            "Saturation": round(parameters.saturation),
            "Texture": round(parameters.texture),
            "Clarity2012": round(parameters.clarity),
            "Dehaze": round(parameters.dehaze),
            "ToneCurveName2012": "Custom",
            "ToneCurvePV2012": _flat_tone_curve(parameters),
            "SplitToningShadowHue": parameters.color_grading.shadows.hue,
            "SplitToningShadowSaturation": parameters.color_grading.shadows.saturation,
            "ColorGradeShadowLum": parameters.color_grading.shadows.luminance,
            "ColorGradeMidtoneHue": parameters.color_grading.midtones.hue,
            "ColorGradeMidtoneSat": parameters.color_grading.midtones.saturation,
            "ColorGradeMidtoneLum": parameters.color_grading.midtones.luminance,
            "SplitToningHighlightHue": parameters.color_grading.highlights.hue,
            "SplitToningHighlightSaturation": parameters.color_grading.highlights.saturation,
            "ColorGradeHighlightLum": parameters.color_grading.highlights.luminance,
            "ColorGradeBlending": parameters.color_grading.blending,
            "SplitToningBalance": parameters.color_grading.balance,
            "Sharpness": parameters.sharpening.amount,
            "SharpenRadius": round(parameters.sharpening.radius, 2),
            "SharpenDetail": parameters.sharpening.detail,
            "SharpenEdgeMasking": parameters.sharpening.masking,
            "LuminanceSmoothing": parameters.noise_reduction.luminance,
            "ColorNoiseReduction": parameters.noise_reduction.color,
        }
    )
    settings.update(_hsl_settings(parameters))
    settings.update(_advanced_settings(parameters))
    return settings


def _advanced_settings(parameters: EditingParametersV1) -> dict[str, Any]:
    """Map every advanced (non-basic-panel) parameter onto its Lightroom key.

    Iterating ``PARAMETER_SPECS`` keeps the develop settings table, the XMP
    writer and the preview renderer in sync by construction.
    """

    values: dict[str, Any] = {}
    for spec in PARAMETER_SPECS:
        current = getattr(parameters.advanced, spec.key)
        if is_neutral(spec, current):
            # Only write what the style actually changes: emitting every default
            # would silently reset a user's existing lens correction or transform.
            continue
        values[spec.lr_key] = encode_for_lightroom(spec, current)
    return values


def develop_settings_manifest(parameters: EditingParametersV1) -> dict[str, Any]:
    """Serializable description used by the web UI and the bridge job file."""

    return {
        "schema_version": DEVELOP_SETTINGS_VERSION,
        "process_version": LIGHTROOM_PROCESS_VERSION,
        "settings": build_lightroom_develop_settings(parameters),
    }


def _flat_tone_curve(parameters: EditingParametersV1) -> list[int]:
    flattened: list[int] = []
    for point in parameters.tone_curve:
        flattened.extend((point.x, point.y))
    return flattened


def _hsl_settings(parameters: EditingParametersV1) -> dict[str, int]:
    values: dict[str, int] = {}
    for channel in HSL_CHANNELS:
        adjustment = parameters.hsl.get(channel)
        hue = adjustment.hue if adjustment is not None else 0
        saturation = adjustment.saturation if adjustment is not None else 0
        luminance = adjustment.luminance if adjustment is not None else 0
        title = channel.capitalize()
        values[f"HueAdjustment{title}"] = hue
        values[f"SaturationAdjustment{title}"] = saturation
        values[f"LuminanceAdjustment{title}"] = luminance
    return values
