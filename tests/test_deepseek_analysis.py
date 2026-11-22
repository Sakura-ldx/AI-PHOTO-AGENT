from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from app.adapters.analysis import MockAnalysisProvider
from app.adapters.deepseek_analysis import (
    MAX_OUTPUT_TOKENS,
    DeepSeekAnalysisProvider,
    _response_content,
    _style_diversity_issue,
    _style_quality_issue,
)
from app.adapters.vision_models import StructuredPhotoAnalysis
from app.errors import ProviderOutputError, ProviderServiceError, ProviderTimeoutError
from app.schemas import ImageAnalysis, ImageMetadata


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(
        self,
        *,
        responses: list[FakeResponse | Exception] | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if not self._responses:
            raise AssertionError("unexpected DeepSeek request")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_metadata() -> ImageMetadata:
    return ImageMetadata(
        width=320,
        height=200,
        format="PNG",
        color_mode="RGB",
        file_size=100,
        content_type="image/png",
    )


def make_image(path) -> None:
    Image.new("RGB", (320, 200), color=(35, 70, 110)).save(path, format="PNG")


def to_structured(analysis: ImageAnalysis) -> StructuredPhotoAnalysis:
    payload = analysis.model_dump(mode="python", round_trip=True)
    for style in payload["recommended_styles"]:
        hsl = style["parameters"]["hsl"]
        style["parameters"]["hsl"] = {
            channel: hsl.get(channel, {})
            for channel in (
                "red",
                "orange",
                "yellow",
                "green",
                "aqua",
                "blue",
                "purple",
                "magenta",
            )
        }
    return StructuredPhotoAnalysis.model_validate(payload)


def make_response(structured: StructuredPhotoAnalysis) -> FakeResponse:
    content = json.dumps(
        structured.model_dump(mode="json"),
        ensure_ascii=False,
    )
    return FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 200},
        },
    )


def make_provider(client: FakeClient, *, max_retries: int = 0) -> DeepSeekAnalysisProvider:
    return DeepSeekAnalysisProvider(
        api_key="sk-test",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        timeout_seconds=120.0,
        max_retries=max_retries,
        client=client,
    )


def test_deepseek_provider_sends_image_and_returns_dynamic_analysis(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    structured = to_structured(expected)
    client = FakeClient(responses=[make_response(structured)])

    actual = make_provider(client).analyze(image_path, metadata)

    assert actual == expected
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "https://api.deepseek.com/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    payload = call["json"]
    assert payload["model"] == "deepseek-flash"
    assert payload["max_tokens"] == MAX_OUTPUT_TOKENS
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][0]["role"] == "system"
    prompt = payload["messages"][1]["content"][0]["text"]
    assert "JSON Schema" in prompt
    assert "recommended_styles" in prompt
    image_url = payload["messages"][1]["content"][1]["image_url"]["url"]
    prefix, encoded = image_url.split(",", maxsplit=1)
    assert prefix == "data:image/jpeg;base64"
    assert encoded
    assert actual.recommended_styles[0].id == expected.recommended_styles[0].id


def test_deepseek_provider_retries_transient_status(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    client = FakeClient(
        responses=[
            FakeResponse(503, {"error": {"message": "busy"}}),
            make_response(to_structured(expected)),
        ]
    )

    actual = make_provider(client, max_retries=1).analyze(image_path, metadata)

    assert actual == expected
    assert len(client.calls) == 2


def test_deepseek_provider_rejects_invalid_json_after_one_retry(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    invalid = FakeResponse(
        200,
        {"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}]},
    )
    client = FakeClient(responses=[invalid, invalid])

    with pytest.raises(ProviderOutputError):
        make_provider(client).analyze(image_path, make_metadata())
    assert len(client.calls) == 2
    assert "上一次输出未通过" in client.calls[1]["json"]["messages"][1]["content"][0]["text"]


def test_deepseek_provider_retries_invalid_structured_output_once(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    invalid = FakeResponse(
        200,
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]},
    )
    client = FakeClient(responses=[invalid, make_response(to_structured(expected))])

    actual = make_provider(client).analyze(image_path, metadata)

    assert actual == expected
    assert len(client.calls) == 2


def test_deepseek_provider_reports_http_failure(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    client = FakeClient(responses=[FakeResponse(401, {"error": {"message": "bad key"}})])

    with pytest.raises(ProviderServiceError):
        make_provider(client).analyze(image_path, make_metadata())


def test_deepseek_provider_reports_timeout(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    client = FakeClient(responses=[httpx.TimeoutException("timed out")])

    with pytest.raises(ProviderTimeoutError):
        make_provider(client).analyze(image_path, make_metadata())


def test_deepseek_provider_strips_markdown_fence(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    content = json.dumps(to_structured(expected).model_dump(mode="json"), ensure_ascii=False)
    client = FakeClient(
        responses=[
            FakeResponse(
                200,
                {
                    "choices": [
                        {
                            "message": {"content": f"```json\n{content}\n```"},
                            "finish_reason": "stop",
                        }
                    ]
                },
            )
        ]
    )

    assert make_provider(client).analyze(image_path, metadata) == expected


def test_deepseek_budget_leaves_room_for_reasoning_tokens() -> None:
    # 12000 was hit in practice because reasoning tokens share the budget;
    # a truncated reply is invalid JSON, not a slightly smaller analysis.
    assert MAX_OUTPUT_TOKENS >= 20_000


def test_deepseek_provider_retries_a_truncated_reply_with_a_compact_prompt(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    truncated = FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {"content": '{"overall_summary": "被截断的中文'},
                    "finish_reason": "length",
                }
            ],
            "usage": {"completion_tokens": MAX_OUTPUT_TOKENS},
        },
    )
    client = FakeClient(responses=[truncated, make_response(to_structured(expected))])

    actual = make_provider(client).analyze(image_path, metadata)

    assert actual == expected
    assert len(client.calls) == 2
    retry_prompt = client.calls[1]["json"]["messages"][1]["content"][0]["text"]
    assert "上一次输出被输出长度限制截断" in retry_prompt
    assert "上一次输出未通过" in retry_prompt


def test_deepseek_provider_reports_truncation_after_the_compact_retry(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    truncated = FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {"content": '{"overall_summary": "仍然被截断'},
                    "finish_reason": "length",
                }
            ]
        },
    )
    client = FakeClient(responses=[truncated, truncated])

    with pytest.raises(ProviderOutputError):
        make_provider(client).analyze(image_path, make_metadata())
    assert len(client.calls) == 2
    assert client.calls[1]["json"]["max_tokens"] == MAX_OUTPUT_TOKENS


def _with_temperatures(analysis: ImageAnalysis, values: list[int]) -> ImageAnalysis:
    clone = analysis.model_copy(deep=True)
    for style, value in zip(clone.recommended_styles, values, strict=True):
        style.parameters.temperature = value
    return clone


def test_style_diversity_check_accepts_the_mock_recipe() -> None:
    analysis = MockAnalysisProvider().analyze(
        Path("unused.png"),
        make_metadata(),
    )

    assert _style_diversity_issue(analysis) is None


def test_style_diversity_check_detects_warm_cool_neutral_triad() -> None:
    analysis = MockAnalysisProvider().analyze(
        Path("unused.png"),
        make_metadata(),
    )
    triad = _with_temperatures(analysis, [18, 0, -18])

    assert "三分法" in (_style_diversity_issue(triad) or "")


def test_deepseek_provider_retries_a_repetitive_triad_once(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    triad = _with_temperatures(expected, [18, 0, -18])
    client = FakeClient(
        responses=[
            make_response(to_structured(triad)),
            make_response(to_structured(expected)),
        ]
    )

    actual = make_provider(client).analyze(image_path, metadata)

    assert actual == expected
    assert len(client.calls) == 2
    assert client.calls[0]["json"]["temperature"] == 0.2
    assert client.calls[1]["json"]["temperature"] == 0.55
    retry_prompt = client.calls[1]["json"]["messages"][1]["content"][0]["text"]
    assert "未通过质量检查" in retry_prompt
    assert "三分法" in retry_prompt


def test_deepseek_provider_accepts_the_second_triad_instead_of_looping(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_image(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    triad = _with_temperatures(expected, [18, 0, -18])
    client = FakeClient(
        responses=[
            make_response(to_structured(triad)),
            make_response(to_structured(triad)),
        ]
    )

    actual = make_provider(client).analyze(image_path, metadata)

    assert actual == triad


def test_style_quality_gate_rejects_a_generic_monochrome_reason_on_a_colorful_photo(
    tmp_path,
) -> None:
    from app.services.measurements import measure_file

    image_path = tmp_path / "colorful.png"
    Image.new("RGB", (320, 200), color=(220, 30, 40)).save(image_path, format="PNG")
    measurements = measure_file(image_path)
    analysis = MockAnalysisProvider(include_monochrome=True).analyze(
        image_path, make_metadata()
    )
    analysis.recommended_styles[-1].best_fit_reason = "更好看，更高级。"

    assert measurements.saturation_mean >= 120
    assert _style_quality_issue(analysis, measurements) is not None

    analysis.recommended_styles[-1].best_fit_reason = (
        "去色后线条和明暗层次更突出，适合这张几何结构强的画面。"
    )
    assert _style_quality_issue(analysis, measurements) is None


def test_response_content_ignores_non_string_text_parts() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "text", "text": None},
                        {"type": "image", "text": "不是文本"},
                        {"type": "text", "text": 3},
                        {"type": "text", "text": "第二段"},
                    ]
                }
            }
        ]
    }

    assert _response_content(payload) == "第一段第二段"
