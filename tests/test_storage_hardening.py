from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from fastapi import UploadFile
from PIL import Image

import app.services.storage as storage_module
from app.adapters.analysis import MockAnalysisProvider
from app.adapters.learning import MockLearningAnalysisProvider
from app.errors import (
    InvalidRequestError,
    ResourceNotFoundError,
    StorageOperationError,
    StoredDataError,
    UploadTooLargeError,
    WorkflowConflictError,
)
from app.schemas import (
    GeneratedAsset,
    LearningSession,
    LearningSummary,
    PhotoSession,
)
from app.services.storage import FileStorage


def image_bytes(image_format: str = "JPEG", size: tuple[int, int] = (32, 24)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=(80, 120, 160)).save(buffer, format=image_format)
    return buffer.getvalue()


def upload(content: bytes, filename: str = "image.jpg") -> UploadFile:
    return UploadFile(file=BytesIO(content), filename=filename)


class ShortReadUpload:
    content_type = "image/png"

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)

    async def read(self, _size: int = -1) -> bytes:
        return next(self._chunks, b"")


def learning_summary(label: str = "old") -> LearningSummary:
    return LearningSummary(
        style_overview=label,
        common_composition_patterns=[label],
        common_color_strategies=[label],
        mistakes_to_avoid=[label],
        exercises=[label],
    )


def test_private_state_is_separate_and_legacy_photo_session_is_read_only(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    photo_id = "a" * 32
    original = storage.uploads_dir / photo_id / "original.jpg"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(image_bytes())
    metadata = storage.extract_metadata(original, "image/jpeg")
    session = PhotoSession(id=photo_id, original_file=original, metadata=metadata)

    legacy_path = storage.legacy_sessions_dir / f"{photo_id}.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_content = session.model_dump_json(indent=2)
    legacy_path.write_text(legacy_content, encoding="utf-8")

    loaded = storage.load_session(photo_id)

    assert loaded.id == photo_id
    assert storage.generated_dir == tmp_path.resolve() / "generated" / "previews"
    assert storage.sessions_dir.is_relative_to(storage.data_dir)
    assert not storage.sessions_dir.is_relative_to(storage.generated_dir)
    assert not storage.session_path(photo_id).exists()
    assert legacy_path.read_text(encoding="utf-8") == legacy_content

    storage.save_session(loaded)

    assert storage.session_path(photo_id).exists()
    assert legacy_path.read_text(encoding="utf-8") == legacy_content


def test_ids_and_asset_paths_cannot_escape_storage_roots(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)

    for invalid_id in ("missing", "A" * 32, r"..\outside", "a" * 31, "a" * 33):
        with pytest.raises(ResourceNotFoundError):
            storage.load_session(invalid_id)
        with pytest.raises(ResourceNotFoundError):
            storage.load_learning_session(invalid_id)

    with pytest.raises(InvalidRequestError):
        storage.generated_url(tmp_path / "outside.jpg")
    with pytest.raises(InvalidRequestError):
        storage.reference_url(tmp_path / "outside.jpg")


def test_upload_byte_limit_and_invalid_image_leave_no_orphans(tmp_path: Path) -> None:
    size_limited = FileStorage(tmp_path / "size", max_upload_bytes=8)

    with pytest.raises(UploadTooLargeError):
        asyncio.run(size_limited.save_upload(upload(b"123456789")))
    assert list(size_limited.uploads_dir.iterdir()) == []

    invalid = FileStorage(tmp_path / "invalid")
    with pytest.raises(InvalidRequestError):
        asyncio.run(invalid.save_upload(upload(b"not an image")))
    assert list(invalid.uploads_dir.iterdir()) == []

    with pytest.raises(InvalidRequestError):
        asyncio.run(invalid.save_upload(upload(b"")))
    assert list(invalid.uploads_dir.iterdir()) == []


def test_upload_limit_handles_streams_that_return_short_reads(tmp_path: Path) -> None:
    png = image_bytes("PNG")
    storage = FileStorage(tmp_path, max_upload_bytes=len(png))
    short_read = ShortReadUpload([png, b"trailing bytes"])

    with pytest.raises(UploadTooLargeError):
        asyncio.run(storage.save_upload(short_read))

    assert list(storage.uploads_dir.iterdir()) == []


def test_complete_image_validation_and_pixel_limit_cleanup(tmp_path: Path) -> None:
    pixel_limited = FileStorage(tmp_path / "pixels", max_image_pixels=100)

    with pytest.raises(UploadTooLargeError):
        asyncio.run(pixel_limited.save_upload(upload(image_bytes(size=(11, 10)))))
    assert list(pixel_limited.uploads_dir.iterdir()) == []

    complete_validation = FileStorage(tmp_path / "complete")
    truncated_jpeg = image_bytes()[:40]
    with pytest.raises(InvalidRequestError):
        asyncio.run(complete_validation.save_upload(upload(truncated_jpeg)))
    assert list(complete_validation.uploads_dir.iterdir()) == []


def test_custom_pixel_limit_cannot_weaken_pillow_safety_threshold(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 50)

    with pytest.raises(ValueError, match="Pillow's decompression safety threshold"):
        FileStorage(tmp_path, max_image_pixels=51)


def test_invalid_learning_reference_upload_leaves_no_orphan(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("invalid reference")

    with pytest.raises(InvalidRequestError):
        asyncio.run(storage.save_learning_reference_upload(session, upload(b"not an image")))

    assert list(storage.references_dir.iterdir()) == []
    assert storage.load_learning_session(session.id).references == []


def test_json_writes_are_atomic_and_corrupt_state_has_domain_error(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("atomic state")
    session.summary = learning_summary("saved")
    storage.save_learning_session(session)

    assert storage.load_learning_session(session.id).summary == session.summary
    assert list(storage.data_dir.rglob("*.tmp")) == []

    storage.learning_session_path(session.id).write_text("{broken", encoding="utf-8")
    with pytest.raises(StoredDataError):
        storage.load_learning_session(session.id)


def test_failed_json_replace_preserves_previous_state_and_removes_temp_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("before")
    state_path = storage.learning_session_path(session.id)
    previous_state = state_path.read_bytes()
    session.topic = "after"

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected replace failure")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)

    with pytest.raises(StorageOperationError, match="atomically persist"):
        storage.save_learning_session(session)

    assert state_path.read_bytes() == previous_state
    assert list(storage.data_dir.rglob("*.tmp")) == []


def test_legacy_learning_session_is_read_then_migrated_on_write(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    legacy = LearningSession(id="b" * 32, topic="legacy learning")
    legacy_path = storage.legacy_learning_sessions_dir / f"{legacy.id}.json"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_text(legacy.model_dump_json(indent=2), encoding="utf-8")

    loaded = storage.load_learning_session(legacy.id)

    assert loaded.topic == "legacy learning"
    assert not storage.learning_session_path(legacy.id).exists()

    loaded.topic = "migrated learning"
    storage.save_learning_session(loaded)

    assert storage.learning_session_path(legacy.id).exists()
    assert storage.load_learning_session(legacy.id).topic == "migrated learning"


def test_reference_commits_reload_latest_session_and_invalidate_summary(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("concurrent references")
    session.summary = learning_summary()
    storage.save_learning_session(session)
    first_stale_copy = storage.load_learning_session(session.id)
    second_stale_copy = storage.load_learning_session(session.id)

    async def add_both() -> None:
        await asyncio.gather(
            storage.save_learning_reference_upload(
                first_stale_copy,
                upload(image_bytes(), "first.jpg"),
            ),
            storage.save_learning_reference_upload(
                second_stale_copy,
                upload(image_bytes("PNG"), "second.png"),
            ),
        )

    asyncio.run(add_both())
    loaded = storage.load_learning_session(session.id)

    assert len(loaded.references) == 2
    assert len({reference.id for reference in loaded.references}) == 2
    assert loaded.summary is None
    assert all(reference.image_file.exists() for reference in loaded.references)


def test_multiple_storage_instances_share_same_root_lock(tmp_path: Path) -> None:
    first_storage = FileStorage(tmp_path)
    second_storage = FileStorage(tmp_path)
    session = first_storage.create_learning_session("shared root")
    first_snapshot = first_storage.load_learning_session(session.id)
    second_snapshot = second_storage.load_learning_session(session.id)

    async def add_both() -> None:
        await asyncio.gather(
            first_storage.save_learning_reference_upload(
                first_snapshot,
                upload(image_bytes(), "first.jpg"),
            ),
            second_storage.save_learning_reference_upload(
                second_snapshot,
                upload(image_bytes("PNG"), "second.png"),
            ),
        )

    asyncio.run(add_both())

    assert len(first_storage.load_learning_session(session.id).references) == 2


def test_concurrent_reference_uploads_cannot_exceed_session_limit(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("bounded concurrent uploads")

    async def exercise() -> list[object]:
        for index in range(7):
            await storage.save_learning_reference_upload(
                session,
                upload(image_bytes(), f"seed-{index}.jpg"),
            )
        first_snapshot = storage.load_learning_session(session.id)
        second_snapshot = storage.load_learning_session(session.id)
        return await asyncio.gather(
            storage.save_learning_reference_upload(
                first_snapshot,
                upload(image_bytes(), "eighth-a.jpg"),
            ),
            storage.save_learning_reference_upload(
                second_snapshot,
                upload(image_bytes("PNG"), "eighth-b.png"),
            ),
            return_exceptions=True,
        )

    results = asyncio.run(exercise())

    assert sum(isinstance(result, WorkflowConflictError) for result in results) == 1
    assert sum(not isinstance(result, Exception) for result in results) == 1
    loaded = storage.load_learning_session(session.id)
    assert len(loaded.references) == 8
    session_reference_dir = storage.references_dir / session.id
    assert len([path for path in session_reference_dir.iterdir() if path.is_dir()]) == 8


def test_repeated_concurrent_reference_directory_creation_is_stable(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)

    async def exercise() -> None:
        for index in range(20):
            session = storage.create_learning_session(f"race {index}")
            first_snapshot = storage.load_learning_session(session.id)
            second_snapshot = storage.load_learning_session(session.id)
            await asyncio.gather(
                storage.save_learning_reference_upload(first_snapshot, upload(image_bytes())),
                storage.save_learning_reference_upload(
                    second_snapshot,
                    upload(image_bytes("PNG"), "second.png"),
                ),
            )
            assert len(storage.load_learning_session(session.id).references) == 2

    asyncio.run(exercise())


def test_legacy_static_reference_url_is_normalized_on_load(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("legacy URL")
    reference = asyncio.run(storage.save_learning_reference_upload(session, upload(image_bytes())))
    persisted = storage.load_learning_session(session.id)
    persisted.references[0].image_url = f"/references/{session.id}/{reference.id}/reference.jpg"
    storage.save_learning_session(persisted)

    loaded = storage.load_learning_session(session.id)

    assert loaded.references[0].image_url == (
        f"/api/learning/sessions/{session.id}/references/{reference.id}/image"
    )


def test_learning_analysis_uses_reference_snapshot_for_conflict_detection(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("snapshot")
    asyncio.run(storage.save_learning_reference_upload(session, upload(image_bytes())))

    snapshot = storage.load_learning_session(session.id)
    expected_ids = tuple(reference.id for reference in snapshot.references)
    result = MockLearningAnalysisProvider().analyze(snapshot)
    analyzed = snapshot.model_copy(
        update={
            "references": [
                reference.model_copy(
                    update={"analysis": result.references[index].analysis},
                    deep=True,
                )
                for index, reference in enumerate(snapshot.references)
            ],
            "summary": result.summary,
        },
        deep=True,
    )

    latest = storage.load_learning_session(session.id)
    asyncio.run(
        storage.save_learning_reference_upload(
            latest,
            upload(image_bytes("PNG"), "new.png"),
        )
    )

    with pytest.raises(WorkflowConflictError):
        storage.save_learning_analysis(
            analyzed,
            expected_session_id=session.id,
            expected_reference_ids=expected_ids,
        )

    current = storage.load_learning_session(session.id)
    assert len(current.references) == 2
    assert current.summary is None

    current_result = MockLearningAnalysisProvider().analyze(current)
    current_analysis = current.model_copy(
        update={
            "references": [
                reference.model_copy(
                    update={"analysis": current_result.references[index].analysis},
                    deep=True,
                )
                for index, reference in enumerate(current.references)
            ],
            "summary": current_result.summary,
        },
        deep=True,
    )
    saved = storage.save_learning_analysis(
        current_analysis,
        expected_session_id=session.id,
        expected_reference_ids=tuple(reference.id for reference in current.references),
    )
    reloaded = storage.load_learning_session(session.id)

    assert saved.summary is not None
    assert reloaded.summary == saved.summary
    assert all(reference.analysis is not None for reference in reloaded.references)


def test_analysis_is_committed_once_in_the_private_photo_session(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = asyncio.run(storage.save_upload(upload(image_bytes())))
    analysis = MockAnalysisProvider().analyze(session.original_file, session.metadata)

    storage.save_analysis(session, analysis)

    session_path = storage.session_path(session.id)
    assert session_path.exists()
    assert storage.load_session(session.id).analysis == analysis
    assert not (storage.data_dir / "analyses").exists()
    assert list(storage.data_dir.rglob("*.tmp")) == []


def test_changed_reanalysis_invalidates_registered_preview_and_preset_assets(
    tmp_path: Path,
) -> None:
    storage = FileStorage(tmp_path)
    session = asyncio.run(storage.save_upload(upload(image_bytes())))
    first = MockAnalysisProvider().analyze(session.original_file, session.metadata)
    storage.save_analysis(session, first)

    preview_path = storage.generated_dir / session.id / "old.jpg"
    preset_path = storage.exports_dir / session.id / "old.xmp"
    preview_path.parent.mkdir(parents=True)
    preset_path.parent.mkdir(parents=True)
    preview_path.write_bytes(b"old preview")
    preset_path.write_bytes(b"old preset")
    storage.save_preview_asset(
        session.id,
        "old-preview",
        GeneratedAsset(path=preview_path, url=storage.generated_url(preview_path)),
    )
    storage.save_preset_asset(
        session.id,
        "old-preset",
        GeneratedAsset(path=preset_path, url=storage.export_url(preset_path)),
    )
    changed = first.model_copy(update={"overall_summary": "a changed analysis"}, deep=True)

    saved = storage.save_analysis(session, changed)

    assert saved.preview_files == {}
    assert saved.preset_files == {}
    assert storage.load_session(session.id).analysis == changed
    assert not preview_path.exists()
    assert not preset_path.exists()


def test_missing_photo_original_is_reported_as_corrupt_state(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = asyncio.run(storage.save_upload(upload(image_bytes())))
    session.original_file.unlink()

    with pytest.raises(StoredDataError, match="original is missing"):
        storage.load_session(session.id)


def test_photo_session_original_must_stay_under_upload_root(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    session = asyncio.run(storage.save_upload(upload(image_bytes())))
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(image_bytes())
    session.original_file = outside
    storage.save_session(session)

    with pytest.raises(StoredDataError, match="path is invalid"):
        storage.load_session(session.id)


def test_stored_resource_ids_and_owned_file_paths_must_match(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    first_photo = asyncio.run(storage.save_upload(upload(image_bytes())))
    second_photo = asyncio.run(storage.save_upload(upload(image_bytes("PNG"), "second.png")))

    first_photo.original_file = second_photo.original_file
    storage.save_session(first_photo)
    with pytest.raises(StoredDataError, match="path is invalid"):
        storage.load_session(first_photo.id)

    first_learning = storage.create_learning_session("first")
    second_learning = storage.create_learning_session("second")
    first_reference = asyncio.run(
        storage.save_learning_reference_upload(first_learning, upload(image_bytes()))
    )
    second_reference = asyncio.run(
        storage.save_learning_reference_upload(
            second_learning,
            upload(image_bytes("PNG"), "second.png"),
        )
    )
    tampered = storage.load_learning_session(first_learning.id)
    assert tampered.references[0].id == first_reference.id
    tampered.references[0].image_file = second_reference.image_file
    storage.save_learning_session(tampered)

    with pytest.raises(StoredDataError, match="reference path is invalid"):
        storage.load_learning_session(first_learning.id)


def test_json_model_id_must_match_resource_filename(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    first = storage.create_learning_session("first")
    second = storage.create_learning_session("second")
    storage.learning_session_path(first.id).write_text(
        second.model_dump_json(indent=2),
        encoding="utf-8",
    )

    with pytest.raises(StoredDataError, match="id does not match"):
        storage.load_learning_session(first.id)
