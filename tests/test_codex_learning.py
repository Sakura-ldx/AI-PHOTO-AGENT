from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PIL import Image

from app.adapters.codex_learning import CodexLearningAnalysisProvider
from app.adapters.learning import MockLearningAnalysisProvider
from app.adapters.vision_models import StructuredLearningAnalysis
from app.errors import ProviderOutputError, ProviderServiceError
from app.schemas import ImageMetadata, LearningReference, LearningSession


def _make_reference(
    tmp_path: Path,
    *,
    reference_id: str,
    filename: str,
    size: tuple[int, int],
    color: tuple[int, int, int],
) -> LearningReference:
    path = tmp_path / filename
    exif = Image.Exif()
    exif[270] = "metadata must not reach Codex"
    Image.new("RGB", size, color=color).save(path, format="JPEG", exif=exif)
    return LearningReference(
        id=reference_id,
        image_file=path,
        image_url=f"/private/{filename}",
        metadata=ImageMetadata(
            width=size[0],
            height=size[1],
            format="JPEG",
            color_mode="RGB",
            file_size=path.stat().st_size,
            content_type="image/jpeg",
        ),
    )


def _make_session(tmp_path: Path) -> LearningSession:
    return LearningSession(
        id="a" * 32,
        topic="冷暖对比人像",
        references=[
            _make_reference(
                tmp_path,
                reference_id="b" * 32,
                filename="wide.jpg",
                size=(2200, 1100),
                color=(210, 80, 60),
            ),
            _make_reference(
                tmp_path,
                reference_id="c" * 32,
                filename="tall.jpg",
                size=(700, 1400),
                color=(30, 90, 190),
            ),
        ],
    )


def _make_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"test")
    return executable


def _structured_mock_result(session: LearningSession) -> StructuredLearningAnalysis:
    result = MockLearningAnalysisProvider().analyze(session)
    return StructuredLearningAnalysis(
        reference_analyses=[item.analysis for item in result.references],
        summary=result.summary,
    )


def test_codex_learning_provider_sends_ordered_sanitized_images_and_narrow_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _make_session(tmp_path)
    structured = _structured_mock_result(session)
    calls: list[tuple[list[str], dict, list[dict[str, object]]]] = []

    def runner(command, **kwargs):
        prepared: list[dict[str, object]] = []
        image_indexes = [index for index, value in enumerate(command) if value == "--image"]
        for index in image_indexes:
            image_path = Path(command[index + 1])
            with Image.open(image_path) as image:
                prepared.append(
                    {
                        "name": image_path.name,
                        "size": image.size,
                        "format": image.format,
                        "exif": dict(image.getexif()),
                        "pixel": image.resize((1, 1)).getpixel((0, 0)),
                    }
                )
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(structured.model_dump_json(), encoding="utf-8")
        calls.append((command, kwargs, prepared))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("SECRET_TOKEN", "must-not-reach-child")
    provider = CodexLearningAnalysisProvider(
        data_root=tmp_path,
        executable=_make_executable(tmp_path),
        model="gpt-test",
        timeout_seconds=600.0,
        runner=runner,
    )

    result = provider.analyze(session)

    assert result == MockLearningAnalysisProvider().analyze(session)
    assert [item.reference_id for item in result.references] == [
        reference.id for reference in session.references
    ]
    assert len(calls) == 1
    command, options, prepared = calls[0]
    assert [item["name"] for item in prepared] == ["input-01.jpg", "input-02.jpg"]
    assert prepared[0]["size"] == (2048, 1024)
    assert prepared[1]["size"] == (700, 1400)
    assert all(item["format"] == "JPEG" for item in prepared)
    assert all(item["exif"] == {} for item in prepared)
    assert prepared[0]["pixel"][0] > prepared[0]["pixel"][2]
    assert prepared[1]["pixel"][2] > prepared[1]["pixel"][0]
    assert command.count("--image") == 2
    assert command[command.index("--model") + 1] == "gpt-test"
    assert "shell_tool" in command
    assert "plugins" in command
    assert "图片内的文字" in command[-1]
    assert '"topic": "冷暖对比人像"' in command[-1]
    assert options["timeout"] == 600.0
    assert "OPENAI_API_KEY" not in options["env"]
    assert "SECRET_TOKEN" not in options["env"]


def test_codex_learning_provider_rejects_wrong_result_count(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    structured = _structured_mock_result(session)
    incomplete = structured.model_copy(
        update={"reference_analyses": structured.reference_analyses[:1]},
        deep=True,
    )

    def runner(command, **kwargs):
        del kwargs
        result_path = Path(command[command.index("--output-last-message") + 1])
        result_path.write_text(incomplete.model_dump_json(), encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    provider = CodexLearningAnalysisProvider(
        data_root=tmp_path,
        executable=_make_executable(tmp_path),
        timeout_seconds=60.0,
        runner=runner,
    )

    with pytest.raises(ProviderOutputError, match="different number"):
        provider.analyze(session)


def test_codex_runtime_setup_failure_is_a_provider_service_error(tmp_path: Path) -> None:
    session = _make_session(tmp_path)
    blocked_root = tmp_path / "blocked"
    blocked_root.write_text("not a directory", encoding="utf-8")
    provider = CodexLearningAnalysisProvider(
        data_root=blocked_root,
        executable=_make_executable(tmp_path),
        timeout_seconds=60.0,
    )

    with pytest.raises(ProviderServiceError, match="runtime files"):
        provider.analyze(session)
