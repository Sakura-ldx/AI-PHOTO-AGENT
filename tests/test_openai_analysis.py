from __future__ import annotations

import base64
import json
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError, OpenAI
from openai.lib._pydantic import to_strict_json_schema
from PIL import Image, ImageCms

import app.adapters.model_image as model_image_module
from app.adapters.analysis import MockAnalysisProvider
from app.adapters.openai_analysis import (
    MAX_OUTPUT_TOKENS,
    MODEL_IMAGE_MAX_SIZE,
    OpenAIAnalysisProvider,
    OpenAIImageAnalysis,
)
from app.errors import ProviderOutputError, ProviderServiceError, ProviderTimeoutError
from app.schemas import ImageAnalysis, ImageMetadata


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


def make_metadata() -> ImageMetadata:
    return ImageMetadata(
        width=3200,
        height=1600,
        format="PNG",
        color_mode="RGBA",
        file_size=100,
        content_type="image/png",
    )


def make_large_transparent_png(path) -> None:
    Image.new("RGBA", (3200, 1600), color=(30, 60, 90, 128)).save(path, format="PNG")


def test_model_image_color_manages_cmyk_before_rgb_flatten(monkeypatch, tmp_path) -> None:
    image_path = tmp_path / "cmyk.jpg"
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    Image.new("CMYK", (8, 6), color=(20, 40, 60, 10)).save(
        image_path,
        format="JPEG",
        icc_profile=profile,
    )
    observed: dict[str, str] = {}

    def fake_profile_to_profile(image, _source, _target, *, outputMode):
        observed["input_mode"] = image.mode
        observed["output_mode"] = outputMode
        return image.convert(outputMode)

    monkeypatch.setattr(
        model_image_module.ImageCms,
        "profileToProfile",
        fake_profile_to_profile,
    )

    prepared = model_image_module.prepare_model_image(image_path)

    assert observed == {"input_mode": "CMYK", "output_mode": "RGB"}
    with Image.open(BytesIO(prepared.content)) as output:
        assert output.mode == "RGB"


def make_provider(responses: FakeResponses) -> OpenAIAnalysisProvider:
    client = SimpleNamespace(responses=responses)
    return OpenAIAnalysisProvider(
        api_key="test-key",
        model="gpt-5.6-terra",
        timeout_seconds=12.0,
        max_retries=1,
        client=client,
    )


def to_openai_output(analysis: ImageAnalysis) -> OpenAIImageAnalysis:
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
    return OpenAIImageAnalysis.model_validate(payload)


def test_openai_provider_sends_bounded_image_and_returns_structured_analysis(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    make_large_transparent_png(image_path)
    metadata = make_metadata()
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    responses = FakeResponses(
        result=SimpleNamespace(
            output_parsed=to_openai_output(expected),
            usage=SimpleNamespace(input_tokens=100, output_tokens=200),
            _request_id="req_test",
        )
    )

    actual = make_provider(responses).analyze(image_path, metadata)

    assert actual == expected
    assert len(responses.calls) == 1
    request = responses.calls[0]
    assert request["model"] == "gpt-5.6-terra"
    assert request["text_format"] is OpenAIImageAnalysis
    assert request["reasoning"] == {"effort": "low"}
    assert request["max_output_tokens"] == MAX_OUTPUT_TOKENS
    assert request["store"] is False

    image_input = request["input"][1]["content"][1]
    assert image_input["detail"] == "high"
    prefix, encoded = image_input["image_url"].split(",", maxsplit=1)
    assert prefix == "data:image/jpeg;base64"
    with Image.open(BytesIO(base64.b64decode(encoded))) as prepared:
        assert prepared.format == "JPEG"
        assert prepared.mode == "RGB"
        assert prepared.width <= MODEL_IMAGE_MAX_SIZE[0]
        assert prepared.height <= MODEL_IMAGE_MAX_SIZE[1]


def test_openai_structured_output_schema_uses_only_fixed_objects() -> None:
    schema = to_strict_json_schema(OpenAIImageAnalysis)

    def assert_strict_objects(value) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_strict_objects(child)
        elif isinstance(value, list):
            for child in value:
                assert_strict_objects(child)

    assert_strict_objects(schema)


def test_real_openai_sdk_serializes_and_parses_the_provider_request(tmp_path) -> None:
    image_path = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 48), color=(80, 100, 120)).save(image_path)
    metadata = ImageMetadata(
        width=64,
        height=48,
        format="JPEG",
        color_mode="RGB",
        file_size=image_path.stat().st_size,
        content_type="image/jpeg",
    )
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    structured_output = to_openai_output(expected)
    captured_request: dict = {}

    def handle_request(request: httpx.Request) -> httpx.Response:
        captured_request.update(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"x-request-id": "req_mock_transport"},
            json={
                "id": "resp_mock_transport",
                "created_at": 1_700_000_000,
                "model": "gpt-5.6-terra",
                "object": "response",
                "output": [
                    {
                        "id": "msg_mock_transport",
                        "content": [
                            {
                                "annotations": [],
                                "text": structured_output.model_dump_json(),
                                "type": "output_text",
                            }
                        ],
                        "role": "assistant",
                        "status": "completed",
                        "type": "message",
                    }
                ],
                "parallel_tool_calls": True,
                "tool_choice": "auto",
                "tools": [],
            },
            request=request,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handle_request))
    sdk_client = OpenAI(api_key="test-key", http_client=http_client)
    try:
        provider = OpenAIAnalysisProvider(
            api_key="test-key",
            model="gpt-5.6-terra",
            timeout_seconds=12.0,
            max_retries=0,
            client=sdk_client,
        )
        actual = provider.analyze(image_path, metadata)
    finally:
        sdk_client.close()

    assert actual == expected
    assert captured_request["model"] == "gpt-5.6-terra"
    assert captured_request["store"] is False
    assert captured_request["input"][1]["content"][1]["type"] == "input_image"
    schema = captured_request["text"]["format"]["schema"]
    assert schema["additionalProperties"] is False


def test_openai_provider_rejects_missing_structured_output(tmp_path) -> None:
    image_path = tmp_path / "photo.png"
    Image.new("RGB", (32, 24)).save(image_path)
    responses = FakeResponses(result=SimpleNamespace(output_parsed=None))

    with pytest.raises(ProviderOutputError, match="no structured output"):
        make_provider(responses).analyze(image_path, make_metadata())


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (
            APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses")),
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
def test_openai_provider_maps_sdk_failures(
    tmp_path,
    error: Exception,
    expected_error: type[Exception],
) -> None:
    image_path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 24)).save(image_path)
    responses = FakeResponses(error=error)

    with pytest.raises(expected_error):
        make_provider(responses).analyze(image_path, make_metadata())


def test_openai_provider_requires_nonblank_api_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        OpenAIAnalysisProvider(
            api_key=" ",
            model="gpt-5.6-terra",
            timeout_seconds=10.0,
            max_retries=0,
        )
