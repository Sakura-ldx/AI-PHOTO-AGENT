from __future__ import annotations


class ApplicationError(Exception):
    """Base class for errors that can be translated at the API boundary."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class InvalidRequestError(ApplicationError):
    """The supplied input cannot be processed."""


class UploadTooLargeError(ApplicationError):
    """An uploaded file exceeds the configured byte limit."""


class ResourceNotFoundError(ApplicationError):
    """A requested local resource does not exist."""


class WorkflowConflictError(ApplicationError):
    """The requested operation conflicts with current workflow state."""


class StoredDataError(ApplicationError):
    """Persisted state exists but is unreadable or internally inconsistent."""


class StorageOperationError(ApplicationError):
    """A filesystem operation failed while persisting application data."""


class ProviderOutputError(ApplicationError):
    """An analysis provider returned data that violates its application contract."""


class ProviderServiceError(ApplicationError):
    """An upstream analysis provider could not complete a request."""


class ProviderTimeoutError(ProviderServiceError):
    """An upstream analysis provider exceeded its configured timeout."""


class AnalysisCancelled(ApplicationError):
    """A background analysis was cancelled and must not persist its result."""
