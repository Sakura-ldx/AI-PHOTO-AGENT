"""Phase 2 学习总结的分类与推荐强度（确定性纯函数）。

The knowledge base needs two things the vision model cannot supply reliably:
controlled-vocabulary tags that can be filtered, and a recommended intensity
that is justified by measured evidence instead of impression.

This module therefore provides:

1. :func:`classify_analysis` maps keyword hits from the analysis text plus
   histogram measurements into the fixed tag vocabulary defined in
   :mod:`app.knowledge_schemas` (6 dimensions, at most 2 tags per dimension and
   8 tags in total, primary category taken from the first subject tag).
2. :func:`recommend_intensity` starts from a 0.75 baseline and moves it up or
   down according to exposure key, tonal range, flatness, clipping and how
   strong the style parameters already are, then clamps to [0.4, 1.0] on a 0.05
   grid. The reason is one Chinese sentence that quotes the measured numbers.

Both functions are pure and deterministic: equal inputs always produce equal
outputs, and there is no I/O or randomness. Measurement thresholds mirror
:mod:`app.services.edit_resolver` so one photograph is described the same way
everywhere in the product.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.knowledge_schemas import (
    FALLBACK_CATEGORY,
    TAG_DIMENSIONS,
    KnowledgeDimension,
    KnowledgeTag,
    KnowledgeTagSource,
)
from app.schemas import EditingStyle, ImageAnalysis, ImageMetadata
from app.services.edit_resolver import (
    MIN_NEUTRAL_COVERAGE,
    NEUTRAL_RATIO_HIGH,
    NEUTRAL_RATIO_LOW,
)
from app.services.measurements import ImageMeasurements

__all__ = [
    "FALLBACK_CATEGORY",
    "MAX_RECOMMENDED_INTENSITY",
    "MAX_TAGS_PER_DIMENSION",
    "MAX_TAGS_TOTAL",
    "MIN_RECOMMENDED_INTENSITY",
    "OPPOSITE_LABEL_PAIRS",
    "RECOMMENDED_INTENSITY_STEP",
    "BASELINE_RECOMMENDED_INTENSITY",
    "SUMMARY_TAG_RULES",
    "TAG_LABELS_BY_DIMENSION",
    "IntensityRecommendation",
    "SummaryClassification",
    "classify_analysis",
    "recommend_intensity",
]


# --------------------------------------------------------------------------- #
# Recommendation bounds and tag budgets
# --------------------------------------------------------------------------- #

#: The recommended intensity lives on this inclusive range and moves in these
#: steps so the UI can render a stable slider position.
MIN_RECOMMENDED_INTENSITY = 0.4
MAX_RECOMMENDED_INTENSITY = 1.0
RECOMMENDED_INTENSITY_STEP = 0.05
#: The neutral recommendation before any evidence is applied.
BASELINE_RECOMMENDED_INTENSITY = 0.75

#: Same budgets as ``PhotoLearningSummary.tags`` in app/knowledge_schemas.py:
#: the schema allows 12 tags, but the classifier keeps the list short enough to
#: read, which also leaves room for other producers to append tags later.
MAX_TAGS_PER_DIMENSION = 2
MAX_TAGS_TOTAL = 8


@dataclass(frozen=True, slots=True)
class SummaryClassification:
    """Result of classifying one photo analysis.

    ``tags`` is ordered by dimension (the order in ``TAG_DIMENSIONS``) and every
    ``(dimension, label)`` pair is unique, so the value can be persisted as-is.
    """

    primary_category: str
    tags: tuple[KnowledgeTag, ...]


@dataclass(frozen=True, slots=True)
class IntensityRecommendation:
    """A recommended editing intensity plus the Chinese reason behind it."""

    intensity: float
    reason: str


# --------------------------------------------------------------------------- #
# Controlled vocabulary (one fixed label list per dimension)
# --------------------------------------------------------------------------- #

SUBJECT_LABELS: tuple[str, ...] = (
    "人像",
    "风光",
    "建筑",
    "街头",
    "静物",
    "花卉",
    "动物",
    "美食",
    "旅行",
    "夜景",
    "室内",
    "微距",
)
LIGHT_LABELS: tuple[str, ...] = (
    "逆光",
    "侧光",
    "顺光",
    "柔光",
    "硬光",
    "自然光",
    "黄金时刻",
    "蓝调时刻",
    "弱光",
    "影棚",
    "顶光",
    "窗光",
)
COLOR_LABELS: tuple[str, ...] = (
    "暖色",
    "冷色",
    "高饱和",
    "低饱和",
    "单色",
    "色彩丰富",
    "偏色",
    "蓝色调",
    "绿色调",
    "橙青",
    "肤色",
    "互补色",
)
TONE_LABELS: tuple[str, ...] = (
    "高调",
    "低调",
    "中间调",
    "高对比",
    "低对比",
    "灰雾",
    "曝光充足",
    "通透",
    "浓郁",
    "柔和影调",
    "层次丰富",
    "戏剧化",
)
COMPOSITION_LABELS: tuple[str, ...] = (
    "中心构图",
    "三分法",
    "对称构图",
    "引导线",
    "框架构图",
    "前景",
    "留白",
    "纵深",
    "低角度",
    "高角度",
    "横幅",
    "竖幅",
)
ISSUE_LABELS: tuple[str, ...] = (
    "曝光不足",
    "曝光过度",
    "高光溢出",
    "暗部死黑",
    "对比不足",
    "噪点",
    "失焦",
    "动态范围不足",
    "锐度不足",
    "构图杂乱",
    "过饱和",
    "白平衡异常",
)

TAG_LABELS_BY_DIMENSION: dict[str, tuple[str, ...]] = {
    "subject": SUBJECT_LABELS,
    "light": LIGHT_LABELS,
    "color": COLOR_LABELS,
    "tone": TONE_LABELS,
    "composition": COMPOSITION_LABELS,
    "issue": ISSUE_LABELS,
}


#: Keyword table for the analysis text. Chinese labels and both Chinese and
#: English keywords are supported; the ASCII keywords are matched on word
#: boundaries so "sea" does not fire on "seaside" and "model" not on "modeling".
SUMMARY_TAG_RULES: tuple[tuple[KnowledgeDimension, str, tuple[str, ...]], ...] = (
    # ------------------------------------------------------------------ 题材场景
    (
        "subject",
        "人像",
        (
            "人像",
            "肖像",
            "人物",
            "模特",
            "面部特写",
            "portrait",
            "portraiture",
            "headshot",
        ),
    ),
    (
        "subject",
        "风光",
        (
            "风光",
            "风景",
            "山景",
            "海景",
            "湖景",
            "山川",
            "草原",
            "晚霞",
            "日出",
            "日落",
            "landscape",
            "scenery",
            "mountain",
            "seascape",
            "sunset",
            "sunrise",
        ),
    ),
    (
        "subject",
        "建筑",
        (
            "建筑",
            "城市",
            "楼宇",
            "高楼",
            "桥梁",
            "architecture",
            "building",
            "city",
            "urban",
        ),
    ),
    (
        "subject",
        "街头",
        (
            "街拍",
            "街头",
            "市井",
            "street photography",
            "candid",
        ),
    ),
    (
        "subject",
        "静物",
        (
            "静物",
            "产品",
            "摆拍",
            "still life",
            "product photo",
            "flat lay",
        ),
    ),
    (
        "subject",
        "花卉",
        (
            "花卉",
            "花朵",
            "花丛",
            "植物",
            "绿植",
            "flower",
            "floral",
            "plant",
            "botanical",
        ),
    ),
    (
        "subject",
        "动物",
        (
            "动物",
            "宠物",
            "鸟类",
            "野生动物",
            "猫",
            "狗",
            "animal",
            "wildlife",
            "bird",
            "pet",
            "cat",
            "dog",
        ),
    ),
    (
        "subject",
        "美食",
        (
            "美食",
            "食物",
            "菜品",
            "料理",
            "甜点",
            "food",
            "cuisine",
            "dish",
            "dessert",
        ),
    ),
    (
        "subject",
        "旅行",
        (
            "旅行",
            "旅途",
            "旅拍",
            "travel",
            "trip",
            "journey",
        ),
    ),
    (
        "subject",
        "夜景",
        (
            "夜景",
            "星空",
            "星轨",
            "银河",
            "夜色",
            "nightscape",
            "starry",
            "astro photography",
        ),
    ),
    (
        "subject",
        "室内",
        (
            "室内",
            "家居",
            "房间",
            "interior",
            "indoor",
            "living room",
        ),
    ),
    (
        "subject",
        "微距",
        (
            "微距",
            "macro",
            "close-up",
            "closeup",
        ),
    ),
    # ---------------------------------------------------------------------- 光线
    (
        "light",
        "逆光",
        (
            "逆光",
            "背光",
            "backlight",
            "backlit",
            "contre-jour",
        ),
    ),
    (
        "light",
        "侧光",
        (
            "侧光",
            "侧逆光",
            "sidelight",
            "side light",
            "side lighting",
        ),
    ),
    (
        "light",
        "顺光",
        (
            "顺光",
            "frontlight",
            "front light",
            "front lighting",
        ),
    ),
    (
        "light",
        "柔光",
        (
            "柔光",
            "漫射光",
            "阴天光",
            "soft light",
            "diffused light",
            "overcast light",
        ),
    ),
    (
        "light",
        "硬光",
        (
            "硬光",
            "直射光",
            "hard light",
            "harsh light",
        ),
    ),
    (
        "light",
        "自然光",
        (
            "自然光",
            "日光",
            "natural light",
            "daylight",
        ),
    ),
    (
        "light",
        "黄金时刻",
        (
            "黄金时刻",
            "黄金时段",
            "金色时刻",
            "golden hour",
            "golden light",
        ),
    ),
    (
        "light",
        "蓝调时刻",
        (
            "蓝调时刻",
            "蓝调",
            "blue hour",
        ),
    ),
    (
        "light",
        "弱光",
        (
            "弱光",
            "低光",
            "暗光",
            "low light",
            "low-light",
            "dim light",
        ),
    ),
    (
        "light",
        "影棚",
        (
            "影棚",
            "棚拍",
            "布光",
            "studio light",
            "studio lighting",
            "strobe",
        ),
    ),
    (
        "light",
        "顶光",
        (
            "顶光",
            "顶部光",
            "overhead light",
            "top light",
        ),
    ),
    (
        "light",
        "窗光",
        (
            "窗光",
            "窗户光",
            "window light",
        ),
    ),
    # ---------------------------------------------------------------------- 色彩
    (
        "color",
        "暖色",
        (
            "暖色",
            "暖调",
            "偏暖",
            "温暖",
            "金黄色调",
            "warm",
            "warm tone",
            "warm color",
            "golden tone",
        ),
    ),
    (
        "color",
        "冷色",
        (
            "冷色",
            "冷调",
            "偏冷",
            "清冷",
            "cool tone",
            "cool color",
            "bluish",
        ),
    ),
    (
        "color",
        "高饱和",
        (
            "高饱和",
            "饱和度偏高",
            "饱和度高",
            "色彩浓烈",
            "vivid color",
            "high saturation",
            "highly saturated",
            "saturated color",
        ),
    ),
    (
        "color",
        "低饱和",
        (
            "低饱和",
            "饱和度偏低",
            "饱和度低",
            "淡雅",
            "素雅",
            "muted color",
            "low saturation",
            "desaturated",
        ),
    ),
    (
        "color",
        "单色",
        (
            "单色",
            "黑白",
            "灰度",
            "black and white",
            "black-and-white",
            "monochrome",
            "grayscale",
            "greyscale",
        ),
    ),
    (
        "color",
        "色彩丰富",
        (
            "色彩丰富",
            "色彩斑斓",
            "色彩缤纷",
            "丰富色彩",
            "colorful",
            "rich color",
            "vibrant palette",
        ),
    ),
    (
        "color",
        "偏色",
        (
            "偏色",
            "色偏",
            "color cast",
            "color shift",
        ),
    ),
    (
        "color",
        "蓝色调",
        (
            "蓝色调",
            "冷蓝",
            "青蓝",
            "blue tone",
            "blue tint",
        ),
    ),
    (
        "color",
        "绿色调",
        (
            "绿色调",
            "绿意",
            "绿色为主",
            "green tone",
            "green tint",
            "verdant",
        ),
    ),
    (
        "color",
        "橙青",
        (
            "橙青",
            "青橙",
            "橙蓝",
            "teal and orange",
            "orange and teal",
            "teal-orange",
        ),
    ),
    (
        "color",
        "肤色",
        (
            "肤色",
            "皮肤色调",
            "skintone",
            "skin tone",
            "skin color",
        ),
    ),
    (
        "color",
        "互补色",
        (
            "互补色",
            "complementary color",
            "complementary colors",
        ),
    ),
    # ---------------------------------------------------------------------- 影调
    (
        "tone",
        "高调",
        (
            "高调",
            "高明度",
            "亮调",
            "high key",
            "high-key",
            "bright airy",
        ),
    ),
    (
        "tone",
        "低调",
        (
            "低调",
            "低明度",
            "暗调",
            "low key",
            "low-key",
            "dark moody",
        ),
    ),
    (
        "tone",
        "中间调",
        (
            "中间调",
            "中调",
            "mid key",
            "mid-key",
            "midtone",
            "mid-tone",
        ),
    ),
    (
        "tone",
        "高对比",
        (
            "高对比",
            "对比强烈",
            "强对比",
            "反差强烈",
            "high contrast",
            "strong contrast",
            "punchy contrast",
        ),
    ),
    (
        "tone",
        "低对比",
        (
            "低对比",
            "对比偏低",
            "对比偏弱",
            "弱对比",
            "柔和对比",
            "low contrast",
            "soft contrast",
            "flat contrast",
        ),
    ),
    (
        "tone",
        "灰雾",
        (
            "灰雾",
            "发灰",
            "灰蒙蒙",
            "雾感",
            "hazy",
            "haze",
            "milky blacks",
        ),
    ),
    (
        "tone",
        "曝光充足",
        (
            "曝光充足",
            "曝光准确",
            "曝光正常",
            "曝光合适",
            "well exposed",
            "properly exposed",
            "correct exposure",
        ),
    ),
    (
        "tone",
        "通透",
        (
            "通透",
            "通透明亮",
            "干净通透",
        ),
    ),
    (
        "tone",
        "浓郁",
        (
            "浓郁",
            "影调厚重",
            "厚重影调",
            "深邃",
            "rich tone",
            "deep tones",
        ),
    ),
    (
        "tone",
        "柔和影调",
        (
            "柔和影调",
            "影调柔和",
            "柔和平缓",
            "soft tone",
            "gentle tone",
            "smooth tones",
        ),
    ),
    (
        "tone",
        "层次丰富",
        (
            "层次丰富",
            "影调层次",
            "层次分明",
            "rich tonal range",
            "tonal gradation",
        ),
    ),
    (
        "tone",
        "戏剧化",
        (
            "戏剧化",
            "戏剧性",
            "dramatic",
        ),
    ),
    # ---------------------------------------------------------------------- 构图
    (
        "composition",
        "中心构图",
        (
            "中心构图",
            "居中构图",
            "中央构图",
            "主体居中",
            "central composition",
            "centered composition",
        ),
    ),
    (
        "composition",
        "三分法",
        (
            "三分法",
            "三分构图",
            "三分法则",
            "rule of thirds",
            "rule-of-thirds",
        ),
    ),
    (
        "composition",
        "对称构图",
        (
            "对称构图",
            "对称",
            "镜像",
            "symmetry",
            "symmetrical",
            "mirrored",
        ),
    ),
    (
        "composition",
        "引导线",
        (
            "引导线",
            "线条引导",
            "leading line",
            "leading lines",
            "converging lines",
        ),
    ),
    (
        "composition",
        "框架构图",
        (
            "框架构图",
            "框架式",
            "前景框架",
            "frame within a frame",
            "framed composition",
        ),
    ),
    (
        "composition",
        "前景",
        (
            "前景",
            "前景元素",
            "foreground",
            "foreground element",
        ),
    ),
    (
        "composition",
        "留白",
        (
            "留白",
            "负空间",
            "negative space",
            "empty space",
        ),
    ),
    (
        "composition",
        "纵深",
        (
            "纵深",
            "空间纵深",
            "景深层次",
            "sense of depth",
            "depth cues",
            "layered depth",
        ),
    ),
    (
        "composition",
        "低角度",
        (
            "低角度",
            "仰拍",
            "低机位",
            "low angle",
            "low-angle",
            "from below",
        ),
    ),
    (
        "composition",
        "高角度",
        (
            "高角度",
            "俯拍",
            "高机位",
            "航拍",
            "high angle",
            "high-angle",
            "from above",
            "bird's eye",
        ),
    ),
    # -------------------------------------------------------------------- 待改善
    (
        "issue",
        "曝光不足",
        (
            "曝光不足",
            "欠曝",
            "曝光偏暗",
            "画面偏暗",
            "整体偏暗",
            "太暗",
            "underexposed",
            "underexposure",
            "too dark",
        ),
    ),
    (
        "issue",
        "曝光过度",
        (
            "曝光过度",
            "过曝",
            "曝光偏亮",
            "整体偏亮",
            "太亮",
            "overexposed",
            "overexposure",
            "too bright",
        ),
    ),
    (
        "issue",
        "高光溢出",
        (
            "高光溢出",
            "高光过曝",
            "高光过亮",
            "高光剪切",
            "高光丢失",
            "highlight clipping",
            "clipped highlight",
            "blown highlight",
        ),
    ),
    (
        "issue",
        "暗部死黑",
        (
            "暗部死黑",
            "暗部压死",
            "死黑",
            "阴影死黑",
            "黑场压死",
            "crushed black",
            "crushed shadow",
            "clipped shadow",
        ),
    ),
    (
        "issue",
        "对比不足",
        (
            "对比不足",
            "缺乏对比",
            "反差不足",
            "画面发灰",
            "washed out",
            "muddy",
            "flat image",
        ),
    ),
    (
        "issue",
        "噪点",
        (
            "噪点",
            "噪波",
            "高感噪点",
            "noise",
            "noisy",
            "grain",
            "grainy",
        ),
    ),
    (
        "issue",
        "失焦",
        (
            "失焦",
            "虚焦",
            "对焦不准",
            "模糊",
            "不清晰",
            "out of focus",
            "blurry",
            "blurred",
        ),
    ),
    (
        "issue",
        "动态范围不足",
        (
            "动态范围不足",
            "宽容度不足",
            "动态范围有限",
            "limited dynamic range",
        ),
    ),
    (
        "issue",
        "锐度不足",
        (
            "锐度不足",
            "锐利度不足",
            "不够锐",
            "not sharp",
            "lacks sharpness",
        ),
    ),
    (
        "issue",
        "构图杂乱",
        (
            "构图杂乱",
            "画面杂乱",
            "杂乱",
            "主体不突出",
            "主体不明确",
            "cluttered",
            "busy background",
        ),
    ),
    (
        "issue",
        "过饱和",
        (
            "过饱和",
            "饱和度过高",
            "oversaturated",
            "too saturated",
            "oversaturation",
        ),
    ),
    (
        "issue",
        "白平衡异常",
        (
            "白平衡异常",
            "白平衡不准",
            "白平衡错误",
            "white balance off",
            "bad white balance",
            "incorrect white balance",
        ),
    ),
)


# --------------------------------------------------------------------------- #
# Measurement thresholds (kept aligned with app.services.edit_resolver)
# --------------------------------------------------------------------------- #

#: ``edit_resolver`` adds contrast below 90 and backs it off above 210: outside
#: that window the tonal range is what the tag should describe.
MIN_USABLE_TONE_RANGE = 90
WIDE_TONE_RANGE = 210
#: ``edit_resolver`` starts dehazing at this share of pixels near the median.
FLAT_SHARE_THRESHOLD = 0.30
#: Highlight clipping becomes an "issue" once ``edit_resolver`` would start
#: recovering highlights (>1%); crushed shadows follow the 2% wording in
#: measurements._build_notes ("已压成纯黑").
HIGHLIGHT_CLIP_THRESHOLD = 0.01
SHADOW_CLIP_THRESHOLD = 0.02
#: Saturation thresholds used by ``edit_resolver.baseline_corrections``.
SATURATION_HIGH = 160.0
SATURATION_LOW = 70.0
#: A frame this much wider than tall is described as 横幅; the reciprocal is
#: 竖幅. Anything in between is left untagged rather than guessed.
LANDSCAPE_ASPECT_THRESHOLD = 1.15

#: Measured mid tone (positive = darker than middle grey, see
#: ``ImageMeasurements.mid_tone_ev_delta``) that counts as "too dark to lift"
#: rather than "already correctly exposed".
MID_TONE_DARK_EV = 0.5
MID_TONE_BRIGHT_EV = -0.5

#: Intensity movements. The names keep the reason text and the arithmetic from
#: drifting apart.
HIGH_KEY_PENALTY = 0.15
LOW_KEY_BONUS = 0.15
DARK_MIDTONE_BONUS = 0.10
EXPOSURE_FINE_PENALTY = 0.05
NARROW_RANGE_BONUS = 0.10
WIDE_RANGE_PENALTY = 0.05
FLAT_SHARE_BONUS = 0.05
HIGHLIGHT_CLIP_PENALTY = 0.10
SHADOW_CLIP_PENALTY = 0.05
STRONG_STYLE_PENALTY = 0.10
MODERATE_STYLE_PENALTY = 0.05
STRONG_STYLE_THRESHOLD = 0.75
MODERATE_STYLE_THRESHOLD = 0.45

#: Scalar sliders that make a style "strong", with the value that counts as
#: full strength for each (exposure is +/- EV, the rest are +/- Lightroom units).
STYLE_STRENGTH_FIELDS: tuple[tuple[str, str, float], ...] = (
    ("exposure", "曝光", 2.0),
    ("contrast", "对比度", 100.0),
    ("highlights", "高光", 100.0),
    ("shadows", "阴影", 100.0),
    ("whites", "白色", 100.0),
    ("blacks", "黑色", 100.0),
    ("temperature", "色温", 100.0),
    ("tint", "色调", 100.0),
    ("vibrance", "自然饱和度", 100.0),
    ("saturation", "饱和度", 100.0),
    ("texture", "纹理", 100.0),
    ("clarity", "清晰度", 100.0),
    ("dehaze", "去雾", 100.0),
)

#: Word-boundary patterns for ASCII keywords, built once at import time so the
#: functions stay pure (no runtime cache mutation).
_ASCII_KEYWORD_PATTERNS: dict[str, re.Pattern[str]] = {
    keyword: re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])")
    for _dimension, _label, keywords in SUMMARY_TAG_RULES
    for keyword in keywords
    if keyword.isascii()
}


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


#: Labels of the same dimension that must never describe one photograph together.
#: The analysis text is written before the measurements are known, so it can
#: hedge ("偏暖或偏冷", "加强对比") while the histogram only speaks for one side.
#: ("高光溢出", "暗部死黑") is deliberately absent: a bright window and a dark
#: foreground can both be clipped in the same frame, so those two may coexist.
OPPOSITE_LABEL_PAIRS: dict[KnowledgeDimension, tuple[tuple[str, str], ...]] = {
    "color": (("暖色", "冷色"), ("高饱和", "低饱和")),
    "tone": (("高调", "低调"), ("高对比", "低对比")),
    "composition": (("横幅", "竖幅"),),
    "issue": (("曝光不足", "曝光过度"),),
}


def classify_analysis(
    analysis: ImageAnalysis,
    measurements: ImageMeasurements,
    metadata: ImageMetadata,
) -> SummaryClassification:
    """Turn one analysis into a primary category and a short tag list.

    Text keywords contribute labels first (``source="analysis"``), then the
    histogram measurements supplement them (``source="measurement"``) without
    duplicating a label the text already produced. Metadata contributes the
    orientation of the frame as a composition tag.

    Contradictory labels from :data:`OPPOSITE_LABEL_PAIRS` are resolved before
    the per-dimension and total budgets are applied: a measurement outranks the
    model's wording, and two text tags cancel each other because the prose is
    describing possibilities rather than judging this particular photograph.
    Resolving before budgeting matters: when the analysis filled a bucket, a
    measurement that contradicts one of those tags must still be able to replace
    it instead of being silently dropped.
    """

    text = _analysis_text(analysis)
    candidates: list[KnowledgeTag] = []
    seen: set[tuple[str, str]] = set()

    def _collect(
        dimension: KnowledgeDimension,
        label: str,
        source: KnowledgeTagSource,
    ) -> None:
        key = (dimension, label)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            KnowledgeTag(dimension=dimension, label=label, source=source)
        )

    for dimension, label, keywords in SUMMARY_TAG_RULES:
        if any(_contains_keyword(text, keyword) for keyword in keywords):
            _collect(dimension, label, "analysis")

    for dimension, label in _measurement_tags(measurements):
        _collect(dimension, label, "measurement")

    for dimension, label in _metadata_tags(metadata):
        _collect(dimension, label, "measurement")

    resolved = _resolve_opposite_tags(candidates)

    buckets: dict[str, list[KnowledgeTag]] = {
        dimension: [] for dimension in TAG_DIMENSIONS
    }
    for tag in resolved:
        bucket = buckets[tag.dimension]
        if len(bucket) < MAX_TAGS_PER_DIMENSION:
            bucket.append(tag)

    ordered = tuple(
        tag for dimension in TAG_DIMENSIONS for tag in buckets[dimension]
    )[:MAX_TAGS_TOTAL]
    primary_category = next(
        (tag.label for tag in ordered if tag.dimension == "subject"),
        FALLBACK_CATEGORY,
    )
    return SummaryClassification(primary_category=primary_category, tags=ordered)


def _resolve_opposite_tags(
    candidates: list[KnowledgeTag],
) -> list[KnowledgeTag]:
    """Drop the losing side of every contradictory label pair.

    A pair whose two tags have different sources keeps the measurement: the
    histogram is evidence about this file, the text is the model's prose. A pair
    from the same source is ambiguous rather than decisive, so both sides are
    dropped; a dimension may legitimately end up empty.
    """

    by_key = {(tag.dimension, tag.label): tag for tag in candidates}
    dropped: set[tuple[str, str]] = set()

    for dimension, pairs in OPPOSITE_LABEL_PAIRS.items():
        for left, right in pairs:
            left_tag = by_key.get((dimension, left))
            right_tag = by_key.get((dimension, right))
            if left_tag is None or right_tag is None:
                continue
            if left_tag.source == right_tag.source:
                dropped.add((dimension, left))
                dropped.add((dimension, right))
            else:
                analysis_tag = (
                    left_tag if left_tag.source == "analysis" else right_tag
                )
                dropped.add((analysis_tag.dimension, analysis_tag.label))

    return [
        tag for tag in candidates if (tag.dimension, tag.label) not in dropped
    ]


def _analysis_text(analysis: ImageAnalysis) -> str:
    """Join every analysed field into one lower-cased haystack."""

    parts = [
        analysis.overall_summary,
        *analysis.technical_diagnosis,
        *analysis.composition_observations,
        *analysis.color_tone_observations,
    ]
    return "\n".join(part.strip().lower() for part in parts if part.strip())


def _contains_keyword(text: str, keyword: str) -> bool:
    """CJK keywords match as substrings, ASCII keywords only as whole words."""

    pattern = _ASCII_KEYWORD_PATTERNS.get(keyword)
    if pattern is not None:
        return pattern.search(text) is not None
    return keyword in text


def _measurement_tags(
    measurements: ImageMeasurements,
) -> tuple[tuple[KnowledgeDimension, str], ...]:
    """Tags deduced from the histogram rather than from the model's words."""

    tags: list[tuple[KnowledgeDimension, str]] = []
    if measurements.is_high_key:
        tags.append(("tone", "高调"))
    elif measurements.is_low_key:
        tags.append(("tone", "低调"))

    span = measurements.luma.range_90
    if span < MIN_USABLE_TONE_RANGE:
        tags.append(("tone", "低对比"))
    elif span > WIDE_TONE_RANGE:
        tags.append(("tone", "高对比"))

    if measurements.flat_share > FLAT_SHARE_THRESHOLD:
        tags.append(("tone", "灰雾"))

    if (
        measurements.neutral_coverage >= MIN_NEUTRAL_COVERAGE
        and (
            measurements.warm_bias > NEUTRAL_RATIO_HIGH
            or measurements.warm_bias < NEUTRAL_RATIO_LOW
        )
    ):
        tags.append(("color", "偏色"))
    if measurements.saturation_mean > SATURATION_HIGH:
        tags.append(("color", "高饱和"))
    elif measurements.saturation_mean < SATURATION_LOW:
        tags.append(("color", "低饱和"))

    if measurements.clipped_highlights_share > HIGHLIGHT_CLIP_THRESHOLD:
        tags.append(("issue", "高光溢出"))
    if measurements.clipped_shadows_share > SHADOW_CLIP_THRESHOLD:
        tags.append(("issue", "暗部死黑"))
    if measurements.flat_share > FLAT_SHARE_THRESHOLD:
        tags.append(("issue", "对比不足"))
    return tuple(tags)


def _metadata_tags(
    metadata: ImageMetadata,
) -> tuple[tuple[KnowledgeDimension, str], ...]:
    """One composition tag describing the frame orientation."""

    if metadata.width <= 0 or metadata.height <= 0:
        return ()
    aspect = metadata.width / metadata.height
    if aspect >= LANDSCAPE_ASPECT_THRESHOLD:
        return (("composition", "横幅"),)
    if aspect <= 1.0 / LANDSCAPE_ASPECT_THRESHOLD:
        return (("composition", "竖幅"),)
    return ()


# --------------------------------------------------------------------------- #
# Intensity recommendation
# --------------------------------------------------------------------------- #


def recommend_intensity(
    measurements: ImageMeasurements,
    style: EditingStyle,
) -> IntensityRecommendation:
    """Recommend an intensity in [0.4, 1.0] from measurements and style strength.

    The reason always quotes the numbers it was derived from, because the user
    needs to see why the slider was moved.
    """

    score = BASELINE_RECOMMENDED_INTENSITY
    evidence: list[str] = []
    luma = measurements.luma
    mid_tone_ev = measurements.mid_tone_ev_delta

    if measurements.is_high_key:
        score -= HIGH_KEY_PENALTY
        evidence.append(f"画面整体偏亮（中位 {luma.p50}），建议降低强度")
    elif measurements.is_low_key:
        score += LOW_KEY_BONUS
        evidence.append(f"画面整体偏暗（中位 {luma.p50}），建议提高强度")
    elif mid_tone_ev >= MID_TONE_DARK_EV:
        score += DARK_MIDTONE_BONUS
        evidence.append(
            f"中间调偏暗（相对 18% 灰 {mid_tone_ev:+.2f} EV），建议提高强度"
        )
    elif mid_tone_ev <= MID_TONE_BRIGHT_EV:
        score -= EXPOSURE_FINE_PENALTY
        evidence.append(
            f"中间调偏亮（相对 18% 灰 {mid_tone_ev:+.2f} EV），建议略降强度"
        )
    else:
        score -= EXPOSURE_FINE_PENALTY
        evidence.append(
            f"曝光接近标准中灰（相对 18% 灰 {mid_tone_ev:+.2f} EV），建议略降强度"
        )

    span = luma.range_90
    if span < MIN_USABLE_TONE_RANGE:
        score += NARROW_RANGE_BONUS
        evidence.append(f"影调层次偏平（p95-p05={span}），建议提高强度")
    elif span > WIDE_TONE_RANGE:
        score -= WIDE_RANGE_PENALTY
        evidence.append(f"原图对比已经很足（p95-p05={span}），建议略降强度")
    elif measurements.flat_share > FLAT_SHARE_THRESHOLD:
        score += FLAT_SHARE_BONUS
        evidence.append(
            f"像素集中在中间调（平场占比 {measurements.flat_share * 100:.0f}%），"
            "建议提高强度"
        )

    clipped_highlights = measurements.clipped_highlights_share
    if clipped_highlights > HIGHLIGHT_CLIP_THRESHOLD:
        score -= HIGHLIGHT_CLIP_PENALTY
        evidence.append(
            f"原图高光有 {clipped_highlights * 100:.1f}% 溢出，避免满强度"
        )

    clipped_shadows = measurements.clipped_shadows_share
    if clipped_shadows > SHADOW_CLIP_THRESHOLD:
        score -= SHADOW_CLIP_PENALTY
        evidence.append(
            f"原图暗部已有 {clipped_shadows * 100:.1f}% 压成纯黑，强度需要收敛"
        )

    strength, field_label, field_value = _style_strength(style)
    if strength >= STRONG_STYLE_THRESHOLD:
        score -= STRONG_STYLE_PENALTY
        evidence.append(
            f"风格参数本身偏强（{field_label} {field_value}），建议降低强度"
        )
    elif strength >= MODERATE_STYLE_THRESHOLD:
        score -= MODERATE_STYLE_PENALTY
        evidence.append(
            f"风格参数偏强（{field_label} {field_value}），建议略降强度"
        )

    reason = "；".join(evidence) + "。"
    return IntensityRecommendation(
        intensity=_quantize_intensity(score),
        reason=reason,
    )


def _style_strength(style: EditingStyle) -> tuple[float, str, str]:
    """Return ``(strength 0..1, slider label, formatted value)`` for a style.

    The strongest single slider wins rather than a sum, so a style that leans on
    one extreme slider is still recognised as strong. Ties keep the first field
    in ``STYLE_STRENGTH_FIELDS``, which keeps the wording deterministic.
    """

    parameters = style.parameters
    best_strength = 0.0
    best_label = ""
    best_value = ""
    for field, label, full_scale in STYLE_STRENGTH_FIELDS:
        value = float(getattr(parameters, field))
        strength = min(1.0, abs(value) / full_scale)
        if strength > best_strength:
            best_strength = strength
            best_label = label
            best_value = _format_style_value(field, value)
    return best_strength, best_label, best_value


def _format_style_value(field: str, value: float) -> str:
    if field == "exposure":
        return f"{value:+.2f} EV"
    return f"{int(round(value)):+d}"


def _quantize_intensity(score: float) -> float:
    """Clamp to the supported range and snap to the 0.05 step."""

    bounded = max(MIN_RECOMMENDED_INTENSITY, min(MAX_RECOMMENDED_INTENSITY, score))
    steps = round(
        (bounded - MIN_RECOMMENDED_INTENSITY) / RECOMMENDED_INTENSITY_STEP
    )
    return round(
        MIN_RECOMMENDED_INTENSITY + steps * RECOMMENDED_INTENSITY_STEP, 2
    )
