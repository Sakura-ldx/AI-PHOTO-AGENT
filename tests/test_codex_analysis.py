from __future__ import annotations

import json
import subprocess

import pytest
from PIL import Image

from app.adapters.analysis import MockAnalysisProvider
from app.adapters.codex_analysis import CodexAnalysisProvider, _strict_output_schema
from app.adapters.openai_analysis import OpenAIImageAnalysis
from app.errors import ProviderOutputError, ProviderServiceError, ProviderTimeoutError
from app.schemas import ImageAnalysis, ImageMetadata


def make_metadata(image_path) -> ImageMetadata:
    return ImageMetadata(
        width=64,
        height=48,
        format="JPEG",
        color_mode="RGB",
        file_size=image_path.stat().st_size,
        content_type="image/jpeg",
    )


def to_structured_output(analysis: ImageAnalysis) -> OpenAIImageAnalysis:
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


def make_image(tmp_path):
    image_path = tmp_path / "photo.jpg"
    Image.new("RGB", (64, 48), color=(50, 100, 150)).save(image_path)
    return image_path


def make_executable(tmp_path):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"test")
    return executable


def test_codex_provider_uses_image_cli_and_current_login_without_api_key(
    tmp_path,
    monkeypatch,
) -> None:
    image_path = make_image(tmp_path)
    metadata = make_metadata(image_path)
    expected = MockAnalysisProvider().analyze(image_path, metadata)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        result_path = command[command.index("--output-last-message") + 1]
        with open(result_path, "w", encoding="utf-8") as output:
            json.dump(to_structured_output(expected).model_dump(), output)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-codex")
    provider = CodexAnalysisProvider(
        data_root=tmp_path,
        executable=make_executable(tmp_path),
        model="gpt-test",
        timeout_seconds=180.0,
        runner=runner,
    )

    actual = provider.analyze(image_path, metadata)

    assert actual == expected
    command, options = calls[0]
    assert command[1] == "exec"
    assert "--ephemeral" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert command[command.index("--model") + 1] == "gpt-test"
    assert command[command.index("--image") + 1].endswith("input.jpg")
    assert options["input"] == ""
    assert options["encoding"] == "utf-8"
    assert options["errors"] == "replace"
    assert options["timeout"] == 180.0
    assert "OPENAI_API_KEY" not in options["env"]


def test_codex_output_schema_makes_every_object_strict() -> None:
    schema = _strict_output_schema()

    def assert_strict(value) -> None:
        if isinstance(value, dict):
            if "properties" in value:
                assert value["additionalProperties"] is False
                assert value["required"] == list(value["properties"])
            assert "default" not in value
            for child in value.values():
                assert_strict(child)
        elif isinstance(value, list):
            for child in value:
                assert_strict(child)

    assert_strict(schema)


@pytest.mark.parametrize(
    ("runner", "expected_error"),
    [
        (
            lambda command, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(command, kwargs["timeout"])
            ),
            ProviderTimeoutError,
        ),
        (
            lambda command, **kwargs: subprocess.CompletedProcess(
                command,
                1,
                stdout="",
                stderr="failed",
            ),
            ProviderServiceError,
        ),
    ],
)
def test_codex_provider_maps_process_failures(tmp_path, runner, expected_error) -> None:
    image_path = make_image(tmp_path)
    provider = CodexAnalysisProvider(
        data_root=tmp_path,
        executable=make_executable(tmp_path),
        timeout_seconds=60.0,
        runner=runner,
    )

    with pytest.raises(expected_error):
        provider.analyze(image_path, make_metadata(image_path))


def test_codex_provider_rejects_malformed_output(tmp_path) -> None:
    image_path = make_image(tmp_path)

    def runner(command, **kwargs):
        del kwargs
        result_path = command[command.index("--output-last-message") + 1]
        with open(result_path, "w", encoding="utf-8") as output:
            output.write("not-json")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    provider = CodexAnalysisProvider(
        data_root=tmp_path,
        executable=make_executable(tmp_path),
        timeout_seconds=60.0,
        runner=runner,
    )

    with pytest.raises(ProviderOutputError, match="invalid structured output"):
        provider.analyze(image_path, make_metadata(image_path))
