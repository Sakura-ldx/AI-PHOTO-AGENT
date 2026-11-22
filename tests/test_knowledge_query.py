from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.errors import InvalidRequestError
from app.knowledge_schemas import KnowledgeImage, KnowledgeTag
from app.services.knowledge_query import (
    KnowledgeQuery,
    build_overview,
    build_page,
    search_tokens,
)


def make_summary(
    *,
    photo_id: str,
    category: str,
    style_name: str,
    filename: str,
    tags: list[KnowledgeTag],
    generated_at: datetime,
) -> SimpleNamespace:
    return SimpleNamespace(
        photo_id=photo_id,
        primary_category=category,
        tags=tags,
        image=KnowledgeImage(
            display_url=f"/api/photos/{photo_id}/display",
            width=1200,
            height=800,
            format="JPEG",
            color_mode="RGB",
            file_size=1000,
            original_filename=filename,
        ),
        analysis=SimpleNamespace(overall_summary=f"{category} 的总结"),
        overview=f"{category}，{style_name} 的修图方向。",
        directions=[
            SimpleNamespace(
                style_name=style_name,
                recommended_intensity=0.7,
                preview_url=None,
            )
        ],
        generated_at=generated_at,
    )


def test_query_parameters_are_normalized_and_validated() -> None:
    query = KnowledgeQuery.from_params(
        dimension=" subject ",
        tag=" 人像 ",
        style=" 胶片 ",
        text=" 逆光 ",
        limit=10,
        offset=2,
    )

    assert query.dimension == "subject"
    assert query.tag == "人像"
    assert query.style == "胶片"
    assert query.text == "逆光"

    with pytest.raises(InvalidRequestError):
        KnowledgeQuery.from_params(dimension="unknown", tag="人像")
    with pytest.raises(InvalidRequestError):
        KnowledgeQuery.from_params(dimension="subject")
    with pytest.raises(ValueError):
        KnowledgeQuery.from_params(limit=0)
    with pytest.raises(ValueError):
        KnowledgeQuery.from_params(offset=-1)


def test_search_tokens_split_chinese_punctuation_for_and_search() -> None:
    assert search_tokens("人像，逆光") == ("人像", "逆光")
    assert search_tokens(" 胶片 / 夜景；低饱和 ") == ("胶片", "夜景", "低饱和")

    summary = make_summary(
        photo_id="a" * 32,
        category="人像",
        style_name="逆光胶片",
        filename="portrait.jpg",
        tags=[KnowledgeTag(dimension="subject", label="人像")],
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    page = build_page([summary], KnowledgeQuery.from_params(text="人像，逆光"))

    assert page.total == 1
    assert page.items[0].photo_id == "a" * 32


def test_build_page_filters_but_keeps_facets_over_the_whole_library() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    first = make_summary(
        photo_id="a" * 32,
        category="人像",
        style_name="柔和胶片",
        filename="portrait.jpg",
        tags=[KnowledgeTag(dimension="subject", label="人像")],
        generated_at=now,
    )
    second = make_summary(
        photo_id="b" * 32,
        category="风光",
        style_name="高反差黑白",
        filename="landscape.jpg",
        tags=[KnowledgeTag(dimension="subject", label="风光")],
        generated_at=now + timedelta(hours=1),
    )

    page = build_page(
        [first, second],
        KnowledgeQuery.from_params(category="人像"),
    )

    assert page.total == 1
    assert [item.photo_id for item in page.items] == ["a" * 32]
    assert {facet.dimension for facet in page.facets} == {"subject"}
    assert {tag.label for facet in page.facets for tag in facet.tags} == {
        "人像",
        "风光",
    }
    assert {entry.category for entry in page.primary_categories} == {"人像", "风光"}
    assert {entry.name for entry in page.style_tags} == {"柔和胶片", "高反差黑白"}


def test_build_page_sorts_and_paginates() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    summaries = [
        make_summary(
            photo_id=f"{index:032x}",
            category="人像",
            style_name="柔和胶片",
            filename=f"photo-{index}.jpg",
            tags=[],
            generated_at=now + timedelta(minutes=index),
        )
        for index in range(3)
    ]

    page = build_page(summaries, KnowledgeQuery.from_params(limit=1, offset=1))

    assert page.total == 3
    assert [item.photo_id for item in page.items] == [f"{1:032x}"]


def test_build_overview_reports_latest_and_average() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    summaries = [
        make_summary(
            photo_id="a" * 32,
            category="人像",
            style_name="柔和胶片",
            filename="portrait.jpg",
            tags=[],
            generated_at=now,
        ),
        make_summary(
            photo_id="b" * 32,
            category="风光",
            style_name="高反差黑白",
            filename="landscape.jpg",
            tags=[],
            generated_at=now + timedelta(hours=1),
        ),
    ]

    overview = build_overview(summaries)

    assert overview.total_summaries == 2
    assert overview.covered_categories == 2
    assert overview.average_intensity == pytest.approx(0.7)
    assert overview.latest_generated_at == now + timedelta(hours=1)
