from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.services.xmp as xmp_module
from app.schemas import (
    EditingParametersV1,
    EditingStyle,
    ImageAnalysis,
    ImageMetadata,
    LearningReference,
    LearningReferenceAnalysis,
    LearningReferenceResponse,
    LearningSession,
    LearningSessionResponse,
    LearningSummary,
    MAPPING_VERSION,
    PhotoUploadResponse,
    PreviewResponse,
)
from app.services.xmp import (
    AI_PHOTO_AGENT_NS,
    CAMERA_RAW_NS,
    RDF_NS,
    build_xmp_preset,
    write_xmp_preset,
)


VALID_RESOURCE_ID = "0123456789abcdef0123456789abcdef"


def make_metadata(**overrides) -> ImageMetadata:
    values = {
        "width": 1200,
        "height": 800,
        "format": "JPEG",
        "color_mode": "RGB",
        "file_size": 4096,
        "content_type": "image/jpeg",
    }
    values.update(overrides)
    return ImageMetadata.model_validate(values)


def make_parameters() -> EditingParametersV1:
    return EditingParametersV1(
        exposure=2.0,
        contrast=80,
        temperature=20,
        tone_curve=[
            {"x": 0, "y": 10},
            {"x": 64, "y": 40},
            {"x": 128, "y": 140},
            {"x": 255, "y": 245},
        ],
        hsl={"red": {"hue": -20, "saturation": 40, "luminance": 10}},
        color_grading={
            "shadows": {"hue": 210, "saturation": 20, "luminance": -10},
            "midtones": {"hue": 40, "saturation": 10, "luminance": 6},
            "highlights": {"hue": 55, "saturation": 30, "luminance": 12},
            "blending": 70,
            "balance": 20,
        },
        sharpening={"amount": 100, "radius": 2.0, "detail": 75, "masking": 60},
        noise_reduction={"luminance": 40, "color": 30},
        crop_suggestion={"aspect_ratio": "16:9", "rationale": "test"},
    )


def make_style(style_id: str = "safe-style") -> EditingStyle:
    return EditingStyle(
        id=style_id,
        name="Test",
        description="Test description",
        best_fit_reason="Test reason",
        parameters=EditingParametersV1(),
        expected_visual_impact="Test impact",
        rationale="Test rationale",
    )


def description_from(xmp: str) -> ET.Element:
    root = ET.fromstring(xmp)
    description = root.find(f".//{{{RDF_NS}}}Description")
    assert description is not None
    return description


def curve_from(xmp: str) -> list[str]:
    root = ET.fromstring(xmp)
    return [item.text or "" for item in root.findall(f".//{{{RDF_NS}}}Seq/{{{RDF_NS}}}li")]


def name_from(xmp: str) -> str:
    root = ET.fromstring(xmp)
    item = root.find(f".//{{{CAMERA_RAW_NS}}}Name/{{{RDF_NS}}}Alt/{{{RDF_NS}}}li")
    assert item is not None
    return item.text or ""


def test_scaled_uses_explicit_neutral_baseline_and_preserves_full_strength() -> None:
    parameters = make_parameters()

    neutral = parameters.scaled(0.0)
    full = parameters.scaled(1.0)

    assert neutral.exposure == 0.0
    assert neutral.contrast == 0
    assert neutral.temperature == 0
    assert [(point.x, point.y) for point in neutral.tone_curve] == [
        (0, 0),
        (64, 64),
        (128, 128),
        (255, 255),
    ]
    assert neutral.hsl["red"].model_dump() == {"hue": 0, "saturation": 0, "luminance": 0}
    assert neutral.color_grading.shadows.model_dump() == {"hue": 0, "saturation": 0, "luminance": 0}
    assert neutral.color_grading.blending == 50
    assert neutral.color_grading.balance == 0
    assert neutral.sharpening.model_dump() == {
        "amount": 0,
        "radius": 1.0,
        "detail": 25,
        "masking": 0,
    }
    assert neutral.noise_reduction.model_dump() == {"luminance": 0, "color": 0}
    assert neutral.crop_suggestion is None
    assert neutral.has_adjustments() is False
    assert full == parameters
    assert full is not parameters


def test_scaled_interpolates_nested_parameters_and_clamps_factor() -> None:
    parameters = make_parameters()

    half = parameters.scaled(0.5)

    assert half.exposure == 1.0
    assert half.contrast == 40
    assert [(point.x, point.y) for point in half.tone_curve] == [
        (0, 5),
        (64, 52),
        (128, 134),
        (255, 250),
    ]
    assert half.hsl["red"].model_dump() == {"hue": -10, "saturation": 20, "luminance": 5}
    assert half.color_grading.shadows.hue == 210
    assert half.color_grading.shadows.saturation == 10
    assert half.color_grading.shadows.luminance == -5
    assert half.color_grading.blending == 60
    assert half.color_grading.balance == 10
    assert half.sharpening.model_dump() == {
        "amount": 50,
        "radius": 1.5,
        "detail": 50,
        "masking": 30,
    }
    assert half.noise_reduction.model_dump() == {"luminance": 20, "color": 15}
    assert parameters.scaled(-1.0) == parameters.scaled(0.0)
    assert parameters.scaled(2.0) == parameters


@pytest.mark.parametrize(
    "tone_curve",
    [
        [{"x": 1, "y": 1}, {"x": 255, "y": 255}],
        [
            {"x": 0, "y": 0},
            {"x": 128, "y": 128},
            {"x": 64, "y": 64},
            {"x": 255, "y": 255},
        ],
    ],
)
def test_tone_curve_requires_endpoints_and_strict_x_order(tone_curve) -> None:
    with pytest.raises(ValidationError, match="tone curve"):
        EditingParametersV1(tone_curve=tone_curve)


def test_hsl_style_resource_and_metadata_boundaries() -> None:
    with pytest.raises(ValidationError, match="unsupported HSL channels"):
        EditingParametersV1(hsl={"cyan": {}})

    with pytest.raises(ValidationError):
        make_style("../unsafe")

    with pytest.raises(ValidationError):
        PhotoUploadResponse(
            photo_id="not-a-resource-id",
            metadata=make_metadata(),
            display_url="/api/photos/not-a-resource-id/display",
        )

    for invalid_metadata in (
        {"width": 0},
        {"height": 0},
        {"file_size": 0},
        {"content_type": "application/octet-stream"},
        {"color_mode": "x" * 33},
    ):
        with pytest.raises(ValidationError):
            make_metadata(**invalid_metadata)


def test_analysis_requires_unique_style_ids() -> None:
    repeated = make_style()

    with pytest.raises(ValidationError, match="unique"):
        ImageAnalysis(
            overall_summary="summary",
            technical_diagnosis=["technical"],
            composition_observations=["composition"],
            color_tone_observations=["color"],
            recommended_styles=[repeated, repeated, repeated],
        )


def test_analysis_and_learning_outputs_reject_blank_or_empty_content() -> None:
    with pytest.raises(ValidationError):
        ImageAnalysis(
            overall_summary="   ",
            technical_diagnosis=["technical"],
            composition_observations=["composition"],
            color_tone_observations=["color"],
            recommended_styles=[make_style(), make_style("two"), make_style("three")],
        )

    with pytest.raises(ValidationError):
        LearningReferenceAnalysis(
            composition_structure=[],
            subject_placement="subject",
            visual_hierarchy=["hierarchy"],
            color_palette=["palette"],
            tonal_distribution=["tone"],
            mood_style_labels=["mood"],
            practical_lessons=["lesson"],
        )

    with pytest.raises(ValidationError):
        LearningSummary(
            style_overview="overview",
            common_composition_patterns=["composition"],
            common_color_strategies=["color"],
            mistakes_to_avoid=[],
            exercises=["exercise"],
        )


def test_learning_response_models_exclude_private_image_paths() -> None:
    reference = LearningReference(
        id=VALID_RESOURCE_ID,
        image_file=Path("private/reference.jpg"),
        image_url=f"/references/{VALID_RESOURCE_ID}/reference.jpg",
        metadata=make_metadata(),
    )
    session = LearningSession(
        id="f" * 32,
        topic="privacy",
        references=[reference],
    )

    reference_response = LearningReferenceResponse.model_validate(reference)
    session_response = LearningSessionResponse.model_validate(session)

    assert "image_file" not in reference_response.model_dump()
    assert "image_file" not in session_response.model_dump_json()
    assert session_response.references[0].id == VALID_RESOURCE_ID


def test_learning_session_requires_unique_reference_ids() -> None:
    reference = LearningReference(
        id=VALID_RESOURCE_ID,
        image_file=Path("private/reference.jpg"),
        image_url="/reference",
        metadata=make_metadata(),
    )

    with pytest.raises(ValidationError, match="reference ids must be unique"):
        LearningSession(
            id="f" * 32,
            topic="duplicates",
            references=[reference, reference],
        )


def test_preview_response_marks_pillow_preview_as_approximate() -> None:
    response = PreviewResponse(
        photo_id=VALID_RESOURCE_ID,
        style_id="safe-style",
        intensity=0.5,
        preview_url="/generated/preview.jpg",
        parameters=EditingParametersV1(),
    )

    assert response.preview_is_approximate is True


def test_xmp_is_structured_safe_and_carries_v2_mapping_metadata() -> None:
    parameters = make_parameters()
    preset_name = 'Portrait "A&B" <Neutral>'

    xmp = build_xmp_preset(preset_name, parameters)
    description = description_from(xmp)

    assert MAPPING_VERSION == "lightroom_xmp_v2"
    assert name_from(xmp) == preset_name
    assert f"{{{CAMERA_RAW_NS}}}Name" not in description.attrib
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}HasSettings"] == "True"
    assert description.attrib[f"{{{AI_PHOTO_AGENT_NS}}}MappingVersion"] == MAPPING_VERSION
    assert re.fullmatch(r"[0-9A-F]{32}", description.attrib[f"{{{CAMERA_RAW_NS}}}UUID"])
    assert curve_from(xmp) == ["0, 10", "64, 40", "128, 140", "255, 245"]
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}ColorGradeShadowLum"] == "-10"
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}ColorGradeMidtoneLum"] == "6"
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}ColorGradeHighlightLum"] == "12"
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}SplitToningShadowHue"] == "210"
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}SplitToningHighlightHue"] == "55"
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}SplitToningBalance"] == "20"
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}IncrementalTemperature"] == "20"
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}IncrementalTint"] == "0"
    assert f"{{{CAMERA_RAW_NS}}}WhiteBalance" not in description.attrib
    assert f"{{{CAMERA_RAW_NS}}}Temperature" not in description.attrib
    assert f"{{{CAMERA_RAW_NS}}}ColorGradeGlobalLum" not in description.attrib
    assert ET.fromstring(xmp).find(f".//{{{CAMERA_RAW_NS}}}HasSettings") is None
    assert 'Portrait "A&amp;B" &lt;Neutral&gt;' in xmp


def test_xmp_uuid_is_deterministic_and_changes_with_content() -> None:
    parameters = make_parameters()

    first = description_from(build_xmp_preset("Stable", parameters))
    second = description_from(build_xmp_preset("Stable", parameters.model_copy(deep=True)))
    changed = description_from(build_xmp_preset("Changed", parameters))
    uuid_key = f"{{{CAMERA_RAW_NS}}}UUID"

    assert first.attrib[uuid_key] == second.attrib[uuid_key]
    assert first.attrib[uuid_key] != changed.attrib[uuid_key]


def test_xmp_reflects_neutral_and_full_strength_curves() -> None:
    parameters = make_parameters()
    neutral_xmp = build_xmp_preset("Neutral", parameters.scaled(0.0))
    full_xmp = build_xmp_preset("Full", parameters.scaled(1.0))
    neutral_description = description_from(neutral_xmp)
    full_description = description_from(full_xmp)

    assert curve_from(neutral_xmp) == []
    assert curve_from(full_xmp) == ["0, 10", "64, 40", "128, 140", "255, 245"]
    assert neutral_description.attrib[f"{{{CAMERA_RAW_NS}}}HasSettings"] == "False"
    assert f"{{{CAMERA_RAW_NS}}}Sharpness" not in neutral_description.attrib
    assert f"{{{CAMERA_RAW_NS}}}ColorNoiseReduction" not in neutral_description.attrib
    assert f"{{{CAMERA_RAW_NS}}}IncrementalTemperature" not in neutral_description.attrib
    assert full_description.attrib[f"{{{CAMERA_RAW_NS}}}HasSettings"] == "True"
    assert full_description.attrib[f"{{{CAMERA_RAW_NS}}}Sharpness"] == "100"
    assert full_description.attrib[f"{{{CAMERA_RAW_NS}}}ColorNoiseReduction"] == "30"


def test_xmp_rejects_blank_or_xml_control_character_names() -> None:
    with pytest.raises(ValueError, match="blank"):
        build_xmp_preset("   ", EditingParametersV1())
    with pytest.raises(ValueError, match="XML 1.0"):
        build_xmp_preset("bad\x00name", EditingParametersV1())


def test_xmp_file_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    output_path = tmp_path / "preset.xmp"

    write_xmp_preset(output_path, "Atomic", EditingParametersV1())

    assert ET.fromstring(output_path.read_text(encoding="utf-8")) is not None
    assert list(tmp_path.glob("*.tmp")) == []


def test_failed_xmp_replace_preserves_previous_file_and_removes_temp_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "preset.xmp"
    output_path.write_text("previous", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(xmp_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        write_xmp_preset(output_path, "Atomic failure", EditingParametersV1())

    assert output_path.read_text(encoding="utf-8") == "previous"
    assert list(tmp_path.glob("*.tmp")) == []
