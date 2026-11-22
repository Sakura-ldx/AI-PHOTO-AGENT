"""Coverage for the advanced Lightroom parameters (schema, mapping, model contract)."""

from __future__ import annotations

import copy

from app.adapters.vision_models import (
    StructuredEditingParameters,
    StructuredPhotoAnalysis,
    strict_output_schema,
)
from app.schemas import (
    AdvancedParameters,
    EditingParametersV1,
    build_advanced_from_overrides,
)
from app.services.lightroom_develop import build_lightroom_develop_settings
from app.services.parameter_specs import (
    CURVE_SPECS,
    MODEL_OVERRIDE_KEYS,
    PARAMETER_SPECS,
    SCALAR_SPECS,
    SPECS_BY_KEY,
    clamp_value,
    encode_for_lightroom,
    is_neutral,
)
from app.services.xmp import build_xmp_preset


def _neutral_analysis_payload(**overrides: object) -> dict[str, object]:
    style = {
        "id": "test-style",
        "name": "测试风格",
        "description": "测试用风格描述。",
        "best_fit_reason": "测试用适配理由。",
        "parameters": {
            "schema_version": "editing_parameters_v1",
            "exposure": 0.0,
            "contrast": 0,
            "highlights": 0,
            "shadows": 0,
            "whites": 0,
            "blacks": 0,
            "temperature": 0,
            "tint": 0,
            "vibrance": 0,
            "saturation": 0,
            "texture": 0,
            "clarity": 0,
            "dehaze": 0,
            "tone_curve": [{"x": 0, "y": 0}, {"x": 128, "y": 128}, {"x": 255, "y": 255}],
            "hsl": {
                channel: {"hue": 0, "saturation": 0, "luminance": 0}
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
            },
            "color_grading": {
                "shadows": {"hue": 0, "saturation": 0, "luminance": 0},
                "midtones": {"hue": 0, "saturation": 0, "luminance": 0},
                "highlights": {"hue": 0, "saturation": 0, "luminance": 0},
                "blending": 50,
                "balance": 0,
            },
            "sharpening": {"amount": 0, "radius": 1.0, "detail": 25, "masking": 0},
            "noise_reduction": {"luminance": 0, "color": 0},
            "crop_suggestion": None,
            "overrides": [],
        },
        "expected_visual_impact": "测试用预期效果。",
        "rationale": "测试用理由。",
    }
    return {
        "overall_summary": "测试用总体判断。",
        "technical_diagnosis": ["测试用技术诊断。"],
        "composition_observations": ["测试用构图观察。"],
        "color_tone_observations": ["测试用色彩观察。"],
        "recommended_styles": [
            style,
            copy.deepcopy(dict(style, id="test-style-2")),
            copy.deepcopy(dict(style, id="test-style-3")),
        ],
    }


def test_every_spec_has_a_distinct_lightroom_key() -> None:
    keys = [spec.lr_key for spec in PARAMETER_SPECS]
    assert len(keys) == len(set(keys))
    assert len(PARAMETER_SPECS) >= 55


def test_advanced_defaults_are_neutral() -> None:
    advanced = AdvancedParameters()
    assert all(is_neutral(spec, getattr(advanced, spec.key)) for spec in PARAMETER_SPECS)


def test_neutral_parameters_produce_no_advanced_lightroom_keys() -> None:
    settings = build_lightroom_develop_settings(EditingParametersV1().scaled(0.0))
    advanced_keys = {spec.lr_key for spec in PARAMETER_SPECS}
    assert advanced_keys.isdisjoint(settings)


def test_overrides_drive_develop_settings_and_xmp() -> None:
    advanced = build_advanced_from_overrides(
        [
            {"key": "grain_amount", "value": 40},
            {"key": "post_crop_vignette_amount", "value": -35},
            {"key": "black_and_white", "value": 1},
            {"key": "curve_red", "value": 0, "points": [[0, 0], [128, 150], [255, 255]]},
        ]
    )
    parameters = EditingParametersV1.model_validate(
        {**EditingParametersV1().model_dump(), "advanced": advanced}
    )
    settings = build_lightroom_develop_settings(parameters)
    assert settings["GrainAmount"] == 40
    assert settings["PostCropVignetteAmount"] == -35
    assert settings["ConvertToGrayscale"] == 1
    assert settings["ToneCurvePV2012Red"] == [0, 0, 128, 150, 255, 255]

    xmp = build_xmp_preset("测试", parameters)
    assert 'crs:GrainAmount="40"' in xmp
    assert "ToneCurvePV2012Red" in xmp


def test_overrides_clamp_and_ignore_unknown_keys() -> None:
    advanced = build_advanced_from_overrides(
        [
            {"key": "not_a_parameter", "value": 10},
            {"key": "grain_amount", "value": 999},
            {"key": "perspective_rotate", "value": -4.26},
        ]
    )
    assert advanced["grain_amount"] == SPECS_BY_KEY["grain_amount"].maximum
    assert advanced["perspective_rotate"] == -4.3
    assert "not_a_parameter" not in advanced


def test_scaled_zero_returns_neutral_advanced() -> None:
    parameters = EditingParametersV1().scaled(0.0)
    assert all(is_neutral(spec, getattr(parameters.advanced, spec.key)) for spec in PARAMETER_SPECS)


def test_scaled_keeps_advanced_at_full_intensity() -> None:
    advanced = build_advanced_from_overrides([{"key": "grain_amount", "value": 40}])
    base = EditingParametersV1().model_validate(
        {**EditingParametersV1().model_dump(), "advanced": advanced}
    )
    assert base.scaled(1.0).advanced.grain_amount == 40
    assert base.scaled(0.5).advanced.grain_amount == 20


def test_analysis_contract_folds_overrides_into_advanced() -> None:
    payload = _neutral_analysis_payload()
    payload["recommended_styles"][0]["parameters"]["overrides"] = [
        {"key": "grain_amount", "value": 25},
        {"key": "black_and_white", "value": 1},
    ]
    analysis = StructuredPhotoAnalysis.model_validate(payload).to_domain()
    first = analysis.recommended_styles[0].parameters.advanced
    assert first.grain_amount == 25
    assert first.black_and_white is True
    # Styles without overrides stay neutral.
    assert analysis.recommended_styles[1].parameters.advanced.grain_amount == 0


def test_model_override_keys_are_a_subset_of_the_specs() -> None:
    assert MODEL_OVERRIDE_KEYS
    for key in MODEL_OVERRIDE_KEYS:
        assert key in SPECS_BY_KEY
    # Transform keys need the crop geometry to make sense, so they are model-invisible.
    assert "perspective_scale" not in MODEL_OVERRIDE_KEYS


def test_strict_schema_exposes_overrides_as_a_list() -> None:
    schema = strict_output_schema(StructuredPhotoAnalysis)
    parameters = schema["$defs"]["StructuredEditingParameters"]
    assert parameters["required"].count("overrides") == 1
    assert parameters["properties"]["overrides"]["type"] == "array"


def test_scalar_specs_cover_curve_kinds() -> None:
    assert {spec.key for spec in CURVE_SPECS} == {"curve_red", "curve_green", "curve_blue"}
    assert not any(spec.kind == "curve" for spec in SCALAR_SPECS)


def test_encode_handles_bool_enum_and_curve() -> None:
    assert encode_for_lightroom(SPECS_BY_KEY["black_and_white"], True) == 1
    upright = SPECS_BY_KEY["perspective_upright"]
    mapping = dict(upright.enum_map or ())
    assert encode_for_lightroom(upright, "vertical") == mapping["vertical"]
    curve = SPECS_BY_KEY["curve_red"]
    assert encode_for_lightroom(curve, ((0, 0), (255, 255))) == [0, 0, 255, 255]


def test_clamp_value_respects_spec_bounds() -> None:
    spec = SPECS_BY_KEY["grain_size"]
    assert clamp_value(spec, -50) == spec.minimum
    assert clamp_value(spec, 5000) == spec.maximum


def test_structured_parameters_accept_empty_overrides() -> None:
    payload = _neutral_analysis_payload()
    parameters = StructuredEditingParameters.model_validate(
        payload["recommended_styles"][0]["parameters"]
    )
    assert parameters.overrides == []

def test_advanced_model_matches_the_spec_table() -> None:
    # parameter_specs.py is the single source of truth; the pydantic model must not
    # drift from it or a slider would silently stop being settable.
    assert set(AdvancedParameters.model_fields) == {spec.key for spec in PARAMETER_SPECS}

def test_defaults_are_not_written_for_a_basic_only_style() -> None:
    # Writing every Lightroom default would reset a user's existing lens correction
    # or transform whenever this preset is applied, so only non neutral keys ship.
    parameters = EditingParametersV1.model_validate(
        {**EditingParametersV1().model_dump(), "exposure": 0.4, "shadows": 20}
    )
    settings = build_lightroom_develop_settings(parameters)
    advanced_keys = {spec.lr_key for spec in PARAMETER_SPECS}
    assert advanced_keys.isdisjoint(settings)
    assert settings["Exposure2012"] == 0.4

    xmp = build_xmp_preset("测试", parameters)
    assert "crs:GrainAmount" not in xmp
    assert "crs:PostCropVignetteStyle" not in xmp
