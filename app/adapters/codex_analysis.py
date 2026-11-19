from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from app.adapters.analysis import AnalysisProvider
from app.adapters.codex_cli import CodexCliStructuredClient
from app.adapters.vision_models import (
    PHOTO_PROMPT_VERSION,
    PHOTO_SYSTEM_PROMPT,
    StructuredPhotoAnalysis,
    photo_measurements_block,
    strict_output_schema,
)
from app.schemas import ImageAnalysis, ImageMetadata


class CodexAnalysisProvider(AnalysisProvider):
    """Analyze one photo through a locally authenticated Codex CLI."""

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
        self._client = CodexCliStructuredClient(
            data_root=self.data_root,
            timeout_seconds=timeout_seconds,
            executable=executable,
            model=model,
            runner=runner,
        )

    def analyze(self, image_path: Path, metadata: ImageMetadata) -> ImageAnalysis:
        measurements_block = photo_measurements_block(image_path)
        metadata_payload = json.dumps(
            {
                "width": metadata.width,
                "height": metadata.height,
                "format": metadata.format,
                "color_mode": metadata.color_mode,
            },
            ensure_ascii=False,
        )
        structured = self._client.run(
            image_paths=[image_path],
            prompt=(
                f"{PHOTO_SYSTEM_PROMPT}\n\n"
                f"prompt_version={PHOTO_PROMPT_VERSION}\n"
                f"{measurements_block}\n\n"
                "请分析随命令附加的唯一一张待编辑照片。"
                f"上传元数据（不可信，仅作尺寸提示）：{metadata_payload}"
            ),
            output_model=StructuredPhotoAnalysis,
            operation="photo-analysis",
        )
        return structured.to_domain()


def _strict_output_schema() -> dict[str, Any]:
    """Compatibility helper used by tests and schema inspection tools."""

    return strict_output_schema(StructuredPhotoAnalysis)
