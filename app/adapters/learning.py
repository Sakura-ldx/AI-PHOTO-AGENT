from __future__ import annotations

from abc import ABC, abstractmethod

from app.schemas import (
    LearningAnalysisResult,
    LearningReference,
    LearningReferenceAnalysis,
    LearningReferenceAnalysisResult,
    LearningSession,
    LearningSummary,
)


class LearningAnalysisProvider(ABC):
    @abstractmethod
    def analyze(self, session: LearningSession) -> LearningAnalysisResult:
        """Analyze references without returning or controlling session state."""


class MockLearningAnalysisProvider(LearningAnalysisProvider):
    """Deterministic learning provider for Phase 2 workflow validation."""

    def analyze(self, session: LearningSession) -> LearningAnalysisResult:
        analyzed_references = [
            LearningReferenceAnalysisResult(
                reference_id=reference.id,
                analysis=self._analyze_reference(session.topic, reference),
            )
            for reference in session.references
        ]
        return LearningAnalysisResult(
            references=analyzed_references,
            summary=self._summarize(session.topic, session.references),
        )

    def _analyze_reference(
        self,
        topic: str,
        reference: LearningReference,
    ) -> LearningReferenceAnalysis:
        metadata = reference.metadata
        orientation = "横向" if metadata.width >= metadata.height else "竖向"
        megapixels = round((metadata.width * metadata.height) / 1_000_000, 1)
        aspect = f"{metadata.width}:{metadata.height}"

        return LearningReferenceAnalysis(
            composition_structure=[
                f"{orientation}画幅，比例约 {aspect}，适合观察{topic}中的画面重心。",
                "Mock 分析会把主体、背景和边缘空间拆开，帮助形成可复用观察框架。",
            ],
            subject_placement=f"主体建议优先观察是否落在画面中心、三分线或负空间边界；当前文件约 {megapixels}MP。",
            visual_hierarchy=[
                "先判断最大明暗对比区域，再判断色彩最饱和区域。",
                "观察前景、中景、背景是否有明确分层，避免只看主体本身。",
            ],
            color_palette=[
                "主色：根据参考图建立 2 到 3 个主要色块。",
                "辅助色：记录与主体形成对比或呼应的环境色。",
                f"文件格式：{metadata.format}，颜色模式：{metadata.color_mode}。",
            ],
            tonal_distribution=[
                "检查高光是否承担视线引导，阴影是否保留形状信息。",
                "记录整体是高调、低调还是中间调为主。",
            ],
            mood_style_labels=[
                topic,
                "参考分析",
                "构图练习",
                "色彩观察",
            ],
            practical_lessons=[
                "临摹时先复现主体位置和明暗层级，再考虑具体调色。",
                "拍摄前明确画面里最亮、最暗、最饱和的区域分别承担什么作用。",
            ],
        )

    def _summarize(
        self,
        topic: str,
        references: list[LearningReference],
    ) -> LearningSummary:
        count = len(references)
        portrait_count = sum(
            1
            for reference in references
            if reference.metadata.height > reference.metadata.width
        )
        landscape_count = count - portrait_count

        return LearningSummary(
            style_overview=(
                f"本次主题“{topic}”共分析 {count} 张参考图。"
                "当前为 Mock 学习总结，重点是建立构图、色彩和练习输出的产品闭环。"
            ),
            common_composition_patterns=[
                f"横向参考 {landscape_count} 张，竖向参考 {portrait_count} 张，可分别比较叙事空间和主体占比。",
                "优先记录主体位置、背景分离和边缘留白，再归纳构图模式。",
                "把视觉层级拆成明暗、颜色、清晰度三个顺序观察。",
            ],
            common_color_strategies=[
                "为每张参考图记录主色、辅助色和强调色。",
                "比较高光颜色与阴影颜色是否形成冷暖关系。",
                "关注饱和度是否集中在主体或引导区域。",
            ],
            mistakes_to_avoid=[
                "不要只模仿滤镜色彩，忽略主体位置和光线方向。",
                "不要在没有来源许可信息时把参考图用于公开内容采集。",
                "不要把单张参考图的偶然特征当作整个风格规律。",
            ],
            exercises=[
                "选择一张参考图，画出主体、背景和视觉引导线。",
                "用 3 个色块概括参考图的主色策略。",
                "拍摄或编辑自己的照片时，只复现一个构图规律和一个色彩规律。",
            ],
        )
