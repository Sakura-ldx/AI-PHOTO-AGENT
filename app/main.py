from __future__ import annotations

import ipaddress
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.adapters.analysis import AnalysisProvider, MockAnalysisProvider
from app.adapters.factory import build_analysis_providers
from app.adapters.learning import LearningAnalysisProvider, MockLearningAnalysisProvider
from app.api.knowledge import router as knowledge_router
from app.api.learning import router as learning_router
from app.api.lightroom import router as lightroom_router
from app.api.photos import router as photos_router
from app.config import AppSettings, load_settings
from app.container import ServiceContainer
from app.errors import (
    InvalidRequestError,
    ProviderOutputError,
    ProviderServiceError,
    ProviderTimeoutError,
    ResourceNotFoundError,
    StorageOperationError,
    StoredDataError,
    UploadTooLargeError,
    WorkflowConflictError,
)
from app.services.analysis_tasks import AnalysisTaskRegistry
from app.services.lightroom_bridge import LightroomBridge
from app.services.storage import FileStorage
from app.services.workflows import LearningWorkflow, PhotoWorkflow


LOGGER = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = BASE_DIR / "app" / "static"

# Multipart envelope overhead (boundaries, part headers, form fields) that is
# allowed on top of the file-size limit before the body is parsed. The exact
# size is not security relevant: the real per-file cap is enforced by
# ``FileStorage._read_upload``; this guard only rejects obviously oversized
# bodies before Starlette spools them to a temporary file.
UPLOAD_ENVELOPE_BYTES = 1024 * 1024
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
# Host headers a local browser may legitimately send.  TrustedHostMiddleware
# rejects DNS names that merely resolve to a loopback address, which closes the
# DNS-rebinding hole left open by an Origin-only check.
TRUSTED_HOSTS = ("127.0.0.1", "localhost", "[::1]", "testserver")
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def create_app(
    *,
    settings: AppSettings | None = None,
    storage: FileStorage | None = None,
    analysis_provider: AnalysisProvider | None = None,
    learning_provider: LearningAnalysisProvider | None = None,
    lightroom_bridge: LightroomBridge | None = None,
    server_url: str | None = None,
    analysis_tasks: AnalysisTaskRegistry | None = None,
) -> FastAPI:
    if storage is not None and settings is not None:
        raise ValueError("Pass either settings or storage, not both.")
    if storage is None:
        resolved_settings = (
            settings if settings is not None else load_settings(BASE_DIR)
        )
        resolved_storage = FileStorage(
            resolved_settings.data_root,
            max_upload_bytes=resolved_settings.max_upload_bytes,
            max_image_pixels=resolved_settings.max_image_pixels,
        )
    else:
        resolved_storage = storage
        resolved_settings = None
    provider_bundle = None
    if resolved_settings is not None and (
        analysis_provider is None or learning_provider is None
    ):
        provider_bundle = build_analysis_providers(resolved_settings)
    resolved_analysis_provider = analysis_provider
    if resolved_analysis_provider is None:
        resolved_analysis_provider = (
            provider_bundle.photo
            if provider_bundle is not None
            else MockAnalysisProvider()
        )
    resolved_learning_provider = learning_provider
    if resolved_learning_provider is None:
        resolved_learning_provider = (
            provider_bundle.learning
            if provider_bundle is not None
            else MockLearningAnalysisProvider()
        )
    resolved_lightroom_bridge = lightroom_bridge or LightroomBridge(
        settings=resolved_settings,
        server_url=server_url,
    )
    services = ServiceContainer(
        storage=resolved_storage,
        photos=PhotoWorkflow(
            storage=resolved_storage,
            analysis_provider=resolved_analysis_provider,
            settings=resolved_settings,
        ),
        learning=LearningWorkflow(
            storage=resolved_storage,
            analysis_provider=resolved_learning_provider,
        ),
        lightroom=resolved_lightroom_bridge,
        analysis_tasks=analysis_tasks or AnalysisTaskRegistry(),
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        try:
            yield
        finally:
            # Cancels in-flight analyses; the daemon threads exit on their own
            # but this is what makes the shutdown path explicit and testable.
            services.analysis_tasks.shutdown()

    application = FastAPI(
        title="AI Photo Agent",
        description=(
            "Photo editing with JPG/CR3/TIFF analysis, Lightroom develop settings "
            "and a Lightroom Classic bridge, plus Phase 2 photography learning."
        ),
        version="0.4.0",
        lifespan=lifespan,
    )
    application.state.services = services
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(TRUSTED_HOSTS),
        www_redirect=False,
    )

    @application.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @application.middleware("http")
    async def reject_oversized_upload(request: Request, call_next):
        if request.method in UNSAFE_METHODS:
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError:
                    length = -1
                limit = resolved_storage.max_upload_bytes
                if length > limit + UPLOAD_ENVELOPE_BYTES:
                    return JSONResponse(
                        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                        content={
                            "detail": f"Uploaded file exceeds the {limit} byte limit."
                        },
                    )
        return await call_next(request)

    @application.middleware("http")
    async def reject_cross_origin_writes(request: Request, call_next):
        # Requests without an Origin header (Lightroom plug-in, curl, tests) are
        # allowed: the app is a loopback tool with no authentication, and the
        # Origin check only has to stop *browser* cross-site writes.
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not _is_loopback_origin(origin):
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "Cross-origin requests are not allowed."},
                )
        return await call_next(request)

    @application.middleware("http")
    async def add_xmp_download_header(request: Request, call_next):
        response = await call_next(request)
        if (
            request.url.path.startswith("/exports/")
            and request.url.path.lower().endswith(".xmp")
        ):
            filename = Path(request.url.path).name
            response.headers["Content-Disposition"] = _xmp_content_disposition(filename)
        return response

    _register_error_handlers(application)
    application.include_router(photos_router)
    application.include_router(learning_router)
    application.include_router(lightroom_router)
    application.include_router(knowledge_router)
    # More specific mount first: learning-summary previews live in their own
    # generated subdirectory (siblings of previews), and the public URL space
    # must stay under /generated/ so the frontend can reuse same-origin images.
    application.mount(
        "/generated/summaries",
        StaticFiles(directory=resolved_storage.summary_previews_dir),
        name="summary-previews",
    )
    application.mount(
        "/generated",
        StaticFiles(directory=resolved_storage.generated_dir),
        name="generated",
    )
    application.mount(
        "/exports",
        StaticFiles(directory=resolved_storage.exports_dir),
        name="exports",
    )

    @application.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return application


def _xmp_content_disposition(filename: str) -> str:
    """Build a header-safe attachment filename for an exported preset."""

    ascii_name = (
        "".join(
            character
            for character in filename
            if character.isascii()
            and character.isprintable()
            and character not in {'"', "\\"}
        )
        or "preset.xmp"
    )
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )


def _is_loopback_origin(origin: str) -> bool:
    """True when an ``Origin`` header points at this machine.

    The Lightroom plug-in and the bundled web UI talk to the loopback server;
    any other origin is a cross-site browser write and is rejected.
    """

    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    hostname = parsed.hostname
    if not hostname:
        return False
    if hostname in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        # A DNS name such as ``127.evil.example`` is not a loopback address.
        return False


def _register_error_handlers(application: FastAPI) -> None:
    _register_error_handler(application, ResourceNotFoundError, status.HTTP_404_NOT_FOUND)
    _register_error_handler(application, InvalidRequestError, status.HTTP_400_BAD_REQUEST)
    _register_error_handler(
        application,
        UploadTooLargeError,
        status.HTTP_413_CONTENT_TOO_LARGE,
    )
    _register_error_handler(application, WorkflowConflictError, status.HTTP_409_CONFLICT)

    @application.exception_handler(StoredDataError)
    async def stored_data_error_handler(
        _request: Request,
        exc: StoredDataError,
    ) -> JSONResponse:
        LOGGER.error("Stored application data is invalid: %s", exc.detail)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Stored application data could not be read."},
        )

    @application.exception_handler(ProviderOutputError)
    async def provider_output_error_handler(
        _request: Request,
        exc: ProviderOutputError,
    ) -> JSONResponse:
        LOGGER.error("Analysis provider returned invalid data: %s", exc.detail)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Analysis provider returned invalid data."},
        )

    @application.exception_handler(ProviderTimeoutError)
    async def provider_timeout_error_handler(
        _request: Request,
        exc: ProviderTimeoutError,
    ) -> JSONResponse:
        LOGGER.error("Analysis provider timed out: %s", exc.detail)
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content={"detail": "Analysis provider request timed out."},
        )

    @application.exception_handler(ProviderServiceError)
    async def provider_service_error_handler(
        _request: Request,
        exc: ProviderServiceError,
    ) -> JSONResponse:
        LOGGER.error("Analysis provider request failed: %s", exc.detail)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Analysis provider request failed."},
        )

    @application.exception_handler(StorageOperationError)
    async def storage_operation_error_handler(
        _request: Request,
        exc: StorageOperationError,
    ) -> JSONResponse:
        LOGGER.error("Application data could not be persisted: %s", exc.detail)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Application data could not be persisted."},
        )


def _register_error_handler(
    application: FastAPI,
    error_type: type[Exception],
    status_code: int,
) -> None:
    async def handler(_request: Request, exc: Exception) -> JSONResponse:
        detail = getattr(exc, "detail", str(exc))
        return JSONResponse(status_code=status_code, content={"detail": detail})

    application.add_exception_handler(error_type, handler)
