from __future__ import annotations

from typing import Annotated, Any, get_args

from fastapi import APIRouter, Body, Depends, File, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import get_services
from app.container import ServiceContainer
from app.errors import InvalidRequestError, ResourceNotFoundError
from app.schemas import (
    AnalysisTaskCreated,
    AnalysisTaskStatus,
    DevelopResponse,
    ExportRequest,
    ExportResponse,
    ErrorResponse,
    ImageAnalysis,
    LightroomSendRequest,
    LightroomSendResponse,
    PhotoAnalysisProviderSelection,
    PhotoAnalyzeRequest,
    PhotoUploadResponse,
    PreviewRequest,
    PreviewResponse,
)
from app.services.analysis_tasks import submit_analysis


router = APIRouter(prefix="/api/photos", tags=["photos"])
Services = Annotated[ServiceContainer, Depends(get_services)]
ERROR_RESPONSE = {"model": ErrorResponse}
VALID_PROVIDERS = frozenset(get_args(PhotoAnalysisProviderSelection))


def _provider_from_payload(payload: Any) -> str:
    """Read ``provider`` out of whatever JSON shape the client managed to send."""

    provider = payload.get("provider") if isinstance(payload, dict) else None
    if isinstance(provider, str) and provider.strip():
        value = provider.strip()
        if value not in VALID_PROVIDERS:
            # Validate before a task/thread is created: an unknown provider used
            # to surface as a 500 from the background thread.
            raise InvalidRequestError("Unsupported analysis provider.")
        return value
    return "default"


@router.post(
    "",
    response_model=PhotoUploadResponse,
    responses={400: ERROR_RESPONSE, 413: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
async def upload_photo(
    file: Annotated[UploadFile, File(...)],
    services: Services,
) -> PhotoUploadResponse:
    session = await services.photos.upload(file, filename=file.filename)
    return PhotoUploadResponse(
        photo_id=session.id,
        metadata=session.metadata,
        display_url=f"/api/photos/{session.id}/display",
    )


@router.get(
    "/{photo_id}/display",
    response_class=FileResponse,
    responses={404: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def display_photo_source(photo_id: str, services: Services) -> FileResponse:
    session = services.storage.load_session(photo_id)
    display_path = session.display_file or session.original_file
    media_type = (
        "image/jpeg"
        if display_path.suffix.lower() in {".jpg", ".jpeg"}
        else session.metadata.content_type
    )
    return FileResponse(
        display_path,
        media_type=media_type,
        filename=display_path.name,
        content_disposition_type="inline",
    )


@router.post(
    "/{photo_id}/analyze",
    response_model=ImageAnalysis,
    responses={
        404: ERROR_RESPONSE,
        500: ERROR_RESPONSE,
        502: ERROR_RESPONSE,
        504: ERROR_RESPONSE,
    },
)
def analyze_photo(
    photo_id: str,
    services: Services,
    request: PhotoAnalyzeRequest | None = None,
) -> ImageAnalysis:
    provider = request.provider if request is not None else "default"
    return services.photos.analyze(photo_id, provider)


@router.post(
    "/{photo_id}/analyze/jobs",
    response_model=AnalysisTaskCreated,
    status_code=201,
    responses={400: ERROR_RESPONSE, 404: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def create_analysis_job(
    photo_id: str,
    services: Services,
    payload: Annotated[Any, Body()] = None,
) -> AnalysisTaskCreated:
    """Queue the analysis and return immediately so clients can poll progress.

    The body is intentionally lenient: Lightroom's Lua JSON encoder turns an
    empty table into ``[]``, so a strict ``PhotoAnalyzeRequest`` model rejects
    perfectly harmless requests with 422 and forces the plug-in onto the
    blocking synchronous path (no progress). Only ``provider`` matters here.
    """

    session = services.storage.load_session(photo_id)
    provider = _provider_from_payload(payload)
    task = submit_analysis(
        services.analysis_tasks,
        services.photos,
        session.id,
        provider,
    )
    return AnalysisTaskCreated(task_id=task.task_id, photo_id=session.id)


@router.get(
    "/{photo_id}/analyze/jobs/{task_id}",
    response_model=AnalysisTaskStatus,
    responses={404: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def read_analysis_job(
    photo_id: str,
    task_id: str,
    services: Services,
) -> AnalysisTaskStatus:
    task = services.analysis_tasks.get(task_id)
    if task is None or task.photo_id != photo_id:
        raise ResourceNotFoundError("Analysis task was not found.")
    status = services.analysis_tasks.status(task_id)
    if status is None:
        raise ResourceNotFoundError("Analysis task was not found.")
    return status


@router.post(
    "/{photo_id}/analyze/jobs/{task_id}/cancel",
    response_model=AnalysisTaskStatus,
    responses={404: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def cancel_analysis_job(
    photo_id: str,
    task_id: str,
    services: Services,
) -> AnalysisTaskStatus:
    task = services.analysis_tasks.get(task_id)
    if task is None or task.photo_id != photo_id:
        raise ResourceNotFoundError("Analysis task was not found.")
    status = services.analysis_tasks.cancel(task_id)
    if status is None:
        # The task can be reaped between the lookup and the cancellation.
        raise ResourceNotFoundError("Analysis task was not found.")
    return status


@router.post(
    "/{photo_id}/preview",
    response_model=PreviewResponse,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def preview_photo(photo_id: str, request: PreviewRequest, services: Services) -> PreviewResponse:
    return services.photos.preview(photo_id, request)


@router.post(
    "/{photo_id}/export",
    response_model=ExportResponse,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def export_preset(photo_id: str, request: ExportRequest, services: Services) -> ExportResponse:
    return services.photos.export(photo_id, request)


@router.post(
    "/{photo_id}/develop",
    response_model=DevelopResponse,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def develop_settings(photo_id: str, request: ExportRequest, services: Services) -> DevelopResponse:
    return services.photos.develop(photo_id, request)


@router.post(
    "/{photo_id}/lightroom",
    response_model=LightroomSendResponse,
    responses={404: ERROR_RESPONSE, 409: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def send_to_lightroom(
    photo_id: str,
    request: LightroomSendRequest,
    services: Services,
) -> LightroomSendResponse:
    development = services.photos.develop(photo_id, request)
    return services.lightroom.send(
        development,
        install_plugin=request.install_plugin,
    )
