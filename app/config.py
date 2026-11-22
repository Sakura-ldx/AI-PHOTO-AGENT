from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

DEFAULT_MAX_UPLOAD_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 50_000_000
DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"
DEFAULT_OPENAI_TIMEOUT_SECONDS = 45.0
DEFAULT_OPENAI_MAX_RETRIES = 2
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_TIMEOUT_SECONDS = 600.0
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_TIMEOUT_SECONDS = 180.0
DEFAULT_DEEPSEEK_MAX_RETRIES = 1
AnalysisProviderName = Literal["mock", "openai", "codex", "deepseek"]
# Two different defaults are deliberate, not drift:
# * a bare ``AppSettings(...)`` (used by tests and embedders) stays on "mock" so
#   it never silently requires a provider API key;
# * ``load_settings`` (used by launcher/asgi in production) defaults to the real
#   ``deepseek`` provider.
# Both names live here so the intent is documented and greppable.
DEFAULT_LIBRARY_ANALYSIS_PROVIDER: AnalysisProviderName = "mock"
DEFAULT_ENV_ANALYSIS_PROVIDER: AnalysisProviderName = "deepseek"


@dataclass(frozen=True, slots=True)
class AppSettings:
    data_root: Path
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_image_pixels: int = DEFAULT_MAX_IMAGE_PIXELS
    analysis_provider: AnalysisProviderName = DEFAULT_LIBRARY_ANALYSIS_PROVIDER
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str = DEFAULT_OPENAI_MODEL
    openai_timeout_seconds: float = DEFAULT_OPENAI_TIMEOUT_SECONDS
    openai_max_retries: int = DEFAULT_OPENAI_MAX_RETRIES
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_model: str = DEFAULT_DEEPSEEK_MODEL
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    deepseek_timeout_seconds: float = DEFAULT_DEEPSEEK_TIMEOUT_SECONDS
    deepseek_max_retries: int = DEFAULT_DEEPSEEK_MAX_RETRIES
    codex_executable: Path | None = None
    codex_model: str | None = DEFAULT_CODEX_MODEL
    codex_timeout_seconds: float = DEFAULT_CODEX_TIMEOUT_SECONDS
    lightroom_root: Path | None = None

    def __post_init__(self) -> None:
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if self.max_image_pixels <= 0:
            raise ValueError("max_image_pixels must be positive")
        if self.analysis_provider not in {"mock", "openai", "codex", "deepseek"}:
            raise ValueError(
                "analysis_provider must be 'mock', 'openai', 'codex', or 'deepseek'"
            )
        if not self.openai_model.strip():
            raise ValueError("openai_model must not be blank")
        if not 1.0 <= self.openai_timeout_seconds <= 300.0:
            raise ValueError("openai_timeout_seconds must be between 1 and 300")
        if not 0 <= self.openai_max_retries <= 5:
            raise ValueError("openai_max_retries must be between 0 and 5")
        if not self.deepseek_model.strip():
            raise ValueError("deepseek_model must not be blank")
        if not self.deepseek_base_url.strip():
            raise ValueError("deepseek_base_url must not be blank")
        if not 30.0 <= self.deepseek_timeout_seconds <= 600.0:
            raise ValueError("deepseek_timeout_seconds must be between 30 and 600")
        if not 0 <= self.deepseek_max_retries <= 3:
            raise ValueError("deepseek_max_retries must be between 0 and 3")
        if self.codex_model is not None and not self.codex_model.strip():
            raise ValueError("codex_model must not be blank")
        if not 30.0 <= self.codex_timeout_seconds <= 900.0:
            raise ValueError("codex_timeout_seconds must be between 30 and 900")


def expand_path(value: str | os.PathLike[str]) -> Path:
    """Expand environment variables and ``~`` in a configured filesystem path.

    Windows users commonly write ``%APPDATA%\\Adobe\\Lightroom``; POSIX users
    may write ``$HOME/...``. ``Path.expanduser`` alone leaves those tokens
    literal, so resolve environment variables first.
    """

    return Path(os.path.expandvars(os.fspath(value))).expanduser()


def load_settings(default_data_root: Path) -> AppSettings:
    configured_data_root = os.getenv("AI_PHOTO_AGENT_DATA_DIR")
    data_root_value = (
        configured_data_root.strip()
        if configured_data_root and configured_data_root.strip()
        else default_data_root
    )
    data_root = expand_path(data_root_value)
    return AppSettings(
        data_root=data_root.resolve(),
        max_upload_bytes=_positive_int_env(
            "AI_PHOTO_AGENT_MAX_UPLOAD_BYTES",
            DEFAULT_MAX_UPLOAD_BYTES,
        ),
        max_image_pixels=_positive_int_env(
            "AI_PHOTO_AGENT_MAX_IMAGE_PIXELS",
            DEFAULT_MAX_IMAGE_PIXELS,
        ),
        analysis_provider=_analysis_provider_env(),
        openai_api_key=_optional_env("AI_PHOTO_AGENT_OPENAI_API_KEY")
        or _optional_env("OPENAI_API_KEY"),
        openai_model=_nonblank_env(
            "AI_PHOTO_AGENT_OPENAI_MODEL",
            DEFAULT_OPENAI_MODEL,
        ),
        openai_timeout_seconds=_bounded_float_env(
            "AI_PHOTO_AGENT_OPENAI_TIMEOUT_SECONDS",
            DEFAULT_OPENAI_TIMEOUT_SECONDS,
            minimum=1.0,
            maximum=300.0,
        ),
        openai_max_retries=_bounded_int_env(
            "AI_PHOTO_AGENT_OPENAI_MAX_RETRIES",
            DEFAULT_OPENAI_MAX_RETRIES,
            minimum=0,
            maximum=5,
        ),
        deepseek_api_key=_deepseek_api_key(),
        deepseek_model=_nonblank_env(
            "AI_PHOTO_AGENT_DEEPSEEK_MODEL",
            DEFAULT_DEEPSEEK_MODEL,
        ),
        deepseek_base_url=_nonblank_env(
            "AI_PHOTO_AGENT_DEEPSEEK_BASE_URL",
            DEFAULT_DEEPSEEK_BASE_URL,
        ).rstrip("/"),
        deepseek_timeout_seconds=_bounded_float_env(
            "AI_PHOTO_AGENT_DEEPSEEK_TIMEOUT_SECONDS",
            DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
            minimum=30.0,
            maximum=600.0,
        ),
        deepseek_max_retries=_bounded_int_env(
            "AI_PHOTO_AGENT_DEEPSEEK_MAX_RETRIES",
            DEFAULT_DEEPSEEK_MAX_RETRIES,
            minimum=0,
            maximum=3,
        ),
        codex_executable=_optional_path_env("AI_PHOTO_AGENT_CODEX_EXECUTABLE"),
        codex_model=_nonblank_env(
            "AI_PHOTO_AGENT_CODEX_MODEL",
            DEFAULT_CODEX_MODEL,
        ),
        codex_timeout_seconds=_bounded_float_env(
            "AI_PHOTO_AGENT_CODEX_TIMEOUT_SECONDS",
            DEFAULT_CODEX_TIMEOUT_SECONDS,
            minimum=30.0,
            maximum=900.0,
        ),
        lightroom_root=_optional_path_env("AI_PHOTO_AGENT_LIGHTROOM_ROOT"),
    )


def _positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _analysis_provider_env() -> AnalysisProviderName:
    value = _nonblank_env(
        "AI_PHOTO_AGENT_ANALYSIS_PROVIDER", DEFAULT_ENV_ANALYSIS_PROVIDER
    ).lower()
    if value not in {"mock", "openai", "codex", "deepseek"}:
        raise ValueError(
            "AI_PHOTO_AGENT_ANALYSIS_PROVIDER must be "
            "'mock', 'openai', 'codex', or 'deepseek'"
        )
    return value


def _deepseek_api_key() -> str | None:
    """Read the DeepSeek key from env, then an explicitly configured file.

    The key is never logged or returned in HTTP responses. Implicit credential
    stores (for example another application's home directory) are deliberately
    not consulted: set ``AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE`` explicitly
    or use one of the environment variables.
    """

    for name in ("AI_PHOTO_AGENT_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"):
        value = _optional_env(name)
        if value is not None:
            return value
    credentials_file = _optional_path_env(
        "AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE"
    )
    if credentials_file is None:
        return None
    try:
        text = credentials_file.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"(?m)^\s*DEEPSEEK_API_KEY:\s*(\S+)\s*$", text)
    if match is None:
        return None
    value = match.group(1).strip().strip("'\"")
    return value or None


def _optional_env(name: str) -> str | None:
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    value = raw_value.strip()
    return value or None


def _optional_path_env(name: str) -> Path | None:
    value = _optional_env(name)
    return expand_path(value) if value is not None else None


def _nonblank_env(name: str, default: str) -> str:
    value = _optional_env(name)
    return value if value is not None else default


def _bounded_int_env(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float_env(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value
