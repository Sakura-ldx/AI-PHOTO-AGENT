from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError
from PIL import Image

from app.adapters.learning import MockLearningAnalysisProvider
from app.adapters.openai_learning import (
    MAX_LEARNING_OUTPUT_TOKENS,
    OpenAILearningAnalysisProvider,
)
from app.adapters.vision_models import StructuredLearningAnalysis
from app.errors import ProviderOutputError, ProviderServiceError, ProviderTimeoutError
from app.schemas import ImageMetadata, LearningReference, LearningSession


class FakeResponses:
    def __init__(self, *, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _make_session(tmp_path) -> LearningSession:
    references = []
    for index, (size, color) in enumerate(
        [((120, 80), (210, 70, 50)), ((80, 120), (30, 90, 200))]
    ):
        path = tmp_path / f"reference-{index}.png"
        Image.new("RGBA", size, color=(*color, 180)).save(path)
        references.append(
            LearningReference(
                id=("b" if index == 0 else "c") * 32,
                image_file=path,
                image_url=f"/private/{path.name}",
                metadata=ImageMetadata(
                    width=size[0],
                    height=size[1],
                    format="PNG",
                    color_mode="RGBA",
                    file_size=path.stat().st_size,
                    content_type="image/png",
                ),
            )
        )
    return LearningSession(id="a" * 32, topic="冷暖层次", references=references)


def _structured_result(session: LearningSession) -> StructuredLearningAnalysis:
    result = MockLearningAnalysisProvider().analyze(session)
    return StructuredLearningAnalysis(
        reference_analyses=[item.analysis for item in result.references],
        summary=result.summary,
    )


def _provider(responses: FakeResponses) -> OpenAILearningAnalysisProvider:
    return OpenAILearningAnalysisProvider(
        api_key="test-key",
        model="gpt-5.6-terra",
        timeout_seconds=20.0,
        max_retries=1,
        client=SimpleNamespace(responses=responses),
    )


def test_openai_learning_provider_sends_ordered_multi_image_request(tmp_path) -> None:
    session = _make_session(tmp_path)
    structured = _structured_result(session)
    responses = FakeResponses(
        result=SimpleNamespace(
            output_parsed=structured,
            usage=SimpleNamespace(input_tokens=200, output_tokens=300),
            _request_id="req_learning",
        )
    )

    result = _provider(responses).analyze(session)

    assert result == MockLearningAnalysisProvider().analyze(session)
    request = responses.calls[0]
    assert request["model"] == "gpt-5.6-terra"
    assert request["text_format"] is StructuredLearningAnalysis
    assert request["max_output_tokens"] == MAX_LEARNING_OUTPUT_TOKENS
    assert request["store"] is False
    user_content = request["input"][1]["content"]
    assert [item["type"] for item in user_content] == [
        "input_text",
        "input_image",
        "input_image",
    ]
    assert all(
        item["image_url"].startswith("data:image/jpeg;base64,")
        for item in user_content[1:]
    )
    assert all(item["detail"] == "high" for item in user_content[1:])


def test_openai_learning_provider_rejects_result_count_mismatch(tmp_path) -> None:
    session = _make_session(tmp_path)
    structured = _structured_result(session)
    incomplete = structured.model_copy(
        update={"reference_analyses": structured.reference_analyses[:1]},
        deep=True,
    )
    responses = FakeResponses(result=SimpleNamespace(output_parsed=incomplete))

    with pytest.raises(ProviderOutputError, match="different number"):
        _provider(responses).analyze(session)


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (
            APITimeoutError(
                request=httpx.Request("POST", "https://api.openai.com/v1/responses")
            ),
            ProviderTimeoutError,
        ),
        (
            APIConnectionError(
                request=httpx.Request("POST", "https://api.openai.com/v1/responses")
            ),
            ProviderServiceError,
        ),
    ],
)
def test_openai_learning_provider_maps_sdk_failures(
    tmp_path,
    error: Exception,
    expected_error: type[Exception],
) -> None:
    session = _make_session(tmp_path)

    with pytest.raises(expected_error):
        _provider(FakeResponses(error=error)).analyze(session)


def test_openai_learning_provider_rejects_missing_output(tmp_path) -> None:
    session = _make_session(tmp_path)

    with pytest.raises(ProviderOutputError, match="no structured output"):
        _provider(FakeResponses(result=SimpleNamespace(output_parsed=None))).analyze(session)
