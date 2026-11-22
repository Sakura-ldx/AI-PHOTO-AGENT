"""知识库查询的纯应用服务。

API 层只负责解析 HTTP 参数与组装响应；筛选、搜索、facet 聚合和分页都在
这里完成。这个模块不依赖 FastAPI 或文件系统，因此可以直接用普通 Python
对象做单元测试，也避免把领域规则散落在路由函数里。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import cast

from app.errors import InvalidRequestError
from app.knowledge_schemas import (
    TAG_DIMENSION_LABELS,
    TAG_DIMENSIONS,
    KnowledgeCategoryCount,
    KnowledgeDimension,
    KnowledgeFacet,
    KnowledgeOverviewResponse,
    KnowledgeStyleCount,
    KnowledgeSummaryListItem,
    KnowledgeTagCount,
    PhotoLearningSummary,
)

# 近似搜索相似度阈值：0.55 左右对中文短标签足够宽松，又不会把无关结果拖进来。
SEARCH_SIMILARITY_THRESHOLD = 0.55
# 子串命中的最短长度：单字片段（如「人」）会把无关查询也拖进来，只靠相似度兜底。
SEARCH_MIN_SUBSTRING_CHARS = 2
# 概述文本太长时只截取前一段参与搜索，避免逐字比较造成不必要的开销。
SEARCH_SUMMARY_CHARS = 400
_SEARCH_SPLIT_PATTERN = re.compile(r"[\s,，。;；、/|·:：()（）\[\]【】]+")
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _contains(needle: str, haystack: str) -> bool:
    if not needle or not haystack:
        return False
    if needle == haystack:
        return True
    if len(needle) < SEARCH_MIN_SUBSTRING_CHARS:
        return False
    return needle in haystack


def normalize_filter(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_search(value: str) -> str:
    return " ".join(value.casefold().split())


def search_tokens(query: str | None) -> tuple[str, ...]:
    if query is None:
        return ()
    normalized = _normalize_search(query)
    if not normalized:
        return ()
    return tuple(part for part in _SEARCH_SPLIT_PATTERN.split(normalized) if part)


def _style_names(summary: PhotoLearningSummary) -> tuple[str, ...]:
    seen: set[str] = set()
    names: list[str] = []
    for direction in summary.directions:
        if direction.style_name in seen:
            continue
        seen.add(direction.style_name)
        names.append(direction.style_name)
    return tuple(names)


def _search_candidates(summary: PhotoLearningSummary) -> tuple[str, ...]:
    """参与搜索的字段：标签、主分类、风格名、文件名、概述文本。"""

    candidates: list[str] = [summary.primary_category]
    candidates.extend(tag.label for tag in summary.tags)
    candidates.extend(_style_names(summary))
    filename = summary.image.original_filename
    if filename:
        candidates.append(filename)
    candidates.append(summary.overview[:SEARCH_SUMMARY_CHARS])
    return tuple(item for item in candidates if item)


def _candidate_parts(candidate: str) -> tuple[str, ...]:
    parts = [part for part in _SEARCH_SPLIT_PATTERN.split(candidate) if part]
    return tuple(parts) or (candidate,)


def _token_matches(token: str, candidate: str) -> bool:
    if not token or not candidate:
        return False
    if _contains(token, candidate) or _contains(candidate, token):
        return True
    for part in _candidate_parts(candidate):
        if _contains(token, part) or _contains(part, token):
            return True
        if SequenceMatcher(None, token, part).ratio() >= SEARCH_SIMILARITY_THRESHOLD:
            return True
    return (
        SequenceMatcher(None, token, candidate).ratio()
        >= SEARCH_SIMILARITY_THRESHOLD
    )


def _matches_query(summary: PhotoLearningSummary, tokens: Sequence[str]) -> bool:
    if not tokens:
        return True
    candidates = tuple(
        _normalize_search(candidate) for candidate in _search_candidates(summary)
    )
    return all(
        any(_token_matches(token, candidate) for candidate in candidates)
        for token in tokens
    )


def _build_facets(
    summaries: Sequence[PhotoLearningSummary],
) -> list[KnowledgeFacet]:
    """Count tags per dimension over the whole knowledge base."""

    counts: dict[str, dict[str, int]] = {dimension: {} for dimension in TAG_DIMENSIONS}
    for summary in summaries:
        seen: set[tuple[str, str]] = set()
        for tag in summary.tags:
            key = (tag.dimension, tag.label)
            if key in seen:
                continue
            seen.add(key)
            labels = counts.setdefault(tag.dimension, {})
            labels[tag.label] = labels.get(tag.label, 0) + 1
    facets: list[KnowledgeFacet] = []
    for dimension in TAG_DIMENSIONS:
        dimension_counts = counts.get(dimension) or {}
        if not dimension_counts:
            continue
        ordered = sorted(
            dimension_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )
        typed_dimension = cast(KnowledgeDimension, dimension)
        facets.append(
            KnowledgeFacet(
                dimension=typed_dimension,
                label=TAG_DIMENSION_LABELS[dimension],
                tags=[
                    KnowledgeTagCount(
                        dimension=typed_dimension,
                        label=label,
                        count=count,
                    )
                    for label, count in ordered
                ],
            )
        )
    return facets


def _build_category_counts(
    summaries: Sequence[PhotoLearningSummary],
) -> list[KnowledgeCategoryCount]:
    counts: dict[str, int] = {}
    for summary in summaries:
        counts[summary.primary_category] = counts.get(summary.primary_category, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        KnowledgeCategoryCount(category=category, count=count)
        for category, count in ordered
    ]


def _build_style_counts(
    summaries: Sequence[PhotoLearningSummary],
) -> list[KnowledgeStyleCount]:
    """按修图风格统计照片数，供知识库「图片风格」目录使用。"""

    counts: dict[str, int] = {}
    for summary in summaries:
        for style_name in _style_names(summary):
            counts[style_name] = counts.get(style_name, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        KnowledgeStyleCount(name=name, count=count) for name, count in ordered
    ]


def _matches_filters(
    summary: PhotoLearningSummary,
    query: KnowledgeQuery,
    tokens: Sequence[str],
) -> bool:
    if query.category is not None and summary.primary_category != query.category:
        return False
    if query.style is not None:
        normalized_style = _normalize_search(query.style)
        if not any(
            _normalize_search(name) == normalized_style
            for name in _style_names(summary)
        ):
            return False
    if query.tag is not None and not any(
        item.label == query.tag
        and (query.dimension is None or item.dimension == query.dimension)
        for item in summary.tags
    ):
        return False
    return _matches_query(summary, tokens)


@dataclass(frozen=True, slots=True)
class KnowledgeQuery:
    dimension: KnowledgeDimension | None = None
    tag: str | None = None
    category: str | None = None
    style: str | None = None
    text: str | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0

    @classmethod
    def from_params(
        cls,
        *,
        dimension: str | None = None,
        tag: str | None = None,
        category: str | None = None,
        style: str | None = None,
        text: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> KnowledgeQuery:
        normalized_dimension = normalize_filter(dimension)
        normalized_tag = normalize_filter(tag)
        normalized_category = normalize_filter(category)
        normalized_style = normalize_filter(style)
        normalized_text = normalize_filter(text)
        if (
            normalized_dimension is not None
            and normalized_dimension not in TAG_DIMENSIONS
        ):
            raise InvalidRequestError("Unsupported knowledge tag dimension.")
        if normalized_dimension is not None and normalized_tag is None:
            raise InvalidRequestError(
                "Filtering by dimension requires a tag label as well."
            )
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        return cls(
            dimension=cast(KnowledgeDimension | None, normalized_dimension),
            tag=normalized_tag,
            category=normalized_category,
            style=normalized_style,
            text=normalized_text,
            limit=limit,
            offset=offset,
        )


@dataclass(frozen=True, slots=True)
class KnowledgePage:
    total: int
    items: list[KnowledgeSummaryListItem]
    facets: list[KnowledgeFacet]
    primary_categories: list[KnowledgeCategoryCount]
    style_tags: list[KnowledgeStyleCount]


def build_page(
    summaries: Sequence[PhotoLearningSummary],
    query: KnowledgeQuery,
) -> KnowledgePage:
    """Filter, sort and paginate summaries, keeping facets over the full set."""

    facets = _build_facets(summaries)
    primary_categories = _build_category_counts(summaries)
    style_tags = _build_style_counts(summaries)
    tokens = search_tokens(query.text)
    filtered = [
        summary
        for summary in summaries
        if _matches_filters(summary, query, tokens)
    ]
    filtered.sort(key=lambda summary: summary.generated_at, reverse=True)
    page = filtered[query.offset : query.offset + query.limit]
    return KnowledgePage(
        total=len(filtered),
        items=[KnowledgeSummaryListItem.from_summary(summary) for summary in page],
        facets=facets,
        primary_categories=primary_categories,
        style_tags=style_tags,
    )


def build_overview(
    summaries: Sequence[PhotoLearningSummary],
) -> KnowledgeOverviewResponse:
    facets = _build_facets(summaries)
    primary_categories = _build_category_counts(summaries)
    style_tags = _build_style_counts(summaries)
    intensities = [
        direction.recommended_intensity
        for summary in summaries
        for direction in summary.directions
    ]
    latest_generated_at = max(
        (summary.generated_at for summary in summaries),
        default=None,
    )
    return KnowledgeOverviewResponse(
        total_summaries=len(summaries),
        covered_categories=len(primary_categories),
        average_intensity=(
            sum(intensities) / len(intensities) if intensities else None
        ),
        latest_generated_at=latest_generated_at,
        facets=facets,
        primary_categories=primary_categories,
        style_tags=style_tags,
    )
