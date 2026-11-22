from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.api.dependencies import get_services
from app.container import ServiceContainer
from app.schemas import (
    ErrorResponse,
    LearningReferenceResponse,
    LearningSessionCreateRequest,
    LearningSessionResponse,
)

router = APIRouter(prefix="/api/learning/sessions", tags=["learning"])
Services = Annotated[ServiceContainer, Depends(get_services)]
ERROR_RESPONSE = {"model": ErrorResponse}


@router.post(
    "",
    response_model=LearningSessionResponse,
    responses={500: ERROR_RESPONSE},
)
def create_learning_session(
    request: LearningSessionCreateRequest,
    services: Services,
) -> LearningSessionResponse:
    return LearningSessionResponse.model_validate(services.learning.create_session(request.topic))


@router.get(
    "/{session_id}",
    response_model=LearningSessionResponse,
    responses={404: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def get_learning_session(session_id: str, services: Services) -> LearningSessionResponse:
    return LearningSessionResponse.model_validate(services.learning.get_session(session_id))


@router.get(
    "/{session_id}/references/{reference_id}/image",
    response_class=FileResponse,
    responses={404: ERROR_RESPONSE, 500: ERROR_RESPONSE},
)
def get_learning_reference_image(
    session_id: str,
    reference_id: str,
    services: Services,
) -> FileResponse:
    reference = services.learning.get_reference(session_id, reference_id)
    return FileResponse(
        reference.image_file,
        media_type=reference.metadata.content_type,
        filename=reference.image_file.name,
        content_disposition_type="inline",
    )


@router.post(
    "/{session_id}/references",
    response_model=LearningReferenceResponse,
    responses={
        400: ERROR_RESPONSE,
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        413: ERROR_RESPONSE,
        500: ERROR_RESPONSE,
    },
)
async def upload_learning_reference(
    session_id: str,
    services: Services,
    file: Annotated[UploadFile, File(...)],
    source_url: Annotated[str | None, Form(max_length=2048)] = None,
    author: Annotated[str | None, Form(max_length=200)] = None,
    license: Annotated[str | None, Form(max_length=200)] = None,
) -> LearningReferenceResponse:
    return LearningReferenceResponse.model_validate(
        await services.learning.add_reference(
            session_id=session_id,
            upload=file,
            source_url=source_url,
            author=author,
            license_name=license,
        )
    )


@router.post(
    "/{session_id}/analyze",
    response_model=LearningSessionResponse,
    responses={
        404: ERROR_RESPONSE,
        409: ERROR_RESPONSE,
        500: ERROR_RESPONSE,
        502: ERROR_RESPONSE,
        504: ERROR_RESPONSE,
    },
)
def analyze_learning_session(session_id: str, services: Services) -> LearningSessionResponse:
    return LearningSessionResponse.model_validate(services.learning.analyze(session_id))
