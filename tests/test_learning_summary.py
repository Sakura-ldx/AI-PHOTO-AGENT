"""Phase 2 学习总结模块的单元测试（确定性、受控词表、强度区间与证据）。"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from app.knowledge_schemas import FALLBACK_CATEGORY, TAG_DIMENSIONS
from app.schemas import EditingParametersV1, EditingStyle, ImageAnalysis, ImageMetadata
from app.services.learning_summary import (
    BASELINE_RECOMMENDED_INTENSITY,
    MAX_RECOMMENDED_INTENSITY,
    MAX_TAGS_TOTAL,
    MIN_RECOMMENDED_INTENSITY,
    OPPOSITE_LABEL_PAIRS,
    RECOMMENDED_INTENSITY_STEP,
    TAG_LABELS_BY_DIMENSION,
    classify_analysis,
    recommend_intensity,
)
from app.services.measurements import ChannelStats, ImageMeasurements


def _channel(
    *,
    p50: int = 128,
    p05: int = 20,
    p95: int = 230,
    clip_low: float = 0.0,
    clip_high: float = 0.0,
) -> ChannelStats:
    """Build a luma channel whose 90% range is 210 by default (no range tag)."""

    return ChannelStats(
        mean=float(p50),
        std=56.0,
        p01=max(0, p05 - 10),
        p05=p05,
        p25=max(0, (p05 + p50) // 2),
        p50=p50,
        p75=min(255, (p50 + p95) // 2),
        p95=p95,
        p99=min(255, p95 + 8),
        clip_low=clip_low,
        clip_high=clip_high,
    )


def _measurements(
    *,
    p50: int = 128,
    p05: int = 20,
    p95: int = 230,
    clip_low: float = 0.0,
    clip_high: float = 0.0,
    saturation: float = 90.0,
    neutral_red: float = 1.0,
    neutral_blue: float = 1.0,
    neutral_coverage: float = 0.0,
    flat_share: float = 0.10,
) -> ImageMeasurements:
    channel = _channel(
        p50=p50,
        p05=p05,
        p95=p95,
        clip_low=clip_low,
        clip_high=clip_high,
    )
    return ImageMeasurements(
        version="test_measurements_v1",
        width=512,
        height=341,
        luma=channel,
        red=channel,
        green=channel,
        blue=channel,
        saturation_mean=saturation,
        chroma_mean=40.0,
        neutral_red_ratio=neutral_red,
        neutral_blue_ratio=neutral_blue,
        neutral_coverage=neutral_coverage,
        deep_shadow_share=0.05,
        bright_highlight_share=0.01,
        flat_share=flat_share,
    )


def _style(
    style_id: str = "style_a",
    parameters: EditingParametersV1 | None = None,
) -> EditingStyle:
    return EditingStyle(
        id=style_id,
        name="测试风格",
        description="用于单元测试的修图风格",
        best_fit_reason="适合测试照片",
        parameters=parameters if parameters is not None else EditingParametersV1(),
        expected_visual_impact="画面更通透",
        rationale="单元测试用途",
    )


def _analysis(
    *,
    overall: str = "照片整体表现平稳",
    technical: tuple[str, ...] = ("细节保留完好",),
    composition: tuple[str, ...] = ("主体安排合理",),
    color_tone: tuple[str, ...] = ("色彩过渡自然",),
) -> ImageAnalysis:
    return ImageAnalysis(
        overall_summary=overall,
        technical_diagnosis=list(technical),
        composition_observations=list(composition),
        color_tone_observations=list(color_tone),
        recommended_styles=[
            _style("style_a"),
            _style("style_b"),
            _style("style_c"),
        ],
    )


def _metadata(width: int = 4032, height: int = 3024) -> ImageMetadata:
    return ImageMetadata(
        width=width,
        height=height,
        format="JPEG",
        color_mode="RGB",
        file_size=5_000_000,
        content_type="image/jpeg",
    )


def _tag_keys(classification) -> set[tuple[str, str]]:
    return {(tag.dimension, tag.label) for tag in classification.tags}


# --------------------------------------------------------------------------- #
# Classification: keyword mapping
# --------------------------------------------------------------------------- #


def test_chinese_keywords_map_to_controlled_tags() -> None:
    analysis = _analysis(
        overall="这是一张风光照片，拍摄于日落时分的逆光环境",
        technical=("画面整体偏暗，暗部细节不足",),
        composition=("采用三分法构图，前景有引导线",),
        color_tone=("色调偏暖，饱和度偏高",),
    )
    result = classify_analysis(analysis, _measurements(), _metadata())

    keys = _tag_keys(result)
    assert ("subject", "风光") in keys
    assert ("light", "逆光") in keys
    assert ("color", "暖色") in keys
    assert ("color", "高饱和") in keys
    assert ("composition", "三分法") in keys
    assert ("composition", "引导线") in keys
    assert ("issue", "曝光不足") in keys
    assert result.primary_category == "风光"
    # All tags came from the text here: the measurements are neutral.
    assert all(tag.source == "analysis" for tag in result.tags)


def test_english_keywords_map_to_controlled_tags() -> None:
    analysis = _analysis(
        overall=(
            "A landscape photo shot during golden hour with strong backlight; "
            "the frame follows the rule of thirds."
        ),
        technical=("The frame is underexposed.",),
        composition=("Leading lines pull the eye into the scene.",),
        color_tone=("The color is warm and highly saturated.",),
    )
    result = classify_analysis(analysis, _measurements(), _metadata())

    keys = _tag_keys(result)
    assert ("subject", "风光") in keys
    assert ("light", "逆光") in keys
    assert ("light", "黄金时刻") in keys
    assert ("composition", "三分法") in keys
    assert ("composition", "引导线") in keys
    assert ("color", "暖色") in keys
    assert ("color", "高饱和") in keys
    assert ("issue", "曝光不足") in keys


def test_ascii_keywords_do_not_match_inside_longer_words() -> None:
    # "warmth" contains "warm" and "cooler" contains "cool"; a word-boundary
    # match must not treat them as the colour keywords.
    analysis = _analysis(
        overall=(
            "The warmth of the interior and the cooler shadows dominate; "
            "the frame is a landscape."
        ),
    )
    result = classify_analysis(analysis, _measurements(), _metadata())
    keys = _tag_keys(result)
    assert ("subject", "风光") in keys
    assert ("color", "暖色") not in keys
    assert ("color", "冷色") not in keys


# --------------------------------------------------------------------------- #
# Classification: measurement-only evidence
# --------------------------------------------------------------------------- #


def test_measurements_alone_supply_tags() -> None:
    analysis = _analysis()
    measurements = _measurements(
        p50=50,
        p05=40,
        p95=90,
        saturation=40.0,
        flat_share=0.8,
    )
    result = classify_analysis(analysis, measurements, _metadata())

    assert result.tags, "measurement evidence must produce tags on its own"
    assert all(tag.source == "measurement" for tag in result.tags)
    keys = _tag_keys(result)
    assert ("tone", "低调") in keys
    assert ("tone", "低对比") in keys
    assert ("color", "低饱和") in keys
    assert ("issue", "对比不足") in keys


def test_measurements_fill_the_six_measurable_dimensions() -> None:
    analysis = _analysis(
        overall="一张风光照片，采用逆光拍摄",
        technical=("暗部细节不足",),
        composition=("三分法构图",),
        color_tone=("色调偏暖",),
    )
    measurements = _measurements(p50=100, p05=40, p95=120, clip_low=0.05)
    result = classify_analysis(analysis, measurements, _metadata())

    # The histogram can only speak for exposure, colour and contrast; the
    # portrait (framing style of a person) and environment dimensions are
    # reserved for the analysis wording and must not be invented by it.
    measurable = {"subject", "light", "color", "tone", "composition", "issue"}
    dimensions = {tag.dimension for tag in result.tags}
    assert dimensions == measurable
    keys = _tag_keys(result)
    assert ("subject", "风光") in keys
    assert ("light", "逆光") in keys
    assert ("color", "暖色") in keys
    assert ("tone", "低对比") in keys
    assert ("composition", "三分法") in keys
    assert ("issue", "暗部死黑") in keys


def test_measurement_tag_source_is_measurement() -> None:
    analysis = _analysis(technical=("画面整体偏暗",))
    measurements = _measurements(p50=50, p05=40, p95=90)
    result = classify_analysis(analysis, measurements, _metadata())
    shadow_tag = next(tag for tag in result.tags if tag.label == "曝光不足")
    assert shadow_tag.source == "analysis"
    muted_tag = next(tag for tag in result.tags if tag.label == "低调")
    assert muted_tag.source == "measurement"


def test_color_cast_requires_neutral_coverage() -> None:
    # The cast is only reported when there are enough neutral pixels to trust it,
    # exactly like edit_resolver's white-balance correction.
    no_coverage = classify_analysis(
        _analysis(),
        _measurements(neutral_red=1.4, neutral_blue=0.8, neutral_coverage=0.02),
        _metadata(),
    )
    assert ("color", "偏色") not in _tag_keys(no_coverage)

    covered = classify_analysis(
        _analysis(),
        _measurements(neutral_red=1.4, neutral_blue=0.8, neutral_coverage=0.2),
        _metadata(),
    )
    assert ("color", "偏色") in _tag_keys(covered)


# --------------------------------------------------------------------------- #
# Classification: primary category, budgets, vocabulary
# --------------------------------------------------------------------------- #


def test_primary_category_is_the_first_subject_tag() -> None:
    analysis = _analysis(overall="海边的人像照片，背景是风光")
    result = classify_analysis(analysis, _measurements(), _metadata())

    subject_labels = [
        tag.label for tag in result.tags if tag.dimension == "subject"
    ]
    assert subject_labels
    assert result.primary_category == subject_labels[0]


def test_primary_category_falls_back_to_general() -> None:
    result = classify_analysis(_analysis(), _measurements(), _metadata(width=1000, height=1000))
    assert result.primary_category == FALLBACK_CATEGORY
    assert ("subject", "综合") not in _tag_keys(result)


def test_duplicate_labels_are_merged_and_analysis_wins() -> None:
    analysis = _analysis(
        overall="逆光、逆光，三分法、三分法，高光溢出、高光溢出",
    )
    measurements = _measurements(clip_high=0.05)
    result = classify_analysis(analysis, measurements, _metadata())

    keys = _tag_keys(result)
    assert len(keys) == len(result.tags), "(dimension, label) pairs must be unique"
    overflow_tags = [tag for tag in result.tags if tag.label == "高光溢出"]
    assert len(overflow_tags) == 1
    assert overflow_tags[0].source == "analysis"


def test_tag_budgets_are_enforced_per_dimension_and_in_total() -> None:
    analysis = _analysis(
        overall="风光人像建筑街拍静物花卉动物美食旅行夜景室内微距",
        technical=(
            "逆光侧光顺光柔光硬光自然光黄金时刻蓝调时刻弱光影棚顶光窗光",
            "曝光不足过曝高光溢出暗部死黑对比不足噪点失焦动态范围不足锐度不足构图杂乱过饱和白平衡异常",
        ),
        composition=("中心构图三分法对称构图引导线框架构图前景留白纵深低角度高角度",),
        color_tone=("暖色冷色高饱和低饱和单色色彩丰富偏色蓝色调绿色调橙青肤色互补色",),
    )
    measurements = _measurements(
        p50=48,
        p05=2,
        p95=250,
        clip_low=0.2,
        clip_high=0.05,
        saturation=180.0,
        neutral_red=1.3,
        neutral_coverage=0.2,
        flat_share=0.5,
    )
    result = classify_analysis(analysis, measurements, _metadata())

    assert len(result.tags) <= MAX_TAGS_TOTAL
    counts = Counter(tag.dimension for tag in result.tags)
    assert all(count <= 2 for count in counts.values())
    keys = _tag_keys(result)
    assert len(keys) == len(result.tags)


def test_balanced_budget_keeps_portrait_and_environment_dimensions() -> None:
    analysis = _analysis(
        overall="室内环境人像特写，棚拍布光，情绪人像，背景虚化，城市环境中的生活感人像。",
        technical=("逆光侧光，柔光，轻微曝光不足",),
        composition=("半身像构图，竖幅取景，三分法构图",),
        color_tone=("低饱和肤色，暖色调，柔和影调",),
    )
    result = classify_analysis(
        analysis, _measurements(), _metadata(width=600, height=900)
    )

    dimensions = {tag.dimension for tag in result.tags}
    assert {"portrait", "environment"} <= dimensions
    assert len(result.tags) <= MAX_TAGS_TOTAL
    counts = Counter(tag.dimension for tag in result.tags)
    assert all(count <= 2 for count in counts.values())


def test_tags_always_come_from_the_controlled_vocabulary() -> None:
    analysis = _analysis(overall="风光逆光人像暖色高对比三分法高光溢出")
    measurements = _measurements(
        p50=48,
        p05=48,
        p95=48,
        flat_share=1.0,
        saturation=180.0,
    )
    result = classify_analysis(analysis, measurements, _metadata())

    for tag in result.tags:
        assert tag.dimension in TAG_DIMENSIONS
        assert tag.label in TAG_LABELS_BY_DIMENSION[tag.dimension]


def test_vocabulary_covers_all_eight_dimensions() -> None:
    assert set(TAG_LABELS_BY_DIMENSION) == set(TAG_DIMENSIONS)
    for dimension, labels in TAG_LABELS_BY_DIMENSION.items():
        assert len(labels) == 12, dimension
        assert len(set(labels)) == len(labels), dimension
        assert all(isinstance(label, str) and label for label in labels)
    # The knowledge base filters on (dimension, label), so two dimensions may
    # not use the exact same wording for different concepts.
    all_labels = [
        label
        for dimension, labels in TAG_LABELS_BY_DIMENSION.items()
        for label in labels
    ]
    assert len(all_labels) == len(set(all_labels))


def test_composition_orientation_comes_from_metadata() -> None:
    landscape = classify_analysis(
        _analysis(), _measurements(), _metadata(width=4032, height=3024)
    )
    assert _tag_keys(landscape) == {("composition", "横幅")}

    portrait = classify_analysis(
        _analysis(), _measurements(), _metadata(width=3024, height=4032)
    )
    assert _tag_keys(portrait) == {("composition", "竖幅")}

    square = classify_analysis(
        _analysis(), _measurements(), _metadata(width=1000, height=1000)
    )
    assert ("composition", "横幅") not in _tag_keys(square)
    assert ("composition", "竖幅") not in _tag_keys(square)


def test_classification_is_deterministic() -> None:
    analysis = _analysis(
        overall="风光逆光暖色三分法高光溢出",
        technical=("暗部细节不足",),
    )
    measurements = _measurements(
        p50=48,
        p05=48,
        p95=48,
        flat_share=1.0,
        saturation=180.0,
        clip_high=0.05,
    )
    first = classify_analysis(analysis, measurements, _metadata())
    second = classify_analysis(analysis, measurements, _metadata())
    assert first == second


# --------------------------------------------------------------------------- #
# Classification: contradictory labels
# --------------------------------------------------------------------------- #


def test_opposite_label_pairs_are_defined_inside_the_vocabulary() -> None:
    assert set(OPPOSITE_LABEL_PAIRS) <= set(TAG_DIMENSIONS)
    for dimension, pairs in OPPOSITE_LABEL_PAIRS.items():
        vocabulary = TAG_LABELS_BY_DIMENSION[dimension]
        for left, right in pairs:
            assert left != right
            assert left in vocabulary, (dimension, left)
            assert right in vocabulary, (dimension, right)

    # The disagreements the Mock analysis and the histogram actually produce.
    assert ("暖色", "冷色") in OPPOSITE_LABEL_PAIRS["color"]
    assert ("高调", "低调") in OPPOSITE_LABEL_PAIRS["tone"]
    assert ("高对比", "低对比") in OPPOSITE_LABEL_PAIRS["tone"]
    assert ("横幅", "竖幅") in OPPOSITE_LABEL_PAIRS["composition"]
    assert ("曝光不足", "曝光过度") in OPPOSITE_LABEL_PAIRS["issue"]
    assert ("室内环境", "户外环境") in OPPOSITE_LABEL_PAIRS["environment"]


def test_text_only_opposite_tags_cancel_each_other() -> None:
    # The Mock analysis hedges: "偏暖或偏冷" describes options, not the frame.
    analysis = _analysis(color_tone=("白平衡可根据风格偏暖或偏冷处理",))
    result = classify_analysis(analysis, _measurements(), _metadata())

    keys = _tag_keys(result)
    assert ("color", "暖色") not in keys
    assert ("color", "冷色") not in keys


def test_measurement_outranks_text_on_contradictory_tone() -> None:
    analysis = _analysis(color_tone=("画面可以加强对比",))
    # range_90 = 110 - 40 = 70 < 90, so the histogram says low contrast.
    measurements = _measurements(p50=90, p05=40, p95=110)
    result = classify_analysis(analysis, measurements, _metadata())

    tone_tags = [tag for tag in result.tags if tag.dimension == "tone"]
    assert [tag.label for tag in tone_tags] == ["低对比"]
    assert tone_tags[0].source == "measurement"
    assert ("tone", "高对比") not in _tag_keys(result)


def test_same_source_contradictory_tags_are_both_dropped() -> None:
    # Both sides come from the text, so neither is trustworthy: the prose is
    # listing competing directions rather than judging this photograph.
    analysis = _analysis(color_tone=("画面既想要高对比，也想要低对比",))
    result = classify_analysis(analysis, _measurements(), _metadata())

    tone_keys = {key for key in _tag_keys(result) if key[0] == "tone"}
    assert tone_keys == set()


def test_non_contradictory_tags_survive_resolution() -> None:
    analysis = _analysis(overall="逆光下的暖色人像")
    result = classify_analysis(analysis, _measurements(), _metadata())

    keys = _tag_keys(result)
    assert ("light", "逆光") in keys
    assert ("color", "暖色") in keys
    assert ("subject", "人像") in keys
    assert result.primary_category == "人像"


def test_resolution_keeps_primary_and_budgets_consistent() -> None:
    analysis = _analysis(
        overall="风光逆光暖色高对比三分法高光溢出",
        technical=("暗部细节不足", "曝光不足"),
        color_tone=("偏暖或偏冷", "加强对比"),
    )
    measurements = _measurements(
        p50=90,
        p05=40,
        p95=110,
        clip_low=0.05,
        saturation=180.0,
    )
    result = classify_analysis(analysis, measurements, _metadata())

    keys = _tag_keys(result)
    assert result.primary_category == "风光"
    assert ("subject", "风光") in keys
    assert len(keys) == len(result.tags), "(dimension, label) pairs must be unique"
    assert len(result.tags) <= MAX_TAGS_TOTAL
    counts = Counter(tag.dimension for tag in result.tags)
    assert all(count <= 2 for count in counts.values())
    for tag in result.tags:
        assert tag.label in TAG_LABELS_BY_DIMENSION[tag.dimension]
    for dimension, pairs in OPPOSITE_LABEL_PAIRS.items():
        for left, right in pairs:
            assert not {(dimension, left), (dimension, right)} <= keys

    # Resolution is deterministic, so a second run must be identical.
    assert result == classify_analysis(analysis, measurements, _metadata())


# --------------------------------------------------------------------------- #
# Classification: portrait and environment dimensions
# --------------------------------------------------------------------------- #


def _labels(classification, dimension: str) -> list[str]:
    return [tag.label for tag in classification.tags if tag.dimension == dimension]


def test_chinese_portrait_keywords_map_to_portrait_tags() -> None:
    framing = classify_analysis(
        _analysis(overall="这张照片是半身像特写"),
        _measurements(),
        _metadata(),
    )
    assert _labels(framing, "portrait") == ["特写", "半身像"]
    assert all(
        tag.source == "analysis"
        for tag in framing.tags
        if tag.dimension == "portrait"
    )

    mood = classify_analysis(
        _analysis(overall="棚拍布光，人物情绪自然，复古风格"),
        _measurements(),
        _metadata(),
    )
    assert _labels(mood, "portrait") == ["棚拍", "情绪人像"]


def test_chinese_environment_keywords_map_to_environment_tags() -> None:
    indoors = classify_analysis(
        _analysis(overall="照片拍摄于室内环境，背景虚化"),
        _measurements(),
        _metadata(),
    )
    assert _labels(indoors, "environment") == ["室内环境", "虚化背景"]

    outdoors = classify_analysis(
        _analysis(overall="照片拍摄于海边、森林与夜景"),
        _measurements(),
        _metadata(),
    )
    assert _labels(outdoors, "environment") == ["海岸", "森林"]


def test_english_portrait_and_environment_keywords_use_word_boundaries() -> None:
    analysis = _analysis(
        overall=(
            "A close-up portrait shot outdoors in a forest with a blurred "
            "background."
        ),
    )
    result = classify_analysis(analysis, _measurements(), _metadata())
    assert _labels(result, "portrait") == ["特写"]
    assert _labels(result, "environment") == ["户外环境", "森林"]
    assert ("environment", "城市环境") not in _tag_keys(result)

    urbanite = classify_analysis(
        _analysis(overall="An urbanite street scene with a minimal background."),
        _measurements(),
        _metadata(),
    )
    keys = _tag_keys(urbanite)
    # "urbanite" is not the word "urban"; only the street/minimal cues apply.
    assert ("environment", "城市环境") not in keys
    assert ("environment", "街头环境") in keys
    assert ("environment", "极简背景") in keys


def test_indoor_and_outdoor_environment_tags_cancel_each_other() -> None:
    # The analysis hedges about where the photo could be taken. Both labels come
    # from the same source, so neither is trustworthy (OPPOSITE_LABEL_PAIRS).
    analysis = _analysis(overall="拍摄环境既可能是室内，也可能是户外")
    result = classify_analysis(analysis, _measurements(), _metadata())
    assert _labels(result, "environment") == []


def test_new_dimensions_do_not_change_the_primary_category() -> None:
    with_subject = classify_analysis(
        _analysis(overall="室内人像特写"),
        _measurements(),
        _metadata(),
    )
    assert with_subject.primary_category == "人像"
    assert _labels(with_subject, "portrait") == ["特写"]
    assert _labels(with_subject, "environment") == ["室内环境"]

    without_subject = classify_analysis(
        _analysis(overall="照片以特写方式呈现，整体极简背景"),
        _measurements(),
        _metadata(),
    )
    assert without_subject.primary_category == FALLBACK_CATEGORY
    assert _labels(without_subject, "portrait") == ["特写"]
    assert ("environment", "极简背景") in _tag_keys(without_subject)


def test_new_dimension_budgets_are_still_enforced() -> None:
    analysis = _analysis(
        overall=(
            "特写半身像全身像环境人像棚拍生活感人像情绪人像集体照"
            "儿童人像复古人像时尚人像逆光人像"
        ),
        technical=(
            (
                "室内户外城市自然环境海岸森林山野街头极简背景纯色背景"
                "虚化背景夜景"
            ),
        ),
    )
    result = classify_analysis(analysis, _measurements(), _metadata())

    assert len(result.tags) <= MAX_TAGS_TOTAL
    counts = Counter(tag.dimension for tag in result.tags)
    assert all(count <= 2 for count in counts.values())
    keys = _tag_keys(result)
    assert len(keys) == len(result.tags), "(dimension, label) pairs must be unique"
    for tag in result.tags:
        assert tag.label in TAG_LABELS_BY_DIMENSION[tag.dimension]
    for dimension, pairs in OPPOSITE_LABEL_PAIRS.items():
        for left, right in pairs:
            assert not {(dimension, left), (dimension, right)} <= keys


# --------------------------------------------------------------------------- #
# Intensity recommendation
# --------------------------------------------------------------------------- #


def test_intensity_stays_in_range_on_the_five_percent_grid() -> None:
    samples = [
        _measurements(),
        _measurements(p50=48, p05=48, p95=48, flat_share=1.0),
        _measurements(p50=240, p05=0, p95=255, clip_high=0.9),
        _measurements(
            p50=120, p05=5, p95=250, clip_low=0.3, clip_high=0.3
        ),
        _measurements(p50=200, clip_high=0.1),
    ]
    for measurements in samples:
        result = recommend_intensity(measurements, _style())
        assert MIN_RECOMMENDED_INTENSITY <= result.intensity
        assert result.intensity <= MAX_RECOMMENDED_INTENSITY
        steps = (result.intensity - MIN_RECOMMENDED_INTENSITY) / RECOMMENDED_INTENSITY_STEP
        assert steps == pytest.approx(round(steps), abs=1e-6)
        assert result.intensity == pytest.approx(round(result.intensity, 2))


def test_high_key_and_highlight_clipping_lower_the_intensity() -> None:
    neutral = recommend_intensity(_measurements(), _style())
    high_key = recommend_intensity(
        _measurements(p50=190, p05=20, p95=220), _style()
    )
    clipped = recommend_intensity(
        _measurements(p50=190, p05=20, p95=220, clip_high=0.05), _style()
    )

    assert neutral.intensity == pytest.approx(
        BASELINE_RECOMMENDED_INTENSITY - 0.05
    )
    assert high_key.intensity < neutral.intensity
    assert clipped.intensity < high_key.intensity
    assert "偏亮" in high_key.reason
    assert "溢出" in clipped.reason


def test_low_key_flat_frame_raises_the_intensity() -> None:
    baseline = recommend_intensity(_measurements(), _style())
    result = recommend_intensity(
        _measurements(p50=48, p05=48, p95=48, flat_share=1.0), _style()
    )
    assert result.intensity > baseline.intensity
    assert "偏暗" in result.reason
    assert "偏平" in result.reason


def test_stronger_style_parameters_lower_the_intensity() -> None:
    measurements = _measurements()
    neutral = recommend_intensity(measurements, _style())
    strong = recommend_intensity(
        measurements,
        _style(
            parameters=EditingParametersV1(exposure=1.2, contrast=80, saturation=70)
        ),
    )
    assert strong.intensity < neutral.intensity
    assert "风格参数" in strong.reason


def test_intensity_clamps_to_the_supported_range() -> None:
    boosted = recommend_intensity(
        _measurements(p50=48, p05=48, p95=48, flat_share=1.0), _style()
    )
    assert boosted.intensity == MAX_RECOMMENDED_INTENSITY

    strong_style = _style(
        parameters=EditingParametersV1(exposure=1.2, contrast=80, saturation=70)
    )
    damped = recommend_intensity(
        _measurements(
            p50=180, p05=3, p95=250, clip_low=0.1, clip_high=0.2
        ),
        strong_style,
    )
    assert damped.intensity == MIN_RECOMMENDED_INTENSITY


def test_reason_is_one_chinese_sentence_with_measurement_evidence() -> None:
    samples = [
        (_measurements(), _style(), "曝光"),
        (_measurements(p50=190, clip_high=0.05), _style(), "溢出"),
        (_measurements(p50=48, p05=48, p95=48, flat_share=1.0), _style(), "偏暗"),
    ]
    for measurements, style, expected in samples:
        result = recommend_intensity(measurements, style)
        assert result.reason.strip()
        assert result.reason.endswith("。")
        assert re.search(r"[\u4e00-\u9fff]", result.reason)
        assert re.search(r"\d", result.reason), result.reason
        assert expected in result.reason


def test_intensity_is_deterministic() -> None:
    measurements = _measurements(p50=100, p05=40, p95=120, clip_high=0.02)
    style = _style(parameters=EditingParametersV1(contrast=60))
    first = recommend_intensity(measurements, style)
    second = recommend_intensity(measurements, style)
    assert first == second
