from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.schemas import (
    ANALYSIS_VERSION,
    AdvancedParameters,
    ColorGrading,
    ColorGradingWheel,
    CropSuggestion,
    EditingParametersV1,
    EditingStyle,
    HslAdjustment,
    ImageAnalysis,
    ImageMetadata,
    NoiseReduction,
    Sharpening,
    ToneCurvePoint,
)


class AnalysisProvider(ABC):
    @abstractmethod
    def analyze(
        self,
        image_path: Path,
        metadata: ImageMetadata,
        *,
        consistency_context: str = "",
    ) -> ImageAnalysis:
        """Analyze an uploaded image and return style recommendations."""


class MockAnalysisProvider(AnalysisProvider):
    """Deterministic provider for local MVP development and tests."""

    def __init__(self, *, include_monochrome: bool = False) -> None:
        self.include_monochrome = include_monochrome

    def analyze(
        self,
        image_path: Path,
        metadata: ImageMetadata,
        *,
        consistency_context: str = "",
    ) -> ImageAnalysis:
        del image_path, consistency_context
        orientation = "横幅" if metadata.width >= metadata.height else "竖幅"
        megapixels = round((metadata.width * metadata.height) / 1_000_000, 1)
        summary = (
            f"这是一张{orientation}{metadata.format}照片，约 {megapixels}MP。"
            "Mock 分析会给出可解释的修图方向，便于验证完整产品闭环。"
        )

        return ImageAnalysis(
            overall_summary=summary,
            technical_diagnosis=[
                "曝光基础稳定，适合通过轻微曝光和高光/阴影调整拉开层次。",
                "白平衡可根据风格偏暖或偏冷处理，避免单纯提高饱和度。",
                "建议保留细节锐化和适度降噪，让预览更接近 Lightroom 基础编辑效果。",
            ],
            composition_observations=[
                f"画面比例为 {metadata.width}:{metadata.height}，当前更适合{orientation}叙事。",
                "可通过裁切建议强化主体位置，但 MVP 阶段不直接裁切原图。",
                "建议优先观察主体与背景分离，再决定清晰度和暗角方向。",
            ],
            color_tone_observations=[
                "自然风格适合低饱和、轻对比，保持真实肤色或环境色。",
                "电影风格适合压高光、抬阴影，并加入冷暖分离。",
                "柔光胶片适合轻微褪色、柔和高光和克制的绿色偏移。",
            ],
            recommended_styles=[
                self._natural_style(),
                self._cinematic_style(),
                (
                    self._black_white_style()
                    if self.include_monochrome
                    else self._soft_film_style()
                ),
            ],
            analysis_version=ANALYSIS_VERSION,
        )

    def _natural_style(self) -> EditingStyle:
        return EditingStyle(
            id="natural-documentary",
            name="自然纪实",
            description="轻微提亮、柔和对比和克制色彩，保留真实现场感。",
            best_fit_reason="适合大多数照片作为安全起点，能改善层次但不改变照片性格。",
            expected_visual_impact="画面更通透，肤色和环境色保持自然。",
            rationale="以可解释的基础校正为主，减少过度风格化。",
            parameters=EditingParametersV1(
                exposure=0.25,
                contrast=12,
                highlights=-22,
                shadows=24,
                whites=8,
                blacks=-10,
                temperature=4,
                tint=1,
                vibrance=14,
                saturation=-2,
                texture=6,
                clarity=5,
                dehaze=2,
                hsl={
                    "orange": HslAdjustment(saturation=4, luminance=6),
                    "green": HslAdjustment(hue=-4, saturation=-8, luminance=2),
                    "blue": HslAdjustment(saturation=-4, luminance=-2),
                },
                color_grading=ColorGrading(
                    shadows=ColorGradingWheel(hue=220, saturation=3, luminance=0),
                    midtones=ColorGradingWheel(hue=45, saturation=2, luminance=1),
                    highlights=ColorGradingWheel(hue=52, saturation=4, luminance=2),
                    blending=45,
                    balance=5,
                ),
                sharpening=Sharpening(amount=45, radius=1.0, detail=25, masking=20),
                noise_reduction=NoiseReduction(luminance=8, color=25),
                crop_suggestion=CropSuggestion(
                    aspect_ratio="original",
                    rationale="保持原比例，先用影调和色彩强化主体。",
                ),
            ),
        )

    def _cinematic_style(self) -> EditingStyle:
        return EditingStyle(
            id="cinematic-teal-warm",
            name="电影冷暖",
            description="压高光、抬阴影，加入青色阴影和暖色高光。",
            best_fit_reason="适合有环境光和明确氛围的照片，能快速建立情绪。",
            expected_visual_impact="暗部偏青，高光偏暖，整体更有电影感。",
            rationale="通过曲线、HSL 和 Color Grading 组合形成风格，而不是只加滤镜。",
            parameters=EditingParametersV1(
                exposure=-0.05,
                contrast=24,
                highlights=-42,
                shadows=30,
                whites=-8,
                blacks=-24,
                temperature=-3,
                tint=4,
                vibrance=18,
                saturation=-6,
                texture=8,
                clarity=14,
                dehaze=8,
                tone_curve=[
                    ToneCurvePoint(x=0, y=10),
                    ToneCurvePoint(x=48, y=42),
                    ToneCurvePoint(x=128, y=126),
                    ToneCurvePoint(x=205, y=214),
                    ToneCurvePoint(x=255, y=248),
                ],
                hsl={
                    "red": HslAdjustment(saturation=-5, luminance=0),
                    "orange": HslAdjustment(hue=-3, saturation=8, luminance=4),
                    "yellow": HslAdjustment(hue=-10, saturation=-18, luminance=-2),
                    "green": HslAdjustment(hue=-20, saturation=-30, luminance=-10),
                    "aqua": HslAdjustment(hue=-12, saturation=18, luminance=-5),
                    "blue": HslAdjustment(hue=-8, saturation=12, luminance=-14),
                },
                color_grading=ColorGrading(
                    shadows=ColorGradingWheel(hue=205, saturation=18, luminance=-4),
                    midtones=ColorGradingWheel(hue=38, saturation=8, luminance=0),
                    highlights=ColorGradingWheel(hue=48, saturation=15, luminance=4),
                    blending=58,
                    balance=10,
                ),
                sharpening=Sharpening(amount=52, radius=1.0, detail=30, masking=35),
                noise_reduction=NoiseReduction(luminance=12, color=25),
                crop_suggestion=CropSuggestion(
                    aspect_ratio="16:9",
                    rationale="电影风格可尝试 16:9 裁切强化横向叙事。",
                ),
            ),
        )

    def _soft_film_style(self) -> EditingStyle:
        return EditingStyle(
            id="soft-film-muted",
            name="柔光胶片",
            description="轻褪色、柔和对比和克制的绿色偏移，保留胶片般的空气感。",
            best_fit_reason="适合光线柔和、色彩不过分抢眼的日常照片，能增加耐看的质感。",
            expected_visual_impact="高光更柔和，暗部略微抬起，颜色更克制。",
            rationale="用轻 tone curve、低饱和和少量色彩分级做出胶片感，而不是压成黑白。",
            parameters=EditingParametersV1(
                exposure=0.15,
                contrast=6,
                highlights=-30,
                shadows=28,
                whites=2,
                blacks=-6,
                temperature=5,
                tint=3,
                vibrance=6,
                saturation=-14,
                texture=3,
                clarity=6,
                dehaze=-3,
                tone_curve=[
                    ToneCurvePoint(x=0, y=12),
                    ToneCurvePoint(x=48, y=48),
                    ToneCurvePoint(x=128, y=128),
                    ToneCurvePoint(x=205, y=208),
                    ToneCurvePoint(x=255, y=244),
                ],
                hsl={
                    "orange": HslAdjustment(saturation=-4, luminance=5),
                    "yellow": HslAdjustment(hue=-4, saturation=-12, luminance=2),
                    "green": HslAdjustment(hue=-8, saturation=-18, luminance=-2),
                    "blue": HslAdjustment(saturation=-10, luminance=3),
                },
                color_grading=ColorGrading(
                    shadows=ColorGradingWheel(hue=110, saturation=7, luminance=1),
                    midtones=ColorGradingWheel(hue=45, saturation=4, luminance=1),
                    highlights=ColorGradingWheel(hue=52, saturation=5, luminance=2),
                    blending=52,
                    balance=4,
                ),
                sharpening=Sharpening(amount=38, radius=1.0, detail=22, masking=25),
                noise_reduction=NoiseReduction(luminance=10, color=25),
                crop_suggestion=CropSuggestion(
                    aspect_ratio="original",
                    rationale="保留原比例，让胶片感来自影调和色彩而不是裁切。",
                ),
            ),
        )

    def _black_white_style(self) -> EditingStyle:
        return EditingStyle(
            id="contrast-bw",
            name="高反差黑白",
            description="去除色彩干扰，加强明暗结构、纹理和主体轮廓。",
            best_fit_reason="当画面线条、光影或人物表情较强时，黑白能突出结构。",
            expected_visual_impact="色彩被去除，明暗对比增强，画面更聚焦。",
            rationale="用真正的黑白转换和强结构参数突出形状与光影。",
            parameters=EditingParametersV1(
                advanced=AdvancedParameters(black_and_white=True),
                exposure=0.1,
                contrast=38,
                highlights=-18,
                shadows=18,
                whites=18,
                blacks=-35,
                temperature=0,
                tint=0,
                vibrance=-70,
                saturation=-100,
                texture=18,
                clarity=26,
                dehaze=10,
                tone_curve=[
                    ToneCurvePoint(x=0, y=0),
                    ToneCurvePoint(x=52, y=38),
                    ToneCurvePoint(x=128, y=125),
                    ToneCurvePoint(x=200, y=218),
                    ToneCurvePoint(x=255, y=255),
                ],
                hsl={
                    "red": HslAdjustment(luminance=8),
                    "orange": HslAdjustment(luminance=10),
                    "yellow": HslAdjustment(luminance=4),
                    "blue": HslAdjustment(luminance=-12),
                },
                color_grading=ColorGrading(
                    shadows=ColorGradingWheel(hue=0, saturation=0, luminance=-2),
                    midtones=ColorGradingWheel(hue=0, saturation=0, luminance=0),
                    highlights=ColorGradingWheel(hue=0, saturation=0, luminance=2),
                    blending=50,
                    balance=0,
                ),
                sharpening=Sharpening(amount=65, radius=1.1, detail=42, masking=50),
                noise_reduction=NoiseReduction(luminance=16, color=30),
                crop_suggestion=CropSuggestion(
                    aspect_ratio="4:5",
                    rationale="竖幅或主体照片可尝试 4:5，让主体占比更稳定。",
                ),
            ),
        )
