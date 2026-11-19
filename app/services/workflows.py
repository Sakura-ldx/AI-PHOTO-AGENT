from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel

from app.adapters.analysis import AnalysisProvider, MockAnalysisProvider
from app.adapters.codex_analysis import CodexAnalysisProvider
from app.adapters.deepseek_analysis import DeepSeekAnalysisProvider
from app.adapters.learning import LearningAnalysisProvider
from app.adapters.openai_analysis import OpenAIAnalysisProvider
from app.config import AppSettings
from app.errors import (
    AnalysisCancelled,
    InvalidRequestError,
    ProviderOutputError,
    ProviderServiceError,
    ProviderTimeoutError,
    ResourceNotFoundError,
    StorageOperationError,
    StoredDataError,
    WorkflowConflictError,
)
from app.knowledge_schemas import (
    EditingDirectionSummary,
    KnowledgeAnalysisSnapshot,
    KnowledgeImage,
    PhotoLearningSummary,
)
from app.schemas import (
    DevelopResponse,
    EditingStyle,
    ExportRequest,
    ExportResponse,
    GeneratedAsset,
    ImageAnalysis,
    LearningAnalysisResult,
    LearningReference,
    LearningSession,
    MAX_LEARNING_REFERENCES,
    MAPPING_VERSION,
    PhotoAnalysisProviderSelection,
    PhotoSession,
    PreviewRequest,
    PreviewResponse,
)
from app.services.edit_resolver import (
    Resolution,
    make_render_observer,
    resolve_parameters,
)
from app.services.learning_summary import (
    SummaryClassification,
    classify_analysis,
    recommend_intensity,
)
from app.services.lightroom_develop import build_lightroom_develop_settings
from app.services.measurements import ImageMeasurements, measure_file
from app.services.parameters_text import describe_parameters
from app.services.preview import PREVIEW_RENDERER_VERSION, render_preview
from app.services.storage import FileStorage, UploadFileLike, find_style
from app.services.xmp import write_xmp_preset


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PhotoWorkflow:
    storage: FileStorage
    analysis_provider: AnalysisProvider
    settings: AppSettings | None = None

    async def upload(
        self,
        upload: UploadFileLike,
        *,
        filename: str | None = None,
    ) -> PhotoSession:
        return await self.storage.save_upload(upload, filename=filename)

    def analyze(
        self,
        photo_id: str,
        provider_name: PhotoAnalysisProviderSelection = "default",
    ) -> ImageAnalysis:
        return self.analyze_with_progress(photo_id, provider_name)

    def analyze_with_progress(
        self,
        photo_id: str,
        provider_name: PhotoAnalysisProviderSelection = "default",
        *,
        progress: Callable[[str, str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ImageAnalysis:
        """Analyze a photo, reporting stages so a client can show progress.

        ``progress`` receives ``(stage, hint)`` pairs. Reporting failures never
        affect the analysis itself: the callback is best-effort telemetry.

        ``cancelled`` is polled between stages. Once it turns true the analysis
        stops as soon as the current step returns, and (most importantly) the
        result is never written back to the session: a cancelled run must not
        overwrite the styles the user just dismissed.
        """

        def report(stage: str, hint: str) -> None:
            if progress is None:
                return
            try:
                progress(stage, hint)
            except Exception:  # noqa: BLE001 - progress must never break analysis
                return

        def check_cancelled() -> None:
            if cancelled is not None and cancelled():
                raise AnalysisCancelled("Photo analysis was cancelled.")

        report("measuring", "正在读取照片与量化统计")
        check_cancelled()
        session = self.storage.load_session(photo_id)
        source = _render_source(session)
        report("measuring", "正在量化影调与白平衡")
        provider = self._analysis_provider_for(provider_name)
        check_cancelled()
        report("provider", "正在调用模型分析照片")
        try:
            provider_result = provider.analyze(
                source,
                session.metadata,
            )
        except (ProviderTimeoutError, ProviderServiceError, ProviderOutputError):
            raise
        except Exception as exc:
            raise ProviderOutputError(
                "Photo analysis provider execution or validation failed."
            ) from exc
        check_cancelled()
        report("validating", "正在校验并保存修图建议")
        try:
            payload = (
                provider_result.model_dump(mode="python", round_trip=True)
                if isinstance(provider_result, BaseModel)
                else provider_result
            )
            analysis = ImageAnalysis.model_validate(payload)
        except (ProviderTimeoutError, ProviderServiceError, ProviderOutputError):
            raise
        except Exception as exc:
            raise ProviderOutputError(
                "Photo analysis provider execution or validation failed."
            ) from exc
        check_cancelled()
        self.storage.save_analysis(session, analysis)
        report("validating", "正在生成学习总结与推荐强度预览")
        try:
            check_cancelled()
            self.build_learning_summary(photo_id)
        except AnalysisCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the summary is best-effort
            # A learning summary must never take down the analysis result the
            # user is waiting for; it can be rebuilt later on demand.
            LOGGER.warning(
                "Learning summary generation failed for photo_id=%s: %s",
                photo_id,
                exc,
                exc_info=LOGGER.isEnabledFor(logging.DEBUG),
            )
        return analysis

    def _analysis_provider_for(
        self,
        provider_name: PhotoAnalysisProviderSelection,
    ) -> AnalysisProvider:
        if provider_name == "default":
            return self.analysis_provider
        if self.settings is None:
            if provider_name == "mock":
                return MockAnalysisProvider()
            raise InvalidRequestError(
                "Provider selection requires application settings."
            )
        if provider_name == self.settings.analysis_provider:
            return self.analysis_provider
        if provider_name == "mock":
            return MockAnalysisProvider()
        if provider_name == "openai":
            if self.settings.openai_api_key is None:
                raise InvalidRequestError(
                    "OPENAI_API_KEY is required to use the OpenAI provider."
                )
            return OpenAIAnalysisProvider(
                api_key=self.settings.openai_api_key,
                model=self.settings.openai_model,
                timeout_seconds=self.settings.openai_timeout_seconds,
                max_retries=self.settings.openai_max_retries,
            )
        if provider_name == "deepseek":
            if self.settings.deepseek_api_key is None:
                raise InvalidRequestError(
                    "DEEPSEEK_API_KEY is required to use the DeepSeek provider."
                )
            return DeepSeekAnalysisProvider(
                api_key=self.settings.deepseek_api_key,
                model=self.settings.deepseek_model,
                base_url=self.settings.deepseek_base_url,
                timeout_seconds=self.settings.deepseek_timeout_seconds,
                max_retries=self.settings.deepseek_max_retries,
            )
        if provider_name == "codex":
            return CodexAnalysisProvider(
                data_root=self.settings.data_root,
                executable=self.settings.codex_executable,
                model=self.settings.codex_model,
                timeout_seconds=self.settings.codex_timeout_seconds,
            )
        raise InvalidRequestError("Unsupported photo analysis provider.")

    def preview(self, photo_id: str, request: PreviewRequest) -> PreviewResponse:
        session = self.storage.load_session(photo_id)
        style = find_style(session, request.style_id)
        resolution = self._resolve_parameters(session, style, request.intensity)
        parameters = resolution.parameters
        strength = resolution.report.strength if resolution.report else 1.0
        token = _asset_token(
            style.id,
            PREVIEW_RENDERER_VERSION,
            parameters.model_dump_json(),
        )
        preview_path = self.storage.generated_dir / photo_id / f"{token}.jpg"
        with self.storage.asset_transaction():
            existed_before = preview_path.exists()
            try:
                render_preview(_render_source(session), preview_path, parameters)
                asset = GeneratedAsset(
                    path=preview_path,
                    url=self.storage.generated_url(preview_path),
                )
                self.storage.save_preview_asset(photo_id, token, asset)
            except OSError as exc:
                self._discard_uncommitted_asset(
                    photo_id,
                    token,
                    preview_path,
                    collection="preview_files",
                    existed_before=existed_before,
                )
                raise StorageOperationError(
                    "Unable to generate or persist the preview asset."
                ) from exc
            except Exception:
                self._discard_uncommitted_asset(
                    photo_id,
                    token,
                    preview_path,
                    collection="preview_files",
                    existed_before=existed_before,
                )
                raise
        return PreviewResponse(
            photo_id=photo_id,
            style_id=style.id,
            intensity=request.intensity,
            preview_url=asset.url,
            parameters=parameters,
            parameters_summary=describe_parameters(parameters),
            strength=strength,
            preview_is_approximate=True,
            verified=resolution.report.verified if resolution.report else True,
            warnings=list(resolution.report.warnings) if resolution.report else [],
            baseline_notes=(
                list(resolution.report.baseline.notes()) if resolution.report else []
            ),
        )

    def get_learning_summary(self, photo_id: str) -> PhotoLearningSummary:
        return self.storage.load_knowledge_summary(photo_id)

    def build_learning_summary(self, photo_id: str) -> PhotoLearningSummary:
        """Persist the Phase 2 learning summary for one analyzed photo.

        The summary captures the image itself (thumbnail + metadata), the
        analysis text, all editing directions with parameter summaries, and an
        intelligently recommended intensity. The recommendation is validated
        through the same adaptive resolver used by real previews, then rendered
        into a dedicated per-photo directory so the recorded recommendation is
        backed by an actual image without disturbing user-requested previews.
        """

        session = self.storage.load_session(photo_id)
        analysis = session.analysis
        if analysis is None:
            raise WorkflowConflictError(
                "Analyze the photo before generating a learning summary."
            )
        measurements = measure_file(_render_source(session))
        classification = classify_analysis(analysis, measurements, session.metadata)
        preview_dir = self.storage.prepare_knowledge_previews(session.id)
        directions = [
            self._build_learning_direction(
                session,
                style,
                measurements,
                preview_dir,
            )
            for style in analysis.recommended_styles
        ]
        summary = PhotoLearningSummary(
            id=session.id,
            photo_id=session.id,
            image=KnowledgeImage(
                display_url=f"/api/photos/{session.id}/display",
                width=session.metadata.width,
                height=session.metadata.height,
                format=session.metadata.format,
                color_mode=session.metadata.color_mode,
                file_size=session.metadata.file_size,
                original_filename=session.original_filename,
                render_source=session.metadata.render_source,
            ),
            analysis=KnowledgeAnalysisSnapshot.from_analysis(analysis),
            primary_category=classification.primary_category,
            tags=list(classification.tags),
            directions=directions,
            overview=_compose_learning_overview(analysis, classification, directions),
        )
        self.storage.save_knowledge_summary(summary)
        return summary

    def _build_learning_direction(
        self,
        session: PhotoSession,
        style: EditingStyle,
        measurements: ImageMeasurements,
        preview_dir: Path,
    ) -> EditingDirectionSummary:
        recommendation = recommend_intensity(measurements, style)
        recommended = recommendation.intensity
        reason = recommendation.reason
        candidates = [recommended]
        for step in (1, 2):
            candidate = round(recommended - 0.1 * step, 2)
            if candidate >= 0.3 and candidate not in candidates:
                candidates.append(candidate)

        resolution: Resolution | None = None
        chosen = recommended
        verified = False
        for index, candidate in enumerate(candidates):
            chosen = candidate
            resolution = self._resolve_parameters(session, style, candidate)
            verified = resolution.report.verified if resolution.report else True
            if verified:
                break
            if index == len(candidates) - 1:
                reason = (
                    f"{reason} 已把自动强度降到 {round(candidate * 100)}%，"
                    "仍建议手动微调。"
                )
        if resolution is None:  # defensive; candidates is never empty
            resolution = self._resolve_parameters(session, style, recommended)

        parameters = resolution.parameters
        report = resolution.report
        effective_strength = report.strength if report else chosen
        warnings = [
            _truncate_text(item, 480) for item in (report.warnings if report else [])
        ]
        baseline_notes = list(report.baseline.notes()) if report else []

        if verified and chosen != recommended:
            reason = (
                f"{reason} 为保证成片安全，预览已自动降档到 {round(chosen * 100)}%。"
            )
        if abs(effective_strength - chosen) >= 0.1:
            reason = (
                f"{reason} 实际按 {round(effective_strength * 100)}% 风格强度渲染，"
                "以保护高光和暗部。"
            )

        parameters_summary = describe_parameters(parameters)
        if baseline_notes:
            parameters_summary = (
                f"{parameters_summary}（基线校正：{'、'.join(baseline_notes)}）"
            )

        preview_url: str | None = None
        try:
            token = _asset_token(
                style.id,
                "summary",
                PREVIEW_RENDERER_VERSION,
                parameters.model_dump_json(),
            )
            preview_path = preview_dir / f"{token}.jpg"
            render_preview(_render_source(session), preview_path, parameters)
            preview_url = self.storage.summary_preview_url(preview_path)
        except Exception as exc:  # noqa: BLE001 - a summary must survive renders
            LOGGER.warning(
                "Unable to render learning summary preview for "
                "photo_id=%s style_id=%s: %s",
                session.id,
                style.id,
                exc,
                exc_info=LOGGER.isEnabledFor(logging.DEBUG),
            )
            preview_url = None
            warnings.append("推荐强度预览生成失败，可重新生成学习总结。")

        return EditingDirectionSummary(
            style_id=style.id,
            style_name=style.name,
            direction=style.description,
            best_fit_reason=style.best_fit_reason,
            expected_visual_impact=style.expected_visual_impact,
            parameters=parameters,
            parameters_summary=_truncate_text(parameters_summary, 2000),
            recommended_intensity=chosen,
            intensity_reason=_truncate_text(reason, 2000),
            effective_strength=effective_strength,
            verified=verified,
            warnings=warnings,
            preview_url=preview_url,
            preview_intensity=chosen,
        )

    def export(
        self,
        photo_id: str,
        request: ExportRequest,
        *,
        resolved: Resolution | None = None,
    ) -> ExportResponse:
        session = self.storage.load_session(photo_id)
        style = find_style(session, request.style_id)
        if resolved is None:
            resolved = self._resolve_parameters(session, style, request.intensity)
        parameters = resolved.parameters
        preset_name = f"AI Photo Agent - {style.name} - {round(request.intensity * 100)}"
        token = _asset_token(
            style.id,
            MAPPING_VERSION,
            preset_name,
            parameters.model_dump_json(),
        )
        preset_path = self.storage.exports_dir / photo_id / f"{token}.xmp"
        with self.storage.asset_transaction():
            existed_before = preset_path.exists()
            try:
                write_xmp_preset(preset_path, preset_name, parameters)
                asset = GeneratedAsset(
                    path=preset_path,
                    url=self.storage.export_url(preset_path),
                )
                self.storage.save_preset_asset(photo_id, token, asset)
            except OSError as exc:
                self._discard_uncommitted_asset(
                    photo_id,
                    token,
                    preset_path,
                    collection="preset_files",
                    existed_before=existed_before,
                )
                raise StorageOperationError(
                    "Unable to generate or persist the preset asset."
                ) from exc
            except Exception:
                self._discard_uncommitted_asset(
                    photo_id,
                    token,
                    preset_path,
                    collection="preset_files",
                    existed_before=existed_before,
                )
                raise
        return ExportResponse(
            photo_id=photo_id,
            style_id=style.id,
            intensity=request.intensity,
            preset_name=preset_name,
            mapping_version=MAPPING_VERSION,
            download_url=asset.url,
            preset_path=preset_path,
        )

    def develop(self, photo_id: str, request: ExportRequest) -> DevelopResponse:
        """Build the Lightroom SDK settings table for a style and intensity.

        The XMP asset is generated through the same export path so a web user,
        the plugin, and a manually imported preset all refer to one mapping.
        """

        session = self.storage.load_session(photo_id)
        style = find_style(session, request.style_id)
        resolution = self._resolve_parameters(session, style, request.intensity)
        parameters = resolution.parameters
        exported = self.export(photo_id, request, resolved=resolution)
        return DevelopResponse(
            photo_id=photo_id,
            style_id=style.id,
            intensity=request.intensity,
            style_name=style.name,
            original_filename=session.original_filename,
            preset_name=exported.preset_name,
            mapping_version=exported.mapping_version,
            parameters=parameters,
            parameters_summary=describe_parameters(parameters),
            strength=resolution.report.strength if resolution.report else 1.0,
            develop_settings=build_lightroom_develop_settings(parameters),
            xmp_url=exported.download_url,
            preset_path=exported.preset_path,
        )

    def _resolve_parameters(
        self,
        session: PhotoSession,
        style: EditingStyle,
        intensity: float,
    ) -> Resolution:
        """Anchor the model's look to measured statistics.

        The model proposes a style; the resolver measures the photo, applies
        deterministic baseline corrections and verifies the rendered tone. Any
        failure degrades to the plain intensity-scaled parameters so a photo can
        always be exported.
        """

        try:
            source = _render_source(session)
            measurements = measure_file(source)
            observer = make_render_observer(source) if intensity > 0.0 else None
            return resolve_parameters(
                measurements=measurements,
                style_parameters=style.parameters,
                intensity=intensity,
                observer=observer,
            )
        except Exception as exc:  # noqa: BLE001 - documented graceful degradation
            LOGGER.warning(
                "Parameter resolution failed for photo_id=%s style_id=%s; "
                "falling back to intensity-scaled parameters: %s",
                session.id,
                style.id,
                exc,
                exc_info=LOGGER.isEnabledFor(logging.DEBUG),
            )
            return Resolution(parameters=style.parameters.scaled(intensity))

    def _discard_uncommitted_asset(
        self,
        photo_id: str,
        token: str,
        path: Path,
        *,
        collection: str,
        existed_before: bool,
    ) -> None:
        if existed_before:
            return
        try:
            latest = self.storage.load_session(photo_id)
            if token in getattr(latest, collection):
                return
        except Exception:
            # If commit state cannot be established, preserve the file for
            # recovery rather than risk deleting another request's asset.
            return
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            pass


@dataclass(frozen=True, slots=True)
class LearningWorkflow:
    storage: FileStorage
    analysis_provider: LearningAnalysisProvider

    def create_session(self, topic: str) -> LearningSession:
        return self.storage.create_learning_session(topic)

    def get_session(self, session_id: str) -> LearningSession:
        return self.storage.load_learning_session(session_id)

    def get_reference(self, session_id: str, reference_id: str) -> LearningReference:
        session = self.storage.load_learning_session(session_id)
        for reference in session.references:
            if reference.id == reference_id:
                if not reference.image_file.is_file():
                    raise StoredDataError(
                        f"Stored learning reference image is missing for reference {reference.id}."
                    )
                return reference
        raise ResourceNotFoundError("Learning reference was not found.")

    async def add_reference(
        self,
        session_id: str,
        upload: UploadFileLike,
        source_url: str | None,
        author: str | None,
        license_name: str | None,
    ) -> LearningReference:
        session = self.storage.load_learning_session(session_id)
        if len(session.references) >= MAX_LEARNING_REFERENCES:
            raise WorkflowConflictError(
                "A learning session supports at most "
                f"{MAX_LEARNING_REFERENCES} references."
            )
        return await self.storage.save_learning_reference_upload(
            session=session,
            upload=upload,
            source_url=_validate_source_url(source_url),
            author=author,
            license=license_name,
        )

    def analyze(self, session_id: str) -> LearningSession:
        session = self.storage.load_learning_session(session_id)
        if not session.references:
            raise WorkflowConflictError(
                "Upload at least one reference image before analyzing a learning session."
            )
        if len(session.references) > MAX_LEARNING_REFERENCES:
            raise WorkflowConflictError(
                "Learning analysis supports at most "
                f"{MAX_LEARNING_REFERENCES} references per session."
            )
        expected_reference_ids = tuple(reference.id for reference in session.references)
        try:
            provider_result = self.analysis_provider.analyze(session)
            payload = (
                provider_result.model_dump(mode="python", round_trip=True)
                if isinstance(provider_result, BaseModel)
                else provider_result
            )
            result = LearningAnalysisResult.model_validate(payload)
        except (ProviderTimeoutError, ProviderServiceError, ProviderOutputError):
            raise
        except Exception as exc:
            raise ProviderOutputError(
                "Learning analysis provider execution or validation failed."
            ) from exc
        analyzed_reference_ids = tuple(
            reference.reference_id for reference in result.references
        )
        if analyzed_reference_ids != expected_reference_ids:
            raise ProviderOutputError(
                "Learning analysis output changed the reference identity/order."
            )
        analysis_by_id = {
            reference.reference_id: reference.analysis for reference in result.references
        }
        analyzed = session.model_copy(
            update={
                "references": [
                    reference.model_copy(
                        update={"analysis": analysis_by_id[reference.id]},
                        deep=True,
                    )
                    for reference in session.references
                ],
                "summary": result.summary,
            },
            deep=True,
        )
        return self.storage.save_learning_analysis(
            analyzed,
            expected_session_id=session.id,
            expected_reference_ids=expected_reference_ids,
        )


def _render_source(session: PhotoSession) -> Path:
    """Return the image Lightroom-agnostic renderers may decode.

    CR3 sensor data is not decodable by Pillow. The upload path stores the
    largest embedded JPEG preview as render_file and all analysis/preview work
    uses that derived source while preserving the original .cr3 untouched.
    """

    return session.render_file or session.original_file


def _compose_learning_overview(
    analysis: ImageAnalysis,
    classification: SummaryClassification,
    directions: list[EditingDirectionSummary],
) -> str:
    """Compose the human-readable Chinese overview shown in the knowledge base."""

    labels = [tag.label for tag in classification.tags[:6]]
    feature_text = "、".join(labels) if labels else "暂无显著标签"
    direction_text = "；".join(
        f"{direction.style_name} 推荐 {round(direction.recommended_intensity * 100)}%"
        for direction in directions
    )
    overview = (
        f"这张照片被归类为「{classification.primary_category}」，主要特征：{feature_text}。"
        f"分析结论：{analysis.overall_summary} "
        f"三个修图方向的智能推荐强度：{direction_text}。"
    )
    return _truncate_text(overview, 2000)


def _truncate_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _asset_token(style_id: str, *content_components: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in style_id
    )
    digest_input = "\0".join((style_id, *content_components)).encode("utf-8")
    digest = hashlib.sha256(digest_input).hexdigest()[:20]
    return f"{normalized}_{digest}"


def _validate_source_url(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = urlsplit(normalized)
        hostname = parsed.hostname
        _port = parsed.port
    except ValueError as exc:
        raise InvalidRequestError("Reference source URL must use http or https.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or any(character.isspace() for character in hostname)
    ):
        raise InvalidRequestError("Reference source URL must use http or https.")
    return normalized
