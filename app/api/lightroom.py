from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_services
from app.container import ServiceContainer
from app.schemas import (
    ErrorResponse,
    LightroomPluginInstallResponse,
    LightroomStatusResponse,
)

router = APIRouter(prefix="/api/lightroom", tags=["lightroom"])
Services = Annotated[ServiceContainer, Depends(get_services)]
ERROR_RESPONSE = {"model": ErrorResponse}


@router.get(
    "/status",
    response_model=LightroomStatusResponse,
    responses={500: ERROR_RESPONSE},
)
def get_lightroom_status(services: Services) -> LightroomStatusResponse:
    return services.lightroom.status()


@router.post(
    "/install-plugin",
    response_model=LightroomPluginInstallResponse,
    responses={500: ERROR_RESPONSE},
)
def install_lightroom_plugin(services: Services) -> LightroomPluginInstallResponse:
    return services.lightroom.install_plugin()
