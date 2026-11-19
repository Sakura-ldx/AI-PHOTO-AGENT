"""Phase 2 修图学习总结 / 知识库 API。

- 列表：筛选后的总结卡片 + 全量 facets（供前端渲染筛选项）
- 详情：单张照片的完整学习总结
- 重建：老会话回填，或在预览资产失效后重新生成
- 总览：知识库统计
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_services
from app.container import ServiceContainer
from app.errors import InvalidRequestError
from app.knowledge_schemas import (
    KnowledgeCategoryCount,
    KnowledgeFacet,
    KnowledgeOverviewResponse,
    KnowledgeSummaryListItem,
    KnowledgeSummaryListResponse,
    KnowledgeTagCount,
    PhotoLearningSummary,
    TAG_DIMENSION_LABELS,
    TAG_DIMENSIONS,
)
from app.schemas import ErrorResponse


router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
Services = Annotated[ServiceContainer, Depends(get_services)]
ERROR_RESPONSE = {"model": ErrorResponse}


def _normalize_filter(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


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
        facets.append(
            KnowledgeFacet(
                dimension=dimension,  # type: ignore[arg-type]
                label=TAG_DIMENSION_LABELS[dimension],
                tags=[
                    KnowledgeTagCount(
                        dimension=dimension,  # type: ignore[arg-type]
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


def _matches_filters(
    summary: PhotoLearningSummary,
    *,
    dimension: str | None,
    tag: str | None,
    category: str | None,
) -> bool:
    if category is not None and summary.primary_category != category:
        return False
    if tag is None:
        return True
    return any(
        item.label == tag and (dimension is None or item.dimension == dimension)
        for item in summary.tags
    )


@router.get(
    "/summaries",
    response_model=KnowledgeSummaryListResponse,
    responses={400: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def list_learning_summaries(
    services: Services,
    dimension: Annotated[str | None, Query(max_length=32)] = None,
    tag: Annotated[str | None, Query(max_length=24)] = None,
    category: Annotated[str | None, Query(max_length=24)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KnowledgeSummaryListResponse:
    dimension = _normalize_filter(dimension)
    tag = _normalize_filter(tag)
    category = _normalize_filter(category)
    if dimension is not None and dimension not in TAG_DIMENSIONS:
        raise InvalidRequestError("Unsupported knowledge tag dimension.")
    if dimension is not None and tag is None:
        raise InvalidRequestError(
            "Filtering by dimension requires a tag label as well."
        )

    summaries = services.storage.list_knowledge_summaries()
    facets = _build_facets(summaries)
    primary_categories = _build_category_counts(summaries)
    filtered = [
        summary
        for summary in summaries
        if _matches_filters(
            summary,
            dimension=dimension,
            tag=tag,
            category=category,
        )
    ]
    filtered.sort(key=lambda summary: summary.generated_at, reverse=True)
    page = filtered[offset : offset + limit]
    return KnowledgeSummaryListResponse(
        total=len(filtered),
        limit=limit,
        offset=offset,
        items=[KnowledgeSummaryListItem.from_summary(summary) for summary in page],
        facets=facets,
        primary_categories=primary_categories,
    )


@router.get(
    "/overview",
    response_model=KnowledgeOverviewResponse,
    responses={500: ERROR_RESPONSE},
)
def knowledge_overview(services: Services) -> KnowledgeOverviewResponse:
    summaries = services.storage.list_knowledge_summaries()
    facets = _build_facets(summaries)
    primary_categories = _build_category_counts(summaries)
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
    )


@router.get(
    "/summaries/{photo_id}",
    response_model=PhotoLearningSummary,
    responses={404: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def get_learning_summary(photo_id: str, services: Services) -> PhotoLearningSummary:
    return services.photos.get_learning_summary(photo_id)


@router.post(
    "/summaries/{photo_id}/rebuild",
    response_model=PhotoLearningSummary,
    responses={
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        500: ERROR_RESPONSE,
    },
)
def rebuild_learning_summary(photo_id: str, services: Services) -> PhotoLearningSummary:
    """Regenerate a summary for an already analyzed photo (legacy backfill)."""

    return services.photos.build_learning_summary(photo_id)
