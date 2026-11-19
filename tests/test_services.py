from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest
from fastapi import UploadFile
from PIL import Image

import app.services.preview as preview_module
from app.adapters.analysis import MockAnalysisProvider
from app.adapters.learning import MockLearningAnalysisProvider
from app.errors import StorageOperationError
from app.schemas import (
    EditingParametersV1,
    ExportRequest,
    ImageMetadata,
    PreviewRequest,
    ToneCurvePoint,
)
from app.services.preview import _apply_white_balance, render_preview
from app.services.storage import FileStorage
from app.services.workflows import PhotoWorkflow
from app.services.xmp import build_xmp_preset


def image_bytes(image_format: str = "JPEG") -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (96, 64), color=(128, 160, 190)).save(buffer, format=image_format)
    return buffer.getvalue()


def oriented_jpeg_bytes() -> bytes:
    buffer = BytesIO()
    exif = Image.Exif()
    exif[274] = 6
    Image.new("RGB", (80, 40), color=(128, 160, 190)).save(
        buffer,
        format="JPEG",
        exif=exif,
    )
    return buffer.getvalue()


def analyzed_photo_workflow(tmp_path: Path) -> tuple[PhotoWorkflow, FileStorage, str, str]:
    storage = FileStorage(tmp_path)
    workflow = PhotoWorkflow(storage=storage, analysis_provider=MockAnalysisProvider())
    upload = UploadFile(file=BytesIO(image_bytes("JPEG")), filename="photo.jpg")
    session = asyncio.run(workflow.upload(upload))
    analysis = workflow.analyze(session.id)
    return workflow, storage, session.id, analysis.recommended_styles[0].id


def test_extract_metadata_for_jpeg_and_png(tmp_path):
    storage = FileStorage(tmp_path)
    jpg = tmp_path / "sample.jpg"
    png = tmp_path / "sample.png"
    jpg.write_bytes(image_bytes("JPEG"))
    png.write_bytes(image_bytes("PNG"))

    jpg_metadata = storage.extract_metadata(jpg, "image/jpeg")
    png_metadata = storage.extract_metadata(png, "image/png")

    assert jpg_metadata.format == "JPEG"
    assert png_metadata.format == "PNG"
    assert jpg_metadata.width == 96
    assert png_metadata.height == 64


def test_extract_metadata_uses_visual_exif_orientation(tmp_path):
    storage = FileStorage(tmp_path)
    image_path = tmp_path / "rotated.jpg"
    image_path.write_bytes(oriented_jpeg_bytes())

    metadata = storage.extract_metadata(image_path, "image/jpeg")

    assert (metadata.width, metadata.height) == (40, 80)


def test_mock_provider_returns_three_complete_styles():
    provider = MockAnalysisProvider()
    analysis = provider.analyze(
        Path("unused.jpg"),
        ImageMetadata(
            width=1200,
            height=800,
            format="JPEG",
            color_mode="RGB",
            file_size=4096,
            content_type="image/jpeg",
        )
    )

    assert len(analysis.recommended_styles) == 3
    assert {style.id for style in analysis.recommended_styles} == {
        "natural-documentary",
        "cinematic-teal-warm",
        "contrast-bw",
    }
    assert all(
        style.parameters.schema_version == "editing_parameters_v1"
        for style in analysis.recommended_styles
    )


def test_editing_parameters_scale_and_clamp():
    params = EditingParametersV1(
        exposure=2.0,
        contrast=80,
        highlights=-60,
        saturation=-40,
        sharpening={"amount": 100, "radius": 1.0, "detail": 40, "masking": 20},
        noise_reduction={"luminance": 40, "color": 25},
    )

    scaled = params.scaled(0.5)

    assert scaled.exposure == 1.0
    assert scaled.contrast == 40
    assert scaled.highlights == -30
    assert scaled.saturation == -20
    assert scaled.sharpening.amount == 50
    assert scaled.noise_reduction.luminance == 20


def test_xmp_contains_mapping_name_and_key_crs_fields():
    params = EditingParametersV1(exposure=0.5, contrast=20, saturation=10)

    xmp = build_xmp_preset("AI Photo Agent - Test", params)

    assert "lightroom_xmp_v2" in xmp
    assert "<crs:Name>" in xmp
    assert '<rdf:li xml:lang="x-default">AI Photo Agent - Test</rdf:li>' in xmp
    assert 'crs:Exposure2012="0.50"' in xmp
    assert 'crs:Contrast2012="20"' in xmp
    assert "crs:ToneCurvePV2012" in xmp
    assert "crs:SplitToningShadowHue" in xmp
    assert "crs:Sharpness" in xmp


def test_preview_renderer_creates_non_empty_file(tmp_path):
    original = tmp_path / "original.jpg"
    preview = tmp_path / "preview.jpg"
    original.write_bytes(image_bytes("JPEG"))

    render_preview(original, preview, EditingParametersV1(exposure=0.3, contrast=15, vibrance=20))

    assert preview.exists()
    assert preview.stat().st_size > 0


def test_failed_preview_replace_preserves_previous_file_and_removes_temp_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original = tmp_path / "original.jpg"
    output = tmp_path / "preview.jpg"
    original.write_bytes(image_bytes("JPEG"))
    output.write_bytes(b"previous")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(preview_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        render_preview(original, output, EditingParametersV1().scaled(0.0))

    assert output.read_bytes() == b"previous"
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.parametrize(
    ("operation", "save_method", "asset_pattern"),
    [
        ("preview", "save_preview_asset", "*.jpg"),
        ("export", "save_preset_asset", "*.xmp"),
    ],
)
def test_asset_metadata_failure_removes_only_new_uncommitted_files(
    monkeypatch,
    tmp_path: Path,
    operation: str,
    save_method: str,
    asset_pattern: str,
) -> None:
    workflow, storage, photo_id, style_id = analyzed_photo_workflow(tmp_path)
    request = (
        PreviewRequest(style_id=style_id, intensity=0.7)
        if operation == "preview"
        else ExportRequest(style_id=style_id, intensity=0.7)
    )
    asset_root = storage.generated_dir if operation == "preview" else storage.exports_dir

    def fail_save(*_args, **_kwargs) -> None:
        raise OSError("injected metadata failure")

    monkeypatch.setattr(storage, save_method, fail_save)

    with pytest.raises(StorageOperationError, match="preview asset|preset asset"):
        getattr(workflow, operation)(photo_id, request)

    assert list(asset_root.rglob(asset_pattern)) == []


@pytest.mark.parametrize(
    ("operation", "save_method", "asset_pattern"),
    [
        ("preview", "save_preview_asset", "*.jpg"),
        ("export", "save_preset_asset", "*.xmp"),
    ],
)
def test_asset_metadata_failure_preserves_an_existing_committed_file(
    monkeypatch,
    tmp_path: Path,
    operation: str,
    save_method: str,
    asset_pattern: str,
) -> None:
    workflow, storage, photo_id, style_id = analyzed_photo_workflow(tmp_path)
    request = (
        PreviewRequest(style_id=style_id, intensity=0.7)
        if operation == "preview"
        else ExportRequest(style_id=style_id, intensity=0.7)
    )
    asset_root = storage.generated_dir if operation == "preview" else storage.exports_dir
    getattr(workflow, operation)(photo_id, request)
    [asset_path] = list(asset_root.rglob(asset_pattern))
    committed_content = asset_path.read_bytes()

    def fail_save(*_args, **_kwargs) -> None:
        raise OSError("injected metadata failure")

    monkeypatch.setattr(storage, save_method, fail_save)

    with pytest.raises(StorageOperationError, match="preview asset|preset asset"):
        getattr(workflow, operation)(photo_id, request)

    assert asset_path.read_bytes() == committed_content


def test_asset_tokens_follow_effective_output_not_raw_float_spelling(tmp_path: Path) -> None:
    workflow, storage, photo_id, style_id = analyzed_photo_workflow(tmp_path)

    first_preview = workflow.preview(photo_id, PreviewRequest(style_id=style_id, intensity=0.001))
    second_preview = workflow.preview(photo_id, PreviewRequest(style_id=style_id, intensity=0.002))
    positive_zero = workflow.preview(photo_id, PreviewRequest(style_id=style_id, intensity=0.0))
    negative_zero = workflow.preview(photo_id, PreviewRequest(style_id=style_id, intensity=-0.0))
    first_export = workflow.export(photo_id, ExportRequest(style_id=style_id, intensity=0.001))
    second_export = workflow.export(photo_id, ExportRequest(style_id=style_id, intensity=0.002))

    assert first_preview.parameters == second_preview.parameters
    assert first_preview.preview_url == second_preview.preview_url
    assert positive_zero.preview_url == negative_zero.preview_url
    assert first_export.download_url == second_export.download_url

    session = storage.load_session(photo_id)
    style = session.analysis.recommended_styles[0]  # type: ignore[union-attr]
    style.parameters.sharpening.amount = 150
    storage.save_session(session)

    lower = workflow.preview(photo_id, PreviewRequest(style_id=style_id, intensity=0.701))
    upper = workflow.preview(photo_id, PreviewRequest(style_id=style_id, intensity=0.704))

    assert round(lower.intensity * 100) == round(upper.intensity * 100) == 70
    assert lower.parameters != upper.parameters
    assert lower.preview_url != upper.preview_url


def test_same_root_asset_transaction_prevents_failed_request_from_deleting_committed_asset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    first_storage = FileStorage(tmp_path)
    second_storage = FileStorage(tmp_path)
    first_workflow = PhotoWorkflow(
        storage=first_storage,
        analysis_provider=MockAnalysisProvider(),
    )
    second_workflow = PhotoWorkflow(
        storage=second_storage,
        analysis_provider=MockAnalysisProvider(),
    )
    upload = UploadFile(file=BytesIO(image_bytes("JPEG")), filename="photo.jpg")
    session = asyncio.run(first_workflow.upload(upload))
    style_id = first_workflow.analyze(session.id).recommended_styles[0].id
    request = PreviewRequest(style_id=style_id, intensity=0.7)
    first_at_commit = Event()
    release_first = Event()

    def fail_after_render(*_args, **_kwargs) -> None:
        first_at_commit.set()
        assert release_first.wait(timeout=5)
        raise OSError("injected metadata failure")

    monkeypatch.setattr(first_storage, "save_preview_asset", fail_after_render)

    with ThreadPoolExecutor(max_workers=2) as executor:
        failed = executor.submit(first_workflow.preview, session.id, request)
        assert first_at_commit.wait(timeout=5)
        successful = executor.submit(second_workflow.preview, session.id, request)
        release_first.set()

        with pytest.raises(StorageOperationError):
            failed.result(timeout=5)
        response = successful.result(timeout=5)

    persisted = second_storage.load_session(session.id)
    [asset] = persisted.preview_files.values()
    assert asset.url == response.preview_url
    assert asset.path.is_file()


def test_preview_renderer_applies_tone_curve(tmp_path):
    original = tmp_path / "original.jpg"
    neutral_preview = tmp_path / "neutral.jpg"
    lifted_preview = tmp_path / "lifted.jpg"
    Image.new("RGB", (32, 32), color=(128, 128, 128)).save(original, format="JPEG")

    render_preview(original, neutral_preview, EditingParametersV1())
    render_preview(
        original,
        lifted_preview,
        EditingParametersV1(
            tone_curve=[
                ToneCurvePoint(x=0, y=0),
                ToneCurvePoint(x=128, y=190),
                ToneCurvePoint(x=255, y=255),
            ]
        ),
    )

    with Image.open(neutral_preview) as neutral, Image.open(lifted_preview) as lifted:
        assert lifted.getpixel((16, 16))[0] > neutral.getpixel((16, 16))[0]


def test_preview_tint_uses_positive_magenta_negative_green_direction():
    image = Image.new("RGB", (1, 1), color=(100, 100, 100))

    positive = _apply_white_balance(image, EditingParametersV1(tint=100))
    negative = _apply_white_balance(image, EditingParametersV1(tint=-100))

    positive_red, positive_green, positive_blue = positive.getpixel((0, 0))
    negative_red, negative_green, negative_blue = negative.getpixel((0, 0))
    assert positive_red > positive_green
    assert positive_blue > positive_green
    assert negative_green > negative_red
    assert negative_green > negative_blue


def test_learning_session_storage_and_reference_upload_round_trip(tmp_path):
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("建筑构图")
    upload = UploadFile(file=BytesIO(image_bytes("JPEG")), filename="reference.jpg")

    reference = asyncio.run(
        storage.save_learning_reference_upload(
            session=session,
            upload=upload,
            source_url="https://example.com/reference",
            author="Photographer",
            license="CC BY",
        )
    )
    loaded = storage.load_learning_session(session.id)

    assert loaded.topic == "建筑构图"
    assert len(loaded.references) == 1
    assert loaded.references[0].id == reference.id
    assert loaded.references[0].source_url == "https://example.com/reference"
    assert loaded.references[0].metadata.format == "JPEG"
    assert loaded.references[0].image_file.exists()
    assert loaded.references[0].image_url == (
        f"/api/learning/sessions/{session.id}/references/{reference.id}/image"
    )


def test_mock_learning_provider_returns_complete_analysis(tmp_path):
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("低调人像")
    upload = UploadFile(file=BytesIO(image_bytes("PNG")), filename="reference.png")
    asyncio.run(storage.save_learning_reference_upload(session=session, upload=upload))

    analyzed = MockLearningAnalysisProvider().analyze(storage.load_learning_session(session.id))

    assert analyzed.summary is not None
    assert "低调人像" in analyzed.summary.style_overview
    assert analyzed.references[0].analysis is not None
    assert analyzed.references[0].analysis.composition_structure
    assert analyzed.references[0].analysis.color_palette
    assert analyzed.references[0].analysis.practical_lessons
