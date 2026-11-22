from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from app.knowledge_schemas import LocalUrl, SingleLineText
from app.schemas import UTCDateTime


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "//evil.example/preview.jpg",
        "/\\evil.example/preview.jpg",
        "https://evil.example/preview.jpg",
        "/preview\r\ninjected.jpg",
    ],
)
def test_local_url_rejects_non_same_origin_or_multiline_values(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(LocalUrl).validate_python(value)


def test_local_url_accepts_generated_asset_paths() -> None:
    adapter = TypeAdapter(LocalUrl)

    assert adapter.validate_python("/api/photos/" + "a" * 32 + "/display")
    assert adapter.validate_python("/generated/summaries/" + "a" * 32 + "/preview.jpg")


def test_original_filename_rejects_control_characters() -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(SingleLineText).validate_python("photo\nname.jpg")


def test_utc_datetime_normalizes_naive_and_aware_values() -> None:
    adapter = TypeAdapter(UTCDateTime)
    naive = adapter.validate_python(datetime(2026, 1, 2, 3, 4, 5))
    aware = adapter.validate_python(
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    )

    assert naive.tzinfo is UTC
    assert naive == aware
