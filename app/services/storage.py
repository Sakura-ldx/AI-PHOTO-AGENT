from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import Lock, RLock
from typing import ClassVar, Protocol, TypeVar
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel, ValidationError

from app.config import DEFAULT_MAX_IMAGE_PIXELS, DEFAULT_MAX_UPLOAD_BYTES, expand_path
from app.errors import (
    InvalidRequestError,
    ResourceNotFoundError,
    StorageOperationError,
    StoredDataError,
    UploadTooLargeError,
    WorkflowConflictError,
)
from app.knowledge_schemas import PhotoLearningSummary
from app.schemas import (
    MAX_LEARNING_REFERENCES,
    GeneratedAsset,
    ImageAnalysis,
    ImageFormat,
    ImageMetadata,
    LearningReference,
    LearningSession,
    PhotoManualEdit,
    PhotoSession,
)
from app.services.image_formats import (
    ImageFormatError,
    ImagePixelLimitError,
    InspectedImage,
    inspect_image,
    write_display_jpeg,
    write_oriented_jpeg,
)

RESOURCE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
LOGGER = logging.getLogger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)


class UploadFileLike(Protocol):
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


class FileStorage:
    _root_locks_guard = Lock()
    _root_locks: ClassVar[dict[str, RLock]] = {}

    def __init__(
        self,
        root: Path,
        *,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS,
    ) -> None:
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if max_image_pixels <= 0:
            raise ValueError("max_image_pixels must be positive")
        if Image.MAX_IMAGE_PIXELS is not None and max_image_pixels > Image.MAX_IMAGE_PIXELS:
            raise ValueError(
                "max_image_pixels must not exceed Pillow's decompression safety threshold "
                f"({Image.MAX_IMAGE_PIXELS})"
            )

        self.root = expand_path(root).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.max_image_pixels = max_image_pixels
        lock_key = os.path.normcase(str(self.root))
        with self._root_locks_guard:
            self._lock = self._root_locks.setdefault(lock_key, RLock())

        # Original uploads and references are intentionally outside the public
        # generated tree. Only previews live below generated_dir.
        self.uploads_dir = self.root / "uploads"
        self.references_dir = self.root / "references"
        self.generated_dir = self.root / "generated" / "previews"
        # Learning-summary previews are derived, per-photo assets. They live
        # under the public generated tree so the static mount can serve them,
        # but they are deliberately *not* recorded in PhotoSession.preview_files:
        # those entries describe user-requested previews and are replaced when
        # the analysis changes. A summary is rebuilt from scratch instead, so
        # this separate directory is cleaned on rebuild/analysis replacement.
        self.summary_previews_dir = self.root / "generated" / "summaries"
        self.exports_dir = self.root / "exports"

        self.data_dir = self.root / "data"
        self.sessions_dir = self.data_dir / "sessions"
        self.learning_sessions_dir = self.data_dir / "learning_sessions"
        # Per-photo visual signatures used to keep a shoot's styles consistent.
        # Derived, disposable data: deleting this directory only makes the next
        # analysis recompute the hashes.
        self.signatures_dir = self.data_dir / "signatures"
        # Phase 2 learning summaries are private knowledge-base records. They are
        # never served directly; the API projects them into list/detail views.
        self.knowledge_dir = self.data_dir / "knowledge" / "summaries"

        # Phase 1/early Phase 2 persisted state is readable for compatibility,
        # but every new write goes to data_dir.
        self.legacy_sessions_dir = self.root / "generated" / "sessions"
        self.legacy_learning_sessions_dir = self.root / "generated" / "learning_sessions"
        self.ensure_directories()

    def ensure_directories(self) -> None:
        for directory in (
            self.uploads_dir,
            self.references_dir,
            self.generated_dir,
            self.summary_previews_dir,
            self.exports_dir,
            self.sessions_dir,
            self.learning_sessions_dir,
            self.signatures_dir,
            self.knowledge_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    async def save_upload(
        self,
        upload: UploadFileLike,
        *,
        filename: str | None = None,
    ) -> PhotoSession:
        content = await self._read_upload(upload)
        original_filename = _sanitize_original_filename(
            filename if filename is not None else getattr(upload, "filename", None)
        )
        return await asyncio.to_thread(
            self._save_upload_content,
            content,
            upload.content_type,
            original_filename,
        )

    def _save_upload_content(
        self,
        content: bytes,
        content_type: str | None,
        original_filename: str | None = None,
    ) -> PhotoSession:
        photo_id = uuid4().hex
        photo_dir = self._resource_directory(
            self.uploads_dir,
            photo_id,
            "Photo session was not found.",
        )
        raw_path = photo_dir / ".uploading"

        try:
            photo_dir.mkdir(parents=True, exist_ok=False)
            raw_path.write_bytes(content)
            inspected = self.inspect_upload(raw_path, content_type)
            metadata = self._metadata_from(inspected, raw_path)
            original_path = photo_dir / f"original{_extension_for(metadata.format)}"
            os.replace(raw_path, original_path)

            render_file: Path | None = None
            display_file: Path | None = None
            if inspected.preview is not None:
                render_path = photo_dir / "embedded.jpg"
                render_path.write_bytes(
                    write_oriented_jpeg(
                        inspected.preview.content,
                        inspected.preview.orientation,
                    )
                )
                render_file = render_path
                display_file = render_path
            else:
                # Normalize EXIF orientation once for the upload card and the
                # before/after comparison, so browsers never need to guess.
                display_path = photo_dir / "display.jpg"
                try:
                    write_display_jpeg(original_path, display_path)
                except (OSError, SyntaxError, ValueError) as exc:
                    raise InvalidRequestError(
                        "Unable to prepare a browser display preview for the uploaded image."
                    ) from exc
                display_file = display_path

            session = PhotoSession(
                id=photo_id,
                original_filename=original_filename,
                original_file=original_path,
                render_file=render_file,
                display_file=display_file,
                metadata=metadata,
            )
            self.save_session(session)
            return session
        except OSError as exc:
            self._remove_resource_directory(photo_dir, self.uploads_dir)
            raise StorageOperationError("Unable to persist the uploaded photo.") from exc
        except Exception:
            self._remove_resource_directory(photo_dir, self.uploads_dir)
            raise

    def inspect_upload(
        self,
        image_path: Path,
        content_type: str | None,
    ) -> InspectedImage:
        del content_type  # Container format is detected from magic bytes.
        try:
            file_size = image_path.stat().st_size
        except OSError as exc:
            raise InvalidRequestError("Unable to read the uploaded image file.") from exc
        if file_size > self.max_upload_bytes:
            raise UploadTooLargeError(
                f"Uploaded file exceeds the {self.max_upload_bytes} byte limit."
            )
        try:
            return inspect_image(image_path, max_image_pixels=self.max_image_pixels)
        except ImagePixelLimitError as exc:
            raise UploadTooLargeError(
                f"Uploaded image exceeds the {self.max_image_pixels} pixel limit."
            ) from exc
        except ImageFormatError as exc:
            raise InvalidRequestError(str(exc)) from exc

    def _metadata_from(self, inspected: InspectedImage, image_path: Path) -> ImageMetadata:
        try:
            file_size = image_path.stat().st_size
        except OSError as exc:
            raise InvalidRequestError("Unable to read the uploaded image file.") from exc
        return ImageMetadata(
            width=inspected.width,
            height=inspected.height,
            format=inspected.format,
            color_mode=inspected.color_mode,
            file_size=file_size,
            content_type=inspected.content_type,
            render_source=inspected.render_source,
        )

    def extract_metadata(self, image_path: Path, content_type: str | None) -> ImageMetadata:
        inspected = self.inspect_upload(image_path, content_type)
        return self._metadata_from(inspected, image_path)

    def session_path(self, photo_id: str) -> Path:
        return self._resource_file(
            self.sessions_dir,
            photo_id,
            "Photo session was not found.",
        )

    def save_session(self, session: PhotoSession) -> None:
        with self._lock:
            self._save_model(self.session_path(session.id), session)

    def load_session(self, photo_id: str) -> PhotoSession:
        with self._lock:
            return self._load_session_unlocked(photo_id)

    def list_sessions(self) -> list[PhotoSession]:
        """Return every readable photo session, newest first.

        Used by series matching. A corrupt or foreign record is skipped with a
        warning instead of failing the current analysis: consistency is a
        best-effort enhancement, not a reason to lose a photo.
        """

        with self._lock:
            try:
                paths = sorted(self.sessions_dir.glob("*.json"))
            except OSError as exc:
                raise StorageOperationError("Unable to read stored photo sessions.") from exc
            sessions: list[PhotoSession] = []
            for path in paths:
                if RESOURCE_ID_PATTERN.fullmatch(path.stem) is None:
                    continue
                try:
                    session = self._load_model(path, PhotoSession, "photo session")
                    self._validate_stored_id(session.id, path.stem, "photo session")
                except (OSError, StoredDataError, ValidationError, ValueError) as exc:
                    LOGGER.warning(
                        "Skipping unreadable photo session %s: %s", path.name, exc
                    )
                    continue
                sessions.append(session)
        sessions.sort(key=lambda session: session.created_at, reverse=True)
        return sessions

    def save_analysis(self, session: PhotoSession, analysis: ImageAnalysis) -> PhotoSession:
        with self._lock:
            latest = self._load_session_unlocked(session.id)
            stale_preview_assets: list[GeneratedAsset] = []
            stale_preset_assets: list[GeneratedAsset] = []
            analysis_changed = latest.analysis is not None and latest.analysis != analysis
            if analysis_changed:
                stale_preview_assets = list(latest.preview_files.values())
                stale_preset_assets = list(latest.preset_files.values())
                latest.preview_files = {}
                latest.preset_files = {}
                latest.manual_edit = None
            latest.analysis = analysis
            self._save_model(self.session_path(latest.id), latest)
            session.analysis = analysis
            session.preview_files = dict(latest.preview_files)
            session.preset_files = dict(latest.preset_files)
            if analysis_changed:
                # The knowledge summary embeds previews of the previous analysis;
                # it must not outlive the styles/previews it references. A fresh
                # one is regenerated by the workflow right after this save.
                self._delete_knowledge_summary_unlocked(latest.id)
            for asset in stale_preview_assets:
                self._remove_stale_asset(asset.path, self.generated_dir)
            for asset in stale_preset_assets:
                self._remove_stale_asset(asset.path, self.exports_dir)
            return latest

    def save_preview_asset(self, photo_id: str, token: str, asset: GeneratedAsset) -> PhotoSession:
        with self._lock:
            latest = self._load_session_unlocked(photo_id)
            latest.preview_files[token] = asset
            self._save_model(self.session_path(latest.id), latest)
            return latest

    def save_preset_asset(self, photo_id: str, token: str, asset: GeneratedAsset) -> PhotoSession:
        with self._lock:
            latest = self._load_session_unlocked(photo_id)
            latest.preset_files[token] = asset
            self._save_model(self.session_path(latest.id), latest)
            return latest

    def save_manual_edit(self, photo_id: str, edit: PhotoManualEdit) -> PhotoSession:
        """Record the style/intensity the user explicitly picked.

        Stored on the photo session itself (not in the derived knowledge
        summary) so rebuilding the summary never loses the user's choice.
        """

        with self._lock:
            latest = self._load_session_unlocked(photo_id)
            latest.manual_edit = edit
            self._save_model(self.session_path(latest.id), latest)
            return latest

    def knowledge_summary_path(self, photo_id: str) -> Path:
        return self._resource_file(
            self.knowledge_dir,
            photo_id,
            "Learning summary was not found.",
        )

    def save_knowledge_summary(self, summary: PhotoLearningSummary) -> None:
        with self._lock:
            if summary.id != summary.photo_id:
                raise StoredDataError(
                    "Stored learning summary id does not match its photo id."
                )
            self._save_model(self.knowledge_summary_path(summary.photo_id), summary)

    def load_knowledge_summary(self, photo_id: str) -> PhotoLearningSummary:
        with self._lock:
            path = self.knowledge_summary_path(photo_id)
            if not path.exists():
                raise ResourceNotFoundError("Learning summary was not found.")
            summary = self._load_model(path, PhotoLearningSummary, "learning summary")
            self._validate_stored_id(summary.id, photo_id, "learning summary")
            return summary

    def list_knowledge_summaries(self) -> list[PhotoLearningSummary]:
        with self._lock:
            try:
                paths = sorted(self.knowledge_dir.glob("*.json"))
            except OSError as exc:
                raise StorageOperationError(
                    "Unable to read stored learning summaries."
                ) from exc
            summaries: list[PhotoLearningSummary] = []
            for path in paths:
                # Ignore foreign files; only resource-keyed records belong to the
                # knowledge base. A malformed record with a valid key is a hard
                # data error so it is never silently dropped.
                if RESOURCE_ID_PATTERN.fullmatch(path.stem) is None:
                    continue
                summary = self._load_model(path, PhotoLearningSummary, "learning summary")
                self._validate_stored_id(summary.id, path.stem, "learning summary")
                summaries.append(summary)
            return summaries

    def ensure_knowledge_previews(self, photo_id: str) -> Path:
        """Return the per-photo directory for learning-summary previews.

        This is intentionally non-destructive: the previous summary and its
        previews must stay readable if a rebuild fails before the new summary is
        committed.  ``prune_knowledge_previews`` removes orphaned files only
        after the new summary has been saved.
        """

        if RESOURCE_ID_PATTERN.fullmatch(photo_id) is None:
            raise InvalidRequestError("Invalid photo id.")
        with self._lock:
            directory = self.summary_previews_dir / photo_id
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageOperationError(
                    "Unable to prepare learning summary preview storage."
                ) from exc
            return directory

    def prune_knowledge_previews(
        self,
        photo_id: str,
        keep_filenames: Sequence[str],
    ) -> None:
        """Delete preview files no longer referenced by the saved summary.

        Pruning is best-effort derived-data maintenance.  A failure here must not
        turn a successful summary rebuild into an error.
        """

        if RESOURCE_ID_PATTERN.fullmatch(photo_id) is None:
            raise InvalidRequestError("Invalid photo id.")
        keep = set(keep_filenames)
        with self._lock:
            directory = self.summary_previews_dir / photo_id
            try:
                entries = list(directory.iterdir())
            except FileNotFoundError:
                return
            except OSError as exc:
                LOGGER.warning(
                    "Unable to inspect summary previews for photo_id=%s: %s",
                    photo_id,
                    exc,
                )
                return
            for path in entries:
                if path.name in keep or not path.is_file():
                    continue
                try:
                    path.unlink()
                except OSError as exc:
                    LOGGER.warning(
                        "Unable to prune stale summary preview %s: %s",
                        path,
                        exc,
                    )

    def delete_knowledge_summary(self, photo_id: str) -> None:
        with self._lock:
            self._delete_knowledge_summary_unlocked(photo_id)

    def _delete_knowledge_summary_unlocked(self, photo_id: str) -> None:
        try:
            path = self._resource_file(
                self.knowledge_dir,
                photo_id,
                "Learning summary was not found.",
            )
        except ResourceNotFoundError:
            path = None
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # Derived data: failing to remove a stale summary must never make
                # analysis persistence fail. The next rebuild overwrites it.
                pass
        if RESOURCE_ID_PATTERN.fullmatch(photo_id) is not None:
            shutil.rmtree(self.summary_previews_dir / photo_id, ignore_errors=True)

    @contextmanager
    def asset_transaction(self) -> Iterator[None]:
        """Serialize file publication and session metadata within this process/root."""

        with self._lock:
            yield

    def learning_session_path(self, session_id: str) -> Path:
        return self._resource_file(
            self.learning_sessions_dir,
            session_id,
            "Learning session was not found.",
        )

    def create_learning_session(self, topic: str) -> LearningSession:
        session = LearningSession(id=uuid4().hex, topic=topic)
        self.save_learning_session(session)
        return session

    def save_learning_session(self, session: LearningSession) -> None:
        with self._lock:
            self._save_model(self.learning_session_path(session.id), session)

    def load_learning_session(self, session_id: str) -> LearningSession:
        with self._lock:
            return self._load_learning_session_unlocked(session_id)

    async def save_learning_reference_upload(
        self,
        session: LearningSession,
        upload: UploadFileLike,
        source_url: str | None = None,
        author: str | None = None,
        license: str | None = None,
    ) -> LearningReference:
        # Read and validate outside the lock. The commit below reloads the
        # latest aggregate while holding the lock, so concurrent uploads cannot
        # overwrite one another with stale session objects.
        self._validate_resource_id(session.id, "Learning session was not found.")
        content = await self._read_upload(upload)
        return await asyncio.to_thread(
            self._save_learning_reference_content,
            session,
            content,
            upload.content_type,
            source_url,
            author,
            license,
        )

    def _save_learning_reference_content(
        self,
        session: LearningSession,
        content: bytes,
        content_type: str | None,
        source_url: str | None,
        author: str | None,
        license: str | None,
    ) -> LearningReference:
        reference_id = uuid4().hex
        session_reference_dir = self._resource_directory(
            self.references_dir,
            session.id,
            "Learning session was not found.",
        )
        reference_dir = self._resource_directory(
            session_reference_dir,
            reference_id,
            "Learning reference was not found.",
        )
        raw_path = reference_dir / ".uploading"

        try:
            reference_dir.mkdir(parents=True, exist_ok=False)
            raw_path.write_bytes(content)
            metadata = self.extract_metadata(raw_path, content_type)
            if metadata.format not in {"JPEG", "PNG"}:
                raise InvalidRequestError(
                    "Learning references support JPEG and PNG images."
                )
            extension = _extension_for(metadata.format)
            image_path = reference_dir / f"reference{extension}"
            os.replace(raw_path, image_path)

            reference = LearningReference(
                id=reference_id,
                source_url=_blank_to_none(source_url),
                author=_blank_to_none(author),
                license=_blank_to_none(license),
                image_file=image_path,
                image_url=self.reference_url(image_path),
                metadata=metadata,
            )

            with self._lock:
                latest = self._load_learning_session_unlocked(session.id)
                if len(latest.references) >= MAX_LEARNING_REFERENCES:
                    raise WorkflowConflictError(
                        "A learning session supports at most "
                        f"{MAX_LEARNING_REFERENCES} references."
                    )
                latest.references.append(reference)
                latest.summary = None
                self._save_model(self.learning_session_path(latest.id), latest)

                # Keep the caller's in-memory object coherent for direct service
                # users without using it as the persisted source of truth.
                session.references = list(latest.references)
                session.summary = None
            return reference
        except OSError as exc:
            self._remove_resource_directory(reference_dir, self.references_dir)
            self._remove_empty_directory(session_reference_dir, self.references_dir)
            raise StorageOperationError("Unable to persist the learning reference.") from exc
        except Exception:
            self._remove_resource_directory(reference_dir, self.references_dir)
            self._remove_empty_directory(session_reference_dir, self.references_dir)
            raise

    def save_learning_analysis(
        self,
        analyzed: LearningSession,
        *,
        expected_session_id: str,
        expected_reference_ids: Sequence[str],
    ) -> LearningSession:
        expected = tuple(expected_reference_ids)
        with self._lock:
            self._validate_resource_id(
                expected_session_id,
                "Learning session was not found.",
            )
            if analyzed.id != expected_session_id:
                raise StoredDataError(
                    "Analyzed learning session id does not match the requested resource."
                )
            latest = self._load_learning_session_unlocked(expected_session_id)
            actual = tuple(reference.id for reference in latest.references)
            analyzed_ids = tuple(reference.id for reference in analyzed.references)
            if analyzed_ids != expected:
                raise StoredDataError(
                    "Analyzed learning references do not match the requested snapshot."
                )
            if actual != expected:
                raise WorkflowConflictError(
                    "Learning references changed while analysis was running; "
                    "analyze the session again."
                )

            analyzed_by_id = {reference.id: reference for reference in analyzed.references}
            updated_references = [
                reference.model_copy(
                    update={"analysis": analyzed_by_id[reference.id].analysis},
                    deep=True,
                )
                for reference in latest.references
            ]
            updated = latest.model_copy(
                update={
                    "references": updated_references,
                    "summary": analyzed.summary,
                },
                deep=True,
            )
            self._save_model(self.learning_session_path(updated.id), updated)
            return updated

    def generated_url(self, path: Path) -> str:
        return f"/generated/{self._relative_asset_path(path, self.generated_dir)}"

    def summary_preview_url(self, path: Path) -> str:
        """Public URL for a learning-summary preview (below generated/)."""

        return f"/generated/{self._relative_asset_path(path, self.root / 'generated')}"

    def export_url(self, path: Path) -> str:
        return f"/exports/{self._relative_asset_path(path, self.exports_dir)}"

    def reference_url(self, path: Path) -> str:
        relative = self._relative_asset_path(path, self.references_dir)
        parts = relative.split("/")
        if len(parts) != 3:
            raise InvalidRequestError("Learning reference path has an invalid layout.")
        session_id, reference_id, _filename = parts
        self._validate_resource_id(session_id, "Learning session was not found.")
        self._validate_resource_id(reference_id, "Learning reference was not found.")
        return f"/api/learning/sessions/{session_id}/references/{reference_id}/image"

    async def _read_upload(self, upload: UploadFileLike) -> bytes:
        try:
            chunks: list[bytes] = []
            remaining = self.max_upload_bytes + 1
            while remaining > 0:
                chunk = await upload.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
        except (OSError, ValueError) as exc:
            raise InvalidRequestError("Unable to read the uploaded file.") from exc
        if not content:
            raise InvalidRequestError("Uploaded file is empty.")
        if len(content) > self.max_upload_bytes:
            raise UploadTooLargeError(
                f"Uploaded file exceeds the {self.max_upload_bytes} byte limit."
            )
        return content

    def _load_session_unlocked(self, photo_id: str) -> PhotoSession:
        current_path = self.session_path(photo_id)
        if current_path.exists():
            session = self._load_model(current_path, PhotoSession, "photo session")
            self._validate_stored_id(session.id, photo_id, "photo session")
            return self._validate_photo_session_files(session)
        legacy_path = self._resource_file(
            self.legacy_sessions_dir,
            photo_id,
            "Photo session was not found.",
        )
        if legacy_path.exists():
            session = self._load_model(legacy_path, PhotoSession, "legacy photo session")
            self._validate_stored_id(session.id, photo_id, "legacy photo session")
            return self._validate_photo_session_files(session)
        raise ResourceNotFoundError("Photo session was not found.")

    def _validate_photo_session_files(self, session: PhotoSession) -> PhotoSession:
        photo_dir = self._resource_directory(
            self.uploads_dir,
            session.id,
            "Photo session was not found.",
        )
        original_path = session.original_file.resolve()
        expected_path = (
            photo_dir / f"original{_extension_for(session.metadata.format)}"
        ).resolve()
        if original_path != expected_path:
            raise StoredDataError(
                f"Stored photo original path is invalid for session {session.id}."
            )
        if not original_path.is_file():
            raise StoredDataError(f"Stored photo original is missing for session {session.id}.")
        session.original_file = original_path

        if session.display_file is not None:
            display_path = session.display_file.resolve()
            expected_display_name = (
                "embedded.jpg" if session.metadata.render_source == "embedded_preview" else "display.jpg"
            )
            expected_display_path = (photo_dir / expected_display_name).resolve()
            if display_path != expected_display_path:
                raise StoredDataError(
                    f"Stored photo display path is invalid for session {session.id}."
                )
            if not display_path.is_file():
                raise StoredDataError(
                    f"Stored photo display source is missing for session {session.id}."
                )
            session.display_file = display_path

        if session.render_file is not None:
            render_path = session.render_file.resolve()
            expected_render_path = (photo_dir / "embedded.jpg").resolve()
            if render_path != expected_render_path:
                raise StoredDataError(
                    f"Stored photo render path is invalid for session {session.id}."
                )
            if not render_path.is_file():
                raise StoredDataError(
                    f"Stored photo render source is missing for session {session.id}."
                )
            session.render_file = render_path
        elif session.metadata.render_source == "embedded_preview":
            raise StoredDataError(
                f"Stored photo render source is missing for session {session.id}."
            )
        return session

    def _load_learning_session_unlocked(self, session_id: str) -> LearningSession:
        current_path = self.learning_session_path(session_id)
        if current_path.exists():
            session = self._load_model(current_path, LearningSession, "learning session")
            self._validate_stored_id(session.id, session_id, "learning session")
            return self._normalize_reference_urls(session)
        legacy_path = self._resource_file(
            self.legacy_learning_sessions_dir,
            session_id,
            "Learning session was not found.",
        )
        if legacy_path.exists():
            session = self._load_model(legacy_path, LearningSession, "legacy learning session")
            self._validate_stored_id(session.id, session_id, "legacy learning session")
            return self._normalize_reference_urls(session)
        raise ResourceNotFoundError("Learning session was not found.")

    def _normalize_reference_urls(self, session: LearningSession) -> LearningSession:
        for reference in session.references:
            if reference.metadata.format not in {"JPEG", "PNG"}:
                raise StoredDataError(
                    f"Stored learning reference format is unsupported for session {session.id}."
                )
            extension = _extension_for(reference.metadata.format)
            expected_path = (
                self.references_dir.resolve()
                / session.id
                / reference.id
                / f"reference{extension}"
            )
            image_path = reference.image_file.resolve()
            if image_path != expected_path or not image_path.is_file():
                raise StoredDataError(
                    f"Stored learning reference path is invalid for session {session.id}."
                )
            reference.image_file = image_path
            reference.image_url = (
                f"/api/learning/sessions/{session.id}/references/{reference.id}/image"
            )
        return session

    @staticmethod
    def _remove_stale_asset(path: Path, allowed_root: Path) -> None:
        try:
            resolved_path = path.resolve()
            resolved_root = allowed_root.resolve()
            if not resolved_path.is_relative_to(resolved_root):
                return
            resolved_path.unlink(missing_ok=True)
            parent = resolved_path.parent
            while parent != resolved_root and parent.is_relative_to(resolved_root):
                parent.rmdir()
                parent = parent.parent
        except OSError:
            # State no longer exposes the stale asset. A best-effort cleanup
            # failure may leave an orphan for a later maintenance sweep.
            return

    @staticmethod
    def _validate_stored_id(actual: str, expected: str, label: str) -> None:
        if actual != expected:
            raise StoredDataError(f"Stored {label} id does not match its resource key.")

    def _save_model(self, path: Path, model: BaseModel) -> None:
        self._atomic_write_text(path, model.model_dump_json(indent=2))

    def _load_model(self, path: Path, model_type: type[ModelT], label: str) -> ModelT:
        try:
            content = path.read_text(encoding="utf-8")
            return model_type.model_validate_json(content)
        except (OSError, UnicodeError, ValidationError) as exc:
            raise StoredDataError(f"Stored {label} is invalid: {path.name}") from exc

    def _atomic_write_text(self, path: Path, content: str) -> None:
        temp_path: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            with temp_path.open("w", encoding="utf-8", newline="\n") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_path, path)
        except OSError as exc:
            raise StorageOperationError("Unable to atomically persist application state.") from exc
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _resource_file(self, base: Path, resource_id: str, not_found_detail: str) -> Path:
        self._validate_resource_id(resource_id, not_found_detail)
        return base / f"{resource_id}.json"

    def _resource_directory(self, base: Path, resource_id: str, not_found_detail: str) -> Path:
        self._validate_resource_id(resource_id, not_found_detail)
        return base / resource_id

    @staticmethod
    def _validate_resource_id(resource_id: str, not_found_detail: str) -> None:
        if RESOURCE_ID_PATTERN.fullmatch(resource_id) is None:
            raise ResourceNotFoundError(not_found_detail)

    @staticmethod
    def _relative_asset_path(path: Path, base: Path) -> str:
        resolved_base = base.resolve()
        resolved_path = path.resolve()
        if not resolved_path.is_relative_to(resolved_base):
            raise InvalidRequestError("Asset path is outside its configured storage directory.")
        return resolved_path.relative_to(resolved_base).as_posix()

    @staticmethod
    def _remove_resource_directory(path: Path, base: Path) -> None:
        resolved_base = base.resolve()
        resolved_path = path.resolve()
        if resolved_path != resolved_base and resolved_path.is_relative_to(resolved_base):
            shutil.rmtree(resolved_path, ignore_errors=True)

    @staticmethod
    def _remove_empty_directory(path: Path, base: Path) -> None:
        resolved_base = base.resolve()
        resolved_path = path.resolve()
        if resolved_path == resolved_base or not resolved_path.is_relative_to(resolved_base):
            return
        try:
            resolved_path.rmdir()
        except OSError:
            pass


def find_style(session: PhotoSession, style_id: str):
    if session.analysis is None:
        raise WorkflowConflictError(
            "Analyze the photo before generating previews or exports."
        )
    for style in session.analysis.recommended_styles:
        if style.id == style_id:
            return style
    raise ResourceNotFoundError("Editing style was not found.")


def _extension_for(image_format: ImageFormat) -> str:
    return {
        "JPEG": ".jpg",
        "PNG": ".png",
        "TIFF": ".tif",
        "CR3": ".cr3",
    }[image_format]


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


MAX_ORIGINAL_FILENAME_LENGTH = 255


def _sanitize_original_filename(value: str | None) -> str | None:
    """Reduce an upload's client-supplied filename to a safe display basename.

    Only the last path component is kept (both separator styles, since browsers
    on Windows may send ``C:\\fakepath\\name.jpg``), control characters are
    dropped, and the result is length-capped. Never returns a path and never
    raises.
    """

    if not isinstance(value, str):
        return None
    candidate = value.replace("\\", "/").rsplit("/", 1)[-1]
    candidate = "".join(
        character for character in candidate if character >= " " and character != "\x7f"
    ).strip()
    if candidate in ("", ".", ".."):
        return None
    return candidate[:MAX_ORIGINAL_FILENAME_LENGTH].strip() or None
