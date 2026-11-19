from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from app.schemas import (
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
    def analyze(self, image_path: Path, metadata: ImageMetadata) -> ImageAnalysis:
        """Analyze an uploaded image and return style recommendations."""


class MockAnalysisProvider(AnalysisProvider):
    """Deterministic provider for local MVP development and tests."""

    def analyze(self, image_path: Path, metadata: ImageMetadata) -> ImageAnalysis:
        del image_path
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
                "黑白风格适合加强对比和局部清晰度，突出形状与光影。",
            ],
            recommended_styles=[
                self._natural_style(),
                self._cinematic_style(),
                self._black_white_style(),
            ],
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

    def _black_white_style(self) -> EditingStyle:
        return EditingStyle(
            id="contrast-bw",
            name="高反差黑白",
            description="去除色彩干扰，加强明暗结构、纹理和主体轮廓。",
            best_fit_reason="当画面线条、光影或人物表情较强时，黑白能突出结构。",
            expected_visual_impact="色彩被压低，明暗对比增强，画面更聚焦。",
            rationale="用低饱和和强结构参数模拟黑白工作流，保留可调空间。",
            parameters=EditingParametersV1(
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
