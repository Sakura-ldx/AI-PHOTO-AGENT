"""Phase 2 修图学习总结 / 知识库 API。

路由只负责 HTTP 参数与响应组装；筛选、搜索、聚合与分页由
``app.services.knowledge_query`` 的纯函数完成，便于独立测试和复用。

- 列表：筛选后的总结卡片 + 全量 facets（供前端渲染筛选项）
- 详情：单张照片的完整学习总结
- 重建：老会话回填，或在预览资产失效后重新生成
- 总览：知识库统计
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_services
from app.container import ServiceContainer
from app.knowledge_schemas import (
    KnowledgeOverviewResponse,
    KnowledgeSummaryListResponse,
    PhotoLearningSummary,
)
from app.schemas import ErrorResponse
from app.services.knowledge_query import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    KnowledgeQuery,
    build_overview,
    build_page,
)

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])
Services = Annotated[ServiceContainer, Depends(get_services)]
ERROR_RESPONSE = {"model": ErrorResponse}


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
    style: Annotated[str | None, Query(max_length=200)] = None,
    q: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> KnowledgeSummaryListResponse:
    query = KnowledgeQuery.from_params(
        dimension=dimension,
        tag=tag,
        category=category,
        style=style,
        text=q,
        limit=limit,
        offset=offset,
    )
    page = build_page(services.storage.list_knowledge_summaries(), query)
    return KnowledgeSummaryListResponse(
        total=page.total,
        limit=query.limit,
        offset=query.offset,
        items=page.items,
        facets=page.facets,
        primary_categories=page.primary_categories,
        style_tags=page.style_tags,
    )


@router.get(
    "/overview",
    response_model=KnowledgeOverviewResponse,
    responses={500: ERROR_RESPONSE},
)
def knowledge_overview(services: Services) -> KnowledgeOverviewResponse:
    return build_overview(services.storage.list_knowledge_summaries())


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
