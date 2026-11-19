from __future__ import annotations

from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.services.parameter_specs import (
    PARAMETER_SPECS,
    SCALAR_SPECS,
    ParameterSpec as ParameterSpec,
    SPECS_BY_KEY,
    clamp_value,
    is_neutral,
)


SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "TIFF", "CR3"}
SUPPORTED_CONTENT_TYPES = {
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/x-canon-cr3",
}
HSL_CHANNELS = ("red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta")
MAPPING_VERSION = "lightroom_xmp_v2"
DEVELOP_SETTINGS_VERSION = "lightroom_develop_v1"
ImageFormat = Literal["JPEG", "PNG", "TIFF", "CR3"]
ImageContentType = Literal["image/jpeg", "image/png", "image/tiff", "image/x-canon-cr3"]
MAX_LEARNING_REFERENCES = 8
PhotoAnalysisProviderSelection = Literal[
    "default", "mock", "openai", "codex", "deepseek"
]

ResourceId = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{32}$"),
]
StyleId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$"),
]
AnalysisTitle = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=120),
]
AnalysisParagraph = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2000),
]
AnalysisListItem = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
AnalysisItems = Annotated[
    list[AnalysisListItem],
    Field(min_length=1, max_length=12),
]

_NEUTRAL_SHARPENING = {
    "amount": 0,
    "radius": 1.0,
    "detail": 25,
    "masking": 0,
}
_NEUTRAL_NOISE_REDUCTION = {
    "luminance": 0,
    "color": 0,
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_xml_1_0_text(value: str) -> bool:
    return all(
        codepoint in {0x9, 0xA, 0xD}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
        for codepoint in map(ord, value)
    )


class ToneCurvePoint(BaseModel):
    x: int = Field(ge=0, le=255)
    y: int = Field(ge=0, le=255)


class HslAdjustment(BaseModel):
    hue: int = Field(default=0, ge=-100, le=100)
    saturation: int = Field(default=0, ge=-100, le=100)
    luminance: int = Field(default=0, ge=-100, le=100)


class ColorGradingWheel(BaseModel):
    hue: int = Field(default=0, ge=0, le=360)
    saturation: int = Field(default=0, ge=0, le=100)
    luminance: int = Field(default=0, ge=-100, le=100)


class ColorGrading(BaseModel):
    shadows: ColorGradingWheel = Field(default_factory=ColorGradingWheel)
    midtones: ColorGradingWheel = Field(default_factory=ColorGradingWheel)
    highlights: ColorGradingWheel = Field(default_factory=ColorGradingWheel)
    blending: int = Field(default=50, ge=0, le=100)
    balance: int = Field(default=0, ge=-100, le=100)


class Sharpening(BaseModel):
    amount: int = Field(default=40, ge=0, le=150)
    radius: float = Field(default=1.0, ge=0.5, le=3.0)
    detail: int = Field(default=25, ge=0, le=100)
    masking: int = Field(default=0, ge=0, le=100)


class NoiseReduction(BaseModel):
    luminance: int = Field(default=0, ge=0, le=100)
    color: int = Field(default=25, ge=0, le=100)


def _validate_curve(points: list[ToneCurvePoint] | None) -> list[ToneCurvePoint] | None:
    if points is None:
        return None
    if len(points) < 2:
        raise ValueError("tone curve needs at least two points")
    if points[0].x != 0 or points[-1].x != 255:
        raise ValueError("tone curve must start at x=0 and end at x=255")
    if any(left.x >= right.x for left, right in pairwise(points)):
        raise ValueError("tone curve x coordinates must be strictly increasing")
    return points


def _identity_curve() -> list[ToneCurvePoint]:
    return [
        ToneCurvePoint(x=0, y=0),
        ToneCurvePoint(x=128, y=128),
        ToneCurvePoint(x=255, y=255),
    ]


class AdvancedParameters(BaseModel):
    """Lightroom Develop sliders outside the "basic" panel.

    One field per entry of :data:`app.services.parameter_specs.PARAMETER_SPECS`.
    Defaults are the Lightroom defaults, so a default instance is visually neutral.
    """

    model_config = ConfigDict(extra="forbid")

    # 基本：黑白
    black_and_white: bool = False
    gray_mixer_red: int = Field(default=0, ge=-100, le=100)
    gray_mixer_orange: int = Field(default=0, ge=-100, le=100)
    gray_mixer_yellow: int = Field(default=0, ge=-100, le=100)
    gray_mixer_green: int = Field(default=0, ge=-100, le=100)
    gray_mixer_aqua: int = Field(default=0, ge=-100, le=100)
    gray_mixer_blue: int = Field(default=0, ge=-100, le=100)
    gray_mixer_purple: int = Field(default=0, ge=-100, le=100)
    gray_mixer_magenta: int = Field(default=0, ge=-100, le=100)

    # 校准
    shadow_tint: int = Field(default=0, ge=-100, le=100)
    red_hue: int = Field(default=0, ge=-100, le=100)
    red_saturation: int = Field(default=0, ge=-100, le=100)
    green_hue: int = Field(default=0, ge=-100, le=100)
    green_saturation: int = Field(default=0, ge=-100, le=100)
    blue_hue: int = Field(default=0, ge=-100, le=100)
    blue_saturation: int = Field(default=0, ge=-100, le=100)

    # 色调曲线（参数曲线 + 分通道点曲线）
    parametric_shadows: int = Field(default=0, ge=-100, le=100)
    parametric_darks: int = Field(default=0, ge=-100, le=100)
    parametric_lights: int = Field(default=0, ge=-100, le=100)
    parametric_highlights: int = Field(default=0, ge=-100, le=100)
    parametric_shadow_split: int = Field(default=25, ge=0, le=100)
    parametric_midtone_split: int = Field(default=50, ge=0, le=100)
    parametric_highlight_split: int = Field(default=75, ge=0, le=100)
    curve_red: list[ToneCurvePoint] | None = None
    curve_green: list[ToneCurvePoint] | None = None
    curve_blue: list[ToneCurvePoint] | None = None

    # 细节：降噪细节修饰
    noise_luminance_detail: int = Field(default=50, ge=0, le=100)
    noise_luminance_contrast: int = Field(default=0, ge=0, le=100)
    noise_color_detail: int = Field(default=50, ge=0, le=100)
    noise_color_smoothness: int = Field(default=50, ge=0, le=100)

    # 镜头
    auto_lateral_ca: bool = False
    lens_manual_distortion: int = Field(default=0, ge=-100, le=100)
    lens_vignette_amount: int = Field(default=0, ge=-100, le=100)
    lens_vignette_midpoint: int = Field(default=50, ge=0, le=100)
    defringe_purple_amount: int = Field(default=0, ge=0, le=100)
    defringe_purple_hue_lo: int = Field(default=30, ge=0, le=100)
    defringe_purple_hue_hi: int = Field(default=70, ge=0, le=100)
    defringe_green_amount: int = Field(default=0, ge=0, le=100)
    defringe_green_hue_lo: int = Field(default=40, ge=0, le=100)
    defringe_green_hue_hi: int = Field(default=60, ge=0, le=100)

    # 变换
    perspective_upright: Literal["off", "auto", "level", "vertical", "full"] = "off"
    perspective_vertical: int = Field(default=0, ge=-100, le=100)
    perspective_horizontal: int = Field(default=0, ge=-100, le=100)
    perspective_rotate: float = Field(default=0.0, ge=-10.0, le=10.0)
    perspective_aspect: int = Field(default=0, ge=-100, le=100)
    perspective_scale: int = Field(default=100, ge=50, le=150)
    perspective_x: int = Field(default=0, ge=-100, le=100)
    perspective_y: int = Field(default=0, ge=-100, le=100)

    # 效果
    post_crop_vignette_amount: int = Field(default=0, ge=-100, le=100)
    post_crop_vignette_midpoint: int = Field(default=50, ge=0, le=100)
    post_crop_vignette_roundness: int = Field(default=0, ge=-100, le=100)
    post_crop_vignette_feather: int = Field(default=50, ge=0, le=100)
    post_crop_vignette_highlights: int = Field(default=0, ge=0, le=100)
    post_crop_vignette_style: Literal["highlight", "color", "paint"] = "highlight"
    grain_amount: int = Field(default=0, ge=0, le=100)
    grain_size: int = Field(default=25, ge=0, le=100)
    grain_roughness: int = Field(default=50, ge=0, le=100)

    @field_validator("curve_red", "curve_green", "curve_blue")
    @classmethod
    def validate_channel_curve(
        cls, points: list[ToneCurvePoint] | None
    ) -> list[ToneCurvePoint] | None:
        return _validate_curve(points)


def build_advanced_from_overrides(overrides: list[Any]) -> dict[str, Any]:
    """Fold sparse model overrides into a full ``AdvancedParameters`` payload.

    Unknown keys are dropped and out of range values are clamped: a hallucinated
    slider must never fail an otherwise good analysis.
    """

    data: dict[str, Any] = AdvancedParameters().model_dump()
    for item in overrides or []:
        if isinstance(item, dict):
            key = item.get("key")
            value = item.get("value")
            points = item.get("points")
        else:
            key = getattr(item, "key", None)
            value = getattr(item, "value", None)
            points = getattr(item, "points", None)
        spec = SPECS_BY_KEY.get(str(key)) if key is not None else None
        if spec is None:
            continue
        if spec.kind == "curve":
            curve = [{"x": int(x), "y": int(y)} for x, y in _coerce_points(points)]
            try:
                data[spec.key] = _validate_curve([ToneCurvePoint(**p) for p in curve])
            except ValueError:
                continue
            continue
        if spec.kind == "bool":
            data[spec.key] = bool(value)
            continue
        if spec.kind == "enum":
            if spec.enum_map is not None:
                allowed = {name for name, _ in spec.enum_map}
                if str(value) in allowed:
                    data[spec.key] = str(value)
            continue
        if value is None:
            continue
        try:
            clamped = clamp_value(spec, float(value))
        except (TypeError, ValueError):
            # Untrusted model output must never fail the whole analysis.
            continue
        data[spec.key] = (
            round(clamped, 1) if spec.kind == "number" else int(round(clamped))
        )
    return data


def _coerce_points(points: Any) -> list[tuple[int, int]]:
    """Best-effort curve point coercion; malformed entries are dropped, not raised.

    Model output is untrusted, so a hallucinated curve must degrade to the
    remaining valid points (or to the default identity curve) instead of failing
    an otherwise good analysis.
    """

    try:
        items = list(points or [])
    except (TypeError, ValueError):
        return []
    result: list[tuple[int, int]] = []
    for item in items:
        if isinstance(item, dict):
            x: Any = item.get("x", 0)
            y: Any = item.get("y", 0)
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            x, y = item[0], item[1]
        else:
            continue
        try:
            result.append((_clamp_curve_coordinate(x), _clamp_curve_coordinate(y)))
        except (TypeError, ValueError):
            continue
    return result


def _clamp_curve_coordinate(value: Any) -> int:
    return max(0, min(255, int(value)))


class CropSuggestion(BaseModel):
    aspect_ratio: AnalysisTitle | None = None
    rationale: AnalysisParagraph | None = None


class EditingParametersV1(BaseModel):
    schema_version: Literal["editing_parameters_v1"] = "editing_parameters_v1"
    exposure: float = Field(default=0.0, ge=-5.0, le=5.0)
    contrast: int = Field(default=0, ge=-100, le=100)
    highlights: int = Field(default=0, ge=-100, le=100)
    shadows: int = Field(default=0, ge=-100, le=100)
    whites: int = Field(default=0, ge=-100, le=100)
    blacks: int = Field(default=0, ge=-100, le=100)
    temperature: int = Field(default=0, ge=-100, le=100)
    tint: int = Field(default=0, ge=-100, le=100)
    vibrance: int = Field(default=0, ge=-100, le=100)
    saturation: int = Field(default=0, ge=-100, le=100)
    texture: int = Field(default=0, ge=-100, le=100)
    clarity: int = Field(default=0, ge=-100, le=100)
    dehaze: int = Field(default=0, ge=-100, le=100)
    tone_curve: list[ToneCurvePoint] = Field(
        min_length=2,
        max_length=256,
        default_factory=lambda: [
            ToneCurvePoint(x=0, y=0),
            ToneCurvePoint(x=64, y=58),
            ToneCurvePoint(x=128, y=128),
            ToneCurvePoint(x=192, y=198),
            ToneCurvePoint(x=255, y=255),
        ]
    )
    hsl: dict[str, HslAdjustment] = Field(default_factory=dict, max_length=len(HSL_CHANNELS))
    color_grading: ColorGrading = Field(default_factory=ColorGrading)
    sharpening: Sharpening = Field(default_factory=Sharpening)
    noise_reduction: NoiseReduction = Field(default_factory=NoiseReduction)
    advanced: AdvancedParameters = Field(default_factory=AdvancedParameters)
    crop_suggestion: CropSuggestion | None = None

    @field_validator("tone_curve")
    @classmethod
    def validate_tone_curve(cls, points: list[ToneCurvePoint]) -> list[ToneCurvePoint]:
        if points[0].x != 0 or points[-1].x != 255:
            raise ValueError("tone curve must start at x=0 and end at x=255")
        if any(left.x >= right.x for left, right in pairwise(points)):
            raise ValueError("tone curve x coordinates must be strictly increasing")
        return points

    @field_validator("hsl")
    @classmethod
    def validate_hsl_channels(cls, value: dict[str, HslAdjustment]) -> dict[str, HslAdjustment]:
        unknown = sorted(set(value).difference(HSL_CHANNELS))
        if unknown:
            raise ValueError(f"unsupported HSL channels: {', '.join(unknown)}")
        return value

    def scaled(self, intensity: float) -> "EditingParametersV1":
        factor = max(0.0, min(1.0, intensity))
        if factor == 1.0:
            return self.model_copy(deep=True)

        data = self.model_dump()
        data["exposure"] = _lerp_float(0.0, data["exposure"], factor, digits=2)
        scalable_int_fields = [
            "contrast",
            "highlights",
            "shadows",
            "whites",
            "blacks",
            "temperature",
            "tint",
            "vibrance",
            "saturation",
            "texture",
            "clarity",
            "dehaze",
        ]
        for field in scalable_int_fields:
            data[field] = _lerp_int(0, data[field], factor)

        data["tone_curve"] = [
            {
                "x": point["x"],
                "y": _lerp_int(point["x"], point["y"], factor),
            }
            for point in data["tone_curve"]
        ]
        for channel in data["hsl"].values():
            for key in ("hue", "saturation", "luminance"):
                channel[key] = _lerp_int(0, channel[key], factor)

        for wheel in ("shadows", "midtones", "highlights"):
            target_wheel = data["color_grading"][wheel]
            # Hue is an angle rather than an effect magnitude. Keep the target hue
            # while saturation grows; use zero only at the fully neutral endpoint.
            target_wheel["hue"] = target_wheel["hue"] if factor > 0.0 else 0
            target_wheel["saturation"] = _lerp_int(0, target_wheel["saturation"], factor)
            target_wheel["luminance"] = _lerp_int(0, target_wheel["luminance"], factor)
        data["color_grading"]["blending"] = _lerp_int(
            50,
            data["color_grading"]["blending"],
            factor,
        )
        data["color_grading"]["balance"] = _lerp_int(
            0,
            data["color_grading"]["balance"],
            factor,
        )

        sharpening = data["sharpening"]
        sharpening["amount"] = _lerp_int(
            _NEUTRAL_SHARPENING["amount"],
            sharpening["amount"],
            factor,
        )
        sharpening["radius"] = _lerp_float(
            _NEUTRAL_SHARPENING["radius"],
            sharpening["radius"],
            factor,
            digits=2,
        )
        sharpening["detail"] = _lerp_int(
            _NEUTRAL_SHARPENING["detail"],
            sharpening["detail"],
            factor,
        )
        sharpening["masking"] = _lerp_int(
            _NEUTRAL_SHARPENING["masking"],
            sharpening["masking"],
            factor,
        )

        noise_reduction = data["noise_reduction"]
        noise_reduction["luminance"] = _lerp_int(
            _NEUTRAL_NOISE_REDUCTION["luminance"],
            noise_reduction["luminance"],
            factor,
        )
        noise_reduction["color"] = _lerp_int(
            _NEUTRAL_NOISE_REDUCTION["color"],
            noise_reduction["color"],
            factor,
        )
        if factor == 0.0:
            data["crop_suggestion"] = None
        data["advanced"] = _scale_advanced(data["advanced"], factor)
        return EditingParametersV1.model_validate(data)

    def has_adjustments(self) -> bool:
        scalar_fields = (
            "exposure",
            "contrast",
            "highlights",
            "shadows",
            "whites",
            "blacks",
            "temperature",
            "tint",
            "vibrance",
            "saturation",
            "texture",
            "clarity",
            "dehaze",
        )
        if any(getattr(self, field) != 0 for field in scalar_fields):
            return True
        if any(point.x != point.y for point in self.tone_curve):
            return True
        if any(
            adjustment.hue != 0 or adjustment.saturation != 0 or adjustment.luminance != 0
            for adjustment in self.hsl.values()
        ):
            return True
        wheels = (
            self.color_grading.shadows,
            self.color_grading.midtones,
            self.color_grading.highlights,
        )
        if any(wheel.saturation != 0 or wheel.luminance != 0 for wheel in wheels):
            return True
        if self.color_grading.balance != 0:
            return True
        if self.sharpening.amount != 0:
            return True
        if self.noise_reduction.luminance != 0 or self.noise_reduction.color != 0:
            return True
        return any(
            not is_neutral(spec, getattr(self.advanced, spec.key))
            for spec in PARAMETER_SPECS
        )


class EditingStyle(BaseModel):
    id: StyleId
    name: AnalysisTitle
    description: AnalysisParagraph
    best_fit_reason: AnalysisParagraph
    parameters: EditingParametersV1
    expected_visual_impact: AnalysisParagraph
    rationale: AnalysisParagraph

    @field_validator("name")
    @classmethod
    def name_must_be_export_safe(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("style name must not be blank")
        if not is_xml_1_0_text(name):
            raise ValueError("style name contains characters that XML 1.0 cannot represent")
        return name


class ImageMetadata(BaseModel):
    width: int = Field(ge=1, le=1_000_000)
    height: int = Field(ge=1, le=1_000_000)
    format: ImageFormat
    color_mode: str = Field(min_length=1, max_length=32)
    file_size: int = Field(ge=1, le=2**63 - 1)
    content_type: ImageContentType
    render_source: Literal["original", "embedded_preview"] = "original"


class ImageAnalysis(BaseModel):
    overall_summary: AnalysisParagraph
    technical_diagnosis: AnalysisItems
    composition_observations: AnalysisItems
    color_tone_observations: AnalysisItems
    recommended_styles: list[EditingStyle] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def require_three_styles(self) -> "ImageAnalysis":
        if len(self.recommended_styles) != 3:
            raise ValueError("analysis must include exactly 3 recommended styles")
        style_ids = [style.id for style in self.recommended_styles]
        if len(set(style_ids)) != len(style_ids):
            raise ValueError("analysis must include 3 unique recommended style ids")
        return self


class LearningReferenceAnalysis(BaseModel):
    composition_structure: AnalysisItems
    subject_placement: AnalysisParagraph
    visual_hierarchy: AnalysisItems
    color_palette: AnalysisItems
    tonal_distribution: AnalysisItems
    mood_style_labels: AnalysisItems
    practical_lessons: AnalysisItems


class LearningSummary(BaseModel):
    style_overview: AnalysisParagraph
    common_composition_patterns: AnalysisItems
    common_color_strategies: AnalysisItems
    mistakes_to_avoid: AnalysisItems
    exercises: AnalysisItems


class LearningReferenceAnalysisResult(BaseModel):
    reference_id: ResourceId
    analysis: LearningReferenceAnalysis

    model_config = ConfigDict(extra="forbid")


class LearningAnalysisResult(BaseModel):
    references: list[LearningReferenceAnalysisResult] = Field(
        min_length=1,
        max_length=MAX_LEARNING_REFERENCES,
    )
    summary: LearningSummary

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def reference_ids_must_be_unique(self) -> "LearningAnalysisResult":
        reference_ids = [reference.reference_id for reference in self.references]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("learning analysis reference ids must be unique")
        return self


class GeneratedAsset(BaseModel):
    path: Path
    url: str
    created_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class LearningReference(BaseModel):
    id: ResourceId
    source_url: str | None = Field(default=None, max_length=2048)
    author: str | None = Field(default=None, max_length=200)
    license: str | None = Field(default=None, max_length=200)
    image_file: Path
    image_url: str
    metadata: ImageMetadata
    analysis: LearningReferenceAnalysis | None = None
    created_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class LearningSession(BaseModel):
    id: ResourceId
    topic: str = Field(min_length=1, max_length=120)
    references: list[LearningReference] = Field(default_factory=list)
    summary: LearningSummary | None = None
    created_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("topic")
    @classmethod
    def topic_must_not_be_blank(cls, value: str) -> str:
        topic = value.strip()
        if not topic:
            raise ValueError("topic must not be blank")
        return topic

    @model_validator(mode="after")
    def reference_ids_must_be_unique(self) -> "LearningSession":
        reference_ids = [reference.id for reference in self.references]
        if len(reference_ids) != len(set(reference_ids)):
            raise ValueError("learning session reference ids must be unique")
        return self


class PhotoSession(BaseModel):
    id: ResourceId
    # Basename of the file the user uploaded (sanitized), kept for identity
    # checks such as the Lightroom bridge's "is this still the selected photo?"
    # guard. Optional so sessions written before this field existed still load.
    original_filename: str | None = None
    original_file: Path
    render_file: Path | None = None
    display_file: Path | None = None
    metadata: ImageMetadata
    analysis: ImageAnalysis | None = None
    preview_files: dict[str, GeneratedAsset] = Field(default_factory=dict)
    preset_files: dict[str, GeneratedAsset] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    model_config = ConfigDict(arbitrary_types_allowed=True)


class PhotoAnalyzeRequest(BaseModel):
    provider: PhotoAnalysisProviderSelection = "default"

    model_config = ConfigDict(extra="forbid")


AnalysisTaskStage = Literal[
    "queued",
    "measuring",
    "provider",
    "validating",
    "succeeded",
    "failed",
    "cancelled",
]


class AnalysisTaskCreated(BaseModel):
    task_id: str
    photo_id: str
    stage: AnalysisTaskStage = "queued"


class AnalysisTaskStatus(BaseModel):
    task_id: str
    photo_id: str
    stage: AnalysisTaskStage
    elapsed_seconds: float = 0.0
    progress_hint: str = ""
    analysis: ImageAnalysis | None = None
    error: str | None = None


class PreviewRequest(BaseModel):
    style_id: StyleId
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)


class ExportRequest(BaseModel):
    style_id: StyleId
    intensity: float = Field(default=1.0, ge=0.0, le=1.0)


class LearningSessionCreateRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=120)

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        topic = value.strip()
        if not topic:
            raise ValueError("topic must not be blank")
        return topic


class PhotoUploadResponse(BaseModel):
    photo_id: ResourceId
    metadata: ImageMetadata
    display_url: str


class PreviewResponse(BaseModel):
    photo_id: ResourceId
    style_id: StyleId
    intensity: float
    preview_url: str
    parameters: EditingParametersV1
    parameters_summary: str = ""
    strength: float = 1.0
    preview_is_approximate: Literal[True] = True
    # Resolution diagnostics, surfaced so the learning summary can record why a
    # recommended intensity was reduced or verified.
    verified: bool = True
    warnings: list[str] = Field(default_factory=list)
    baseline_notes: list[str] = Field(default_factory=list)


class ExportResponse(BaseModel):
    photo_id: ResourceId
    style_id: StyleId
    intensity: float
    preset_name: str
    mapping_version: str = MAPPING_VERSION
    download_url: str
    preset_path: Path | None = Field(default=None, exclude=True)


class DevelopResponse(BaseModel):
    photo_id: ResourceId
    style_id: StyleId
    intensity: float
    style_name: str
    # Sanitized upload basename for the Lightroom bridge's photo-identity guard.
    # Never an absolute path; ``None`` when the upload did not carry a name.
    original_filename: str | None = None
    preset_name: str
    mapping_version: str = MAPPING_VERSION
    develop_settings_version: str = DEVELOP_SETTINGS_VERSION
    parameters: EditingParametersV1
    parameters_summary: str = ""
    strength: float = 1.0
    develop_settings: dict[str, Any]
    xmp_url: str
    preset_path: Path | None = Field(default=None, exclude=True)


class LightroomSendRequest(ExportRequest):
    install_plugin: bool = True


class LightroomStatusResponse(BaseModel):
    detected: bool
    lightroom_root: str | None = None
    presets_dir: str | None = None
    modules_dir: str | None = None
    bridge_inbox: str | None = None
    plugin_installed: bool = False
    plugin_source: str
    server_url: str
    message: str

    model_config = ConfigDict(extra="forbid")


class LightroomSendResponse(BaseModel):
    photo_id: ResourceId
    style_id: StyleId
    intensity: float
    preset_name: str
    status: Literal[
        "queued",
        "lightroom_not_found",
        "plugin_not_installed",
    ]
    job_id: str | None = None
    preset_path: str | None = None
    plugin_installed: bool = False
    lightroom_root: str | None = None
    bridge_inbox: str | None = None
    xmp_url: str
    message: str

    model_config = ConfigDict(extra="forbid")


class LightroomPluginInstallResponse(BaseModel):
    installed: bool
    plugin_dir: str | None = None
    source_dir: str
    message: str

    model_config = ConfigDict(extra="forbid")


class ErrorResponse(BaseModel):
    detail: str | dict[str, Any]


class LearningReferenceResponse(BaseModel):
    id: ResourceId
    source_url: str | None = None
    author: str | None = None
    license: str | None = None
    image_url: str
    metadata: ImageMetadata
    analysis: LearningReferenceAnalysis | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LearningSessionResponse(BaseModel):
    id: ResourceId
    topic: str
    references: list[LearningReferenceResponse]
    summary: LearningSummary | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


def _lerp_int(neutral: int, target: int, factor: float) -> int:
    return round(neutral + (target - neutral) * factor)


def _lerp_float(neutral: float, target: float, factor: float, *, digits: int) -> float:
    return round(neutral + (target - neutral) * factor, digits)


def _scale_advanced(data: dict[str, Any], factor: float) -> dict[str, Any]:
    """Scale every advanced slider from its neutral value towards its target."""

    scaled = dict(data)
    for spec in SCALAR_SPECS:
        current = scaled.get(spec.key)
        if spec.kind == "bool":
            scaled[spec.key] = bool(current) if factor > 0.0 else False
            continue
        if spec.kind == "enum":
            if factor == 0.0 and spec.enum_map is not None:
                scaled[spec.key] = spec.enum_map[0][0]
            continue
        if current is None:
            continue
        if isinstance(current, float):
            scaled[spec.key] = _lerp_float(float(spec.neutral), float(current), factor, digits=1)
        else:
            scaled[spec.key] = _lerp_int(int(spec.neutral), int(current), factor)
    for spec in PARAMETER_SPECS:
        if spec.kind != "curve":
            continue
        points = scaled.get(spec.key)
        if not points:
            scaled[spec.key] = None
            continue
        if factor <= 0.0:
            scaled[spec.key] = None
            continue
        scaled[spec.key] = [
            {
                "x": point["x"] if isinstance(point, dict) else point.x,
                "y": _lerp_int(
                    point["x"] if isinstance(point, dict) else point.x,
                    point["y"] if isinstance(point, dict) else point.y,
                    factor,
                ),
            }
            for point in points
        ]
    return scaled
