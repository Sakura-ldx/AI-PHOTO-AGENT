from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.adapters.model_image import prepare_model_image
from app.adapters.vision_models import strict_output_schema
from app.errors import ProviderOutputError, ProviderServiceError, ProviderTimeoutError


LOGGER = logging.getLogger(__name__)
_EXECUTABLE_CACHE_LOCK = threading.Lock()
StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)

_DISABLED_FEATURES = (
    "shell_tool",
    "plugins",
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "multi_agent",
    "hooks",
    "skill_search",
    "tool_suggest",
    "workspace_dependencies",
)

_ENVIRONMENT_ALLOWLIST = {
    "ALL_PROXY",
    "APPDATA",
    "CODEX_HOME",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LOCALAPPDATA",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}


class CodexCliStructuredClient:
    """Run a tool-disabled, ephemeral Codex image request with strict output."""

    def __init__(
        self,
        *,
        data_root: Path,
        timeout_seconds: float,
        executable: Path | None = None,
        model: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.data_root = data_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.executable = executable
        self.model = model.strip() if model is not None else None
        self._runner = runner

    def run(
        self,
        *,
        image_paths: Sequence[Path],
        prompt: str,
        output_model: type[StructuredOutput],
        operation: str,
    ) -> StructuredOutput:
        if not image_paths:
            raise ValueError("Codex image analysis requires at least one image.")
        started_at = perf_counter()
        raw_result: str
        try:
            runtime_root = self.data_root / "data" / "runtime"
            jobs_root = runtime_root / "codex-jobs"
            jobs_root.mkdir(parents=True, exist_ok=True)
            executable = _resolve_executable(self.executable, runtime_root)

            with tempfile.TemporaryDirectory(prefix=f"{operation}-", dir=jobs_root) as job:
                job_dir = Path(job)
                prepared_paths: list[Path] = []
                for index, source_path in enumerate(image_paths, start=1):
                    filename = "input.jpg" if len(image_paths) == 1 else f"input-{index:02d}.jpg"
                    prepared_path = job_dir / filename
                    prepare_model_image(source_path).write_to(prepared_path)
                    prepared_paths.append(prepared_path)

                schema_path = job_dir / "output-schema.json"
                result_path = job_dir / "output-result.json"
                schema_path.write_text(
                    json.dumps(strict_output_schema(output_model), ensure_ascii=False),
                    encoding="utf-8",
                )
                command = _build_command(
                    executable=executable,
                    job_dir=job_dir,
                    image_paths=prepared_paths,
                    schema_path=schema_path,
                    result_path=result_path,
                    model=self.model,
                    prompt=prompt,
                )
                completed = self._runner(
                    command,
                    input="",
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    env=_minimal_environment(),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode != 0:
                    LOGGER.warning(
                        "Codex CLI operation failed operation=%s exit_code=%s stderr_bytes=%s",
                        operation,
                        completed.returncode,
                        len(completed.stderr.encode("utf-8", errors="replace")),
                    )
                    raise ProviderServiceError(
                        f"Codex CLI {operation} failed with exit code "
                        f"{completed.returncode}."
                    )
                if not result_path.is_file():
                    raise ProviderOutputError(
                        f"Codex CLI {operation} returned no result file."
                    )
                raw_result = result_path.read_text(encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            LOGGER.warning(
                "Codex CLI operation timed out operation=%s elapsed_ms=%s",
                operation,
                elapsed_ms,
            )
            raise ProviderTimeoutError(f"Codex CLI {operation} timed out.") from exc
        except (ProviderOutputError, ProviderServiceError):
            raise
        except (OSError, shutil.Error) as exc:
            raise ProviderServiceError(
                f"Codex CLI {operation} could not start or access its runtime files."
            ) from exc

        try:
            payload = json.loads(raw_result)
            structured = output_model.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise ProviderOutputError(
                f"Codex CLI {operation} returned invalid structured output."
            ) from exc

        elapsed_ms = round((perf_counter() - started_at) * 1000)
        LOGGER.info(
            "Codex CLI operation completed operation=%s model=%s images=%s elapsed_ms=%s",
            operation,
            self.model or "default",
            len(image_paths),
            elapsed_ms,
        )
        return structured


def _minimal_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _ENVIRONMENT_ALLOWLIST
    }
    environment["NO_COLOR"] = "1"
    return environment


def _resolve_executable(configured: Path | None, runtime_root: Path) -> Path:
    if configured is not None:
        source = configured.expanduser().resolve()
    else:
        discovered = shutil.which("codex")
        if discovered is None:
            raise ProviderServiceError(
                "Codex CLI was not found. Install Codex or configure "
                "AI_PHOTO_AGENT_CODEX_EXECUTABLE."
            )
        source = Path(discovered).resolve()
    if not source.is_file():
        raise ProviderServiceError(f"Codex CLI executable does not exist: {source}")
    if os.name != "nt" or "windowsapps" not in str(source).lower():
        return source

    stat = source.stat()
    cache_dir = runtime_root / "codex-cli"
    cache_path = cache_dir / f"codex-{stat.st_size}-{stat.st_mtime_ns}.exe"
    with _EXECUTABLE_CACHE_LOCK:
        if cache_path.is_file() and cache_path.stat().st_size == stat.st_size:
            return cache_path
        cache_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_suffix(".tmp")
        shutil.copy2(source, temporary_path)
        os.replace(temporary_path, cache_path)
    return cache_path


def _build_command(
    *,
    executable: Path,
    job_dir: Path,
    image_paths: Sequence[Path],
    schema_path: Path,
    result_path: Path,
    model: str | None,
    prompt: str,
) -> list[str]:
    command = [
        str(executable),
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--color",
        "never",
        "--cd",
        str(job_dir),
    ]
    for feature in _DISABLED_FEATURES:
        command.extend(["--disable", feature])
    for image_path in image_paths:
        command.extend(["--image", str(image_path)])
    command.extend(
        [
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
        ]
    )
    if model is not None:
        command.extend(["--model", model])
    command.append(prompt)
    return command
