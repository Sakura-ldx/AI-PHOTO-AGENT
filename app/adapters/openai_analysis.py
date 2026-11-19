from __future__ import annotations

import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Any

from openai import APITimeoutError, OpenAI, OpenAIError
from pydantic import BaseModel

from app.adapters.analysis import AnalysisProvider
from app.adapters.model_image import (
    MODEL_IMAGE_MAX_SIZE as MODEL_IMAGE_MAX_SIZE,
    prepare_model_image,
)
from app.adapters.vision_models import (
    PHOTO_PROMPT_VERSION,
    PHOTO_SYSTEM_PROMPT,
    StructuredEditingParameters,
    StructuredEditingStyle,
    StructuredHslAdjustments,
    StructuredPhotoAnalysis,
    photo_measurements_block,
)
from app.errors import ProviderOutputError, ProviderServiceError, ProviderTimeoutError
from app.schemas import ImageAnalysis, ImageMetadata


LOGGER = logging.getLogger(__name__)
MAX_OUTPUT_TOKENS = 8_000

SYSTEM_PROMPT = PHOTO_SYSTEM_PROMPT
OpenAIHslAdjustments = StructuredHslAdjustments
OpenAIEditingParameters = StructuredEditingParameters
OpenAIEditingStyle = StructuredEditingStyle
OpenAIImageAnalysis = StructuredPhotoAnalysis


class OpenAIAnalysisProvider(AnalysisProvider):
    """OpenAI Responses API implementation for Phase 1 photo analysis."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_seconds: float,
        max_retries: int,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OPENAI_API_KEY is required for the OpenAI analysis provider.")
        if not model.strip():
            raise ValueError("OpenAI model must not be blank.")
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self._client = (
            client
            if client is not None
            else OpenAI(
                api_key=api_key,
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
        )

    def analyze(self, image_path: Path, metadata: ImageMetadata) -> ImageAnalysis:
        image_data_url = _prepare_model_image(image_path)
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
        started_at = perf_counter()
        try:
            response = self._client.responses.parse(
                model=self.model,
                input=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    f"prompt_version={PHOTO_PROMPT_VERSION}\n"
                                    f"{measurements_block}\n\n"
                                    "请分析这张待编辑照片。"
                                    "上传元数据（不可信，仅作尺寸提示）："
                                    f"{metadata_payload}"
                                ),
                            },
                            {
                                "type": "input_image",
                                "image_url": image_data_url,
                                "detail": "high",
                            },
                        ],
                    },
                ],
                text_format=OpenAIImageAnalysis,
                reasoning={"effort": "low"},
                max_output_tokens=MAX_OUTPUT_TOKENS,
                store=False,
            )
        except APITimeoutError as exc:
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            LOGGER.warning(
                "OpenAI photo analysis timed out model=%s elapsed_ms=%s",
                self.model,
                elapsed_ms,
            )
            raise ProviderTimeoutError(
                f"OpenAI photo analysis timed out for model {self.model}."
            ) from exc
        except OpenAIError as exc:
            elapsed_ms = round((perf_counter() - started_at) * 1000)
            status_code = getattr(exc, "status_code", None)
            request_id = getattr(exc, "request_id", None)
            LOGGER.warning(
                "OpenAI photo analysis failed model=%s error_type=%s "
                "status_code=%s request_id=%s elapsed_ms=%s",
                self.model,
                type(exc).__name__,
                status_code,
                request_id,
                elapsed_ms,
            )
            raise ProviderServiceError(
                "OpenAI photo analysis request failed "
                f"(model={self.model}, status={status_code}, request_id={request_id})."
            ) from exc

        elapsed_ms = round((perf_counter() - started_at) * 1000)
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise ProviderOutputError(
                "OpenAI photo analysis returned no structured output."
            )
        payload = (
            parsed.model_dump(mode="python", round_trip=True)
            if isinstance(parsed, BaseModel)
            else parsed
        )
        try:
            structured = OpenAIImageAnalysis.model_validate(payload)
            analysis = structured.to_domain()
        except Exception as exc:
            raise ProviderOutputError(
                "OpenAI photo analysis returned invalid structured output."
            ) from exc

        usage = getattr(response, "usage", None)
        LOGGER.info(
            "OpenAI photo analysis completed model=%s request_id=%s elapsed_ms=%s "
            "input_tokens=%s output_tokens=%s",
            self.model,
            getattr(response, "_request_id", None),
            elapsed_ms,
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
        )
        return analysis


def _prepare_model_image(image_path: Path) -> str:
    return prepare_model_image(image_path).data_url
