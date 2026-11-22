"""Phase 2 修图学习总结与知识库的领域模型。

这些模型描述「每次照片分析后沉淀下来的学习总结」：
图像本身 → 分析快照 → 三个修图方向与推荐强度 → 分类标签 → 知识库汇总视图。
与旧的学习会话（app/schemas.py 里的 LearningSession 等）相互独立。
"""

from __future__ import annotations

from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.schemas import (
    AnalysisItems,
    AnalysisParagraph,
    AnalysisTitle,
    EditingParametersV1,
    ImageAnalysis,
    ImageFormat,
    ResourceId,
    StyleId,
    UTCDateTime,
    utc_now,
)

KNOWLEDGE_SUMMARY_VERSION = "photo_learning_summary_v1"
TAG_DIMENSIONS: tuple[str, ...] = (
    "subject",
    "light",
    "color",
    "tone",
    "composition",
    "issue",
    "portrait",
    "environment",
)
TAG_DIMENSION_LABELS: dict[str, str] = {
    "subject": "题材场景",
    "light": "光线",
    "color": "色彩",
    "tone": "影调",
    "composition": "构图",
    "issue": "待改善",
    "portrait": "人像风格",
    "environment": "环境背景",
}
FALLBACK_CATEGORY = "综合"
DIRECTIONS_PER_SUMMARY = 3
MAX_SUMMARY_TAGS = 12
KnowledgeDimension = Literal[
    "subject",
    "light",
    "color",
    "tone",
    "composition",
    "issue",
    "portrait",
    "environment",
]
KnowledgeTagSource = Literal["analysis", "measurement"]
KnowledgeTagLabel = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=24),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


def _validate_local_url(value: str) -> str:
    """Accept only same-origin absolute paths.

    Knowledge summaries are derived JSON on disk. If one is tampered with, a
    ``javascript:`` or protocol-relative URL must fail validation rather than
    reach the browser through a response DTO.
    """

    if any(
        character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F
        for character in value
    ):
        raise ValueError("URL must not contain whitespace or control characters")
    if "\\" in value or not value.startswith("/") or value.startswith("//"):
        raise ValueError("URL must be a same-origin absolute path")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        raise ValueError("URL must be a same-origin absolute path")
    return value


def _reject_control_characters(value: str) -> str:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("value must be a single line without control characters")
    return value


LocalUrl = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2048),
    AfterValidator(_validate_local_url),
]
SingleLineText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=240),
    AfterValidator(_reject_control_characters),
]


class KnowledgeTag(BaseModel):
    """一条分类标签：dimension 决定它在知识库里的分组。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: KnowledgeDimension
    label: KnowledgeTagLabel
    source: KnowledgeTagSource = "analysis"


class KnowledgeImage(BaseModel):
    """总结里展示图像本身所需的信息（缩略图 + 元数据）。"""

    model_config = ConfigDict(extra="forbid")

    display_url: LocalUrl
    width: int = Field(gt=0, le=200_000)
    height: int = Field(gt=0, le=200_000)
    format: ImageFormat
    color_mode: str = Field(min_length=1, max_length=40)
    file_size: int = Field(ge=0)
    original_filename: SingleLineText | None = None
    render_source: Literal["original", "embedded_preview"] = "original"


class KnowledgeAnalysisSnapshot(BaseModel):
    """分析结果的文字快照（不含 recommended_styles，方向见 directions）。"""

    model_config = ConfigDict(extra="forbid")

    overall_summary: AnalysisParagraph
    technical_diagnosis: AnalysisItems
    composition_observations: AnalysisItems
    color_tone_observations: AnalysisItems

    @classmethod
    def from_analysis(cls, analysis: ImageAnalysis) -> KnowledgeAnalysisSnapshot:
        return cls(
            overall_summary=analysis.overall_summary,
            technical_diagnosis=analysis.technical_diagnosis,
            composition_observations=analysis.composition_observations,
            color_tone_observations=analysis.color_tone_observations,
        )


class EditingDirectionSummary(BaseModel):
    """一个可执行的修图方向：风格说明 + 参数总结 + 推荐强度 + 预览。"""

    model_config = ConfigDict(extra="forbid")

    style_id: StyleId
    style_name: AnalysisTitle
    direction: AnalysisParagraph
    best_fit_reason: AnalysisParagraph
    expected_visual_impact: AnalysisParagraph
    parameters: EditingParametersV1
    parameters_summary: AnalysisParagraph
    recommended_intensity: float = Field(ge=0.0, le=1.0)
    intensity_reason: AnalysisParagraph
    effective_strength: float = Field(ge=0.0, le=1.0)
    verified: bool = True
    warnings: list[ShortText] = Field(default_factory=list)
    preview_url: LocalUrl | None = None
    preview_intensity: float | None = Field(default=None, ge=0.0, le=1.0)


class KnowledgeManualEdit(BaseModel):
    """用户主动选择过的一条修图方向（风格 + 强度），用于笔记额外展示。"""

    model_config = ConfigDict(extra="forbid")

    style_id: StyleId
    style_name: AnalysisTitle
    intensity: float = Field(ge=0.0, le=1.0)
    parameters_summary: AnalysisParagraph
    effective_strength: float = Field(ge=0.0, le=1.0)
    verified: bool = True
    warnings: list[ShortText] = Field(default_factory=list)
    preview_url: LocalUrl | None = None
    source: Literal["preview", "export"] = "preview"
    updated_at: UTCDateTime


class PhotoLearningSummary(BaseModel):
    """一张照片的完整学习总结，也是知识库里的基本条目。"""

    model_config = ConfigDict(extra="forbid")

    id: ResourceId
    version: str = KNOWLEDGE_SUMMARY_VERSION
    photo_id: ResourceId
    image: KnowledgeImage
    analysis: KnowledgeAnalysisSnapshot
    primary_category: str = Field(min_length=1, max_length=24)
    tags: list[KnowledgeTag] = Field(default_factory=list, max_length=MAX_SUMMARY_TAGS)
    directions: list[EditingDirectionSummary] = Field(
        min_length=DIRECTIONS_PER_SUMMARY, max_length=DIRECTIONS_PER_SUMMARY
    )
    overview: AnalysisParagraph
    # Not persisted inside the summary JSON: the workflow fills it from the
    # photo session on read so a rebuild can never drop the user's choice.
    manual_edit: KnowledgeManualEdit | None = None
    generated_at: UTCDateTime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def _validate_summary(self) -> PhotoLearningSummary:
        if self.id != self.photo_id:
            raise ValueError("学习总结的 id 必须与 photo_id 一致")
        style_ids = [direction.style_id for direction in self.directions]
        if len(style_ids) != len(set(style_ids)):
            raise ValueError("学习总结的修图方向 style_id 必须唯一")
        tag_keys = [(tag.dimension, tag.label) for tag in self.tags]
        if len(tag_keys) != len(set(tag_keys)):
            raise ValueError("学习总结的标签不得重复")
        return self


class KnowledgeTagCount(BaseModel):
    """某个维度下某个标签的出现次数。"""

    model_config = ConfigDict(extra="forbid")

    dimension: KnowledgeDimension
    label: KnowledgeTagLabel
    count: int = Field(ge=0)


class KnowledgeFacet(BaseModel):
    """知识库筛选用的一个维度分组（含该维度下所有标签与计数）。"""

    model_config = ConfigDict(extra="forbid")

    dimension: KnowledgeDimension
    label: str = Field(min_length=1, max_length=24)
    tags: list[KnowledgeTagCount] = Field(default_factory=list)


class KnowledgeCategoryCount(BaseModel):
    """主分类（题材/场景）的计数。"""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=24)
    count: int = Field(ge=0)


class KnowledgeStyleCount(BaseModel):
    """修图风格（图片风格）的计数，供知识库「图片风格」目录使用。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    count: int = Field(ge=0)


class KnowledgeSummaryListItem(BaseModel):
    """知识库列表项：足够渲染卡片，不携带完整参数对象。"""

    model_config = ConfigDict(extra="forbid")

    photo_id: ResourceId
    image: KnowledgeImage
    overall_summary: AnalysisParagraph
    primary_category: str = Field(min_length=1, max_length=24)
    tags: list[KnowledgeTag] = Field(default_factory=list, max_length=MAX_SUMMARY_TAGS)
    style_names: list[str] = Field(default_factory=list, max_length=DIRECTIONS_PER_SUMMARY)
    recommended_intensities: list[float] = Field(
        default_factory=list, max_length=DIRECTIONS_PER_SUMMARY
    )
    preview_url: LocalUrl | None = None
    generated_at: UTCDateTime

    @classmethod
    def from_summary(cls, summary: PhotoLearningSummary) -> KnowledgeSummaryListItem:
        preview_url = next(
            (
                direction.preview_url
                for direction in summary.directions
                if direction.preview_url
            ),
            None,
        )
        return cls(
            photo_id=summary.photo_id,
            image=summary.image,
            overall_summary=summary.analysis.overall_summary,
            primary_category=summary.primary_category,
            tags=list(summary.tags),
            style_names=[direction.style_name for direction in summary.directions],
            recommended_intensities=[
                direction.recommended_intensity for direction in summary.directions
            ],
            preview_url=preview_url,
            generated_at=summary.generated_at,
        )


class KnowledgeSummaryListResponse(BaseModel):
    """知识库列表响应：筛选后的条目 + 全量 facets（用于筛选 UI）。"""

    model_config = ConfigDict(extra="forbid")

    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    items: list[KnowledgeSummaryListItem] = Field(default_factory=list)
    facets: list[KnowledgeFacet] = Field(default_factory=list)
    primary_categories: list[KnowledgeCategoryCount] = Field(default_factory=list)
    style_tags: list[KnowledgeStyleCount] = Field(default_factory=list)


class KnowledgeOverviewResponse(BaseModel):
    """知识库总览统计。"""

    model_config = ConfigDict(extra="forbid")

    total_summaries: int = Field(ge=0)
    covered_categories: int = Field(ge=0)
    average_intensity: float | None = Field(default=None, ge=0.0, le=1.0)
    latest_generated_at: UTCDateTime | None = None
    facets: list[KnowledgeFacet] = Field(default_factory=list)
    primary_categories: list[KnowledgeCategoryCount] = Field(default_factory=list)
    style_tags: list[KnowledgeStyleCount] = Field(default_factory=list)
