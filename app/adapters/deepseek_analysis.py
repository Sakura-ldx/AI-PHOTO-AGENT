from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
from pydantic import ValidationError

from app.adapters.analysis import AnalysisProvider
from app.adapters.model_image import prepare_model_image
from app.adapters.vision_models import (
    PHOTO_PROMPT_VERSION,
    PHOTO_SYSTEM_PROMPT,
    StructuredPhotoAnalysis,
    photo_measurements_block,
    strict_output_schema,
)
from app.errors import ProviderOutputError, ProviderServiceError, ProviderTimeoutError
from app.schemas import ImageAnalysis, ImageMetadata


LOGGER = logging.getLogger(__name__)
# The model spends a large part of the budget on reasoning tokens before it
# emits JSON, and the three-style payload is ~7 KB of text. 12000 was hit
# repeatedly in practice (finish_reason="length"), which truncates the JSON
# mid-string and surfaces as "invalid structured output".
MAX_OUTPUT_TOKENS = 24_000
JSON_RESPONSE_FORMAT = {"type": "json_object"}
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# Exponential backoff between retries: without it a 429/503 immediately retried
# once, which almost always hit the same overloaded endpoint again.
BASE_RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 8.0


def _backoff_seconds(attempt: int) -> float:
    delay = BASE_RETRY_DELAY_SECONDS * (2**attempt)
    return min(delay + random.uniform(0.0, delay * 0.25), MAX_RETRY_DELAY_SECONDS)


def _retry_delay_seconds(response: Any, attempt: int) -> float:
    headers = getattr(response, "headers", None)
    retry_after = headers.get("Retry-After") if headers is not None else None
    if retry_after:
        try:
            hinted = float(retry_after)
        except ValueError:
            hinted = -1.0
        if hinted >= 0.0:
            return min(hinted, MAX_RETRY_DELAY_SECONDS)
    return _backoff_seconds(attempt)


class DeepSeekAnalysisProvider(AnalysisProvider):
    """Vision analysis through DeepSeek's OpenAI-compatible chat API.

    DeepSeek returns a fresh diagnosis and three image-specific styles for
    every upload. It never falls back to the deterministic mock output.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        timeout_seconds: float = 180.0,
        max_retries: int = 1,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("DEEPSEEK_API_KEY is required for the DeepSeek provider.")
        if not model.strip():
            raise ValueError("DeepSeek model must not be blank.")
        if not base_url.strip():
            raise ValueError("DeepSeek base URL must not be blank.")
        self.model = model.strip()
        self.base_url = base_url.strip().rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self._api_key = api_key.strip()
        self._client = (
            client
            if client is not None
            else httpx.Client(
                timeout=httpx.Timeout(timeout_seconds, connect=20.0),
            )
        )

    def analyze(self, image_path: Path, metadata: ImageMetadata) -> ImageAnalysis:
        prepared = prepare_model_image(image_path)
        measurements_block = photo_measurements_block(image_path)
        started_at = perf_counter()
        last_output_error: Exception | None = None
        retry_compact = False

        # A reasoning model can occasionally emit a truncated or imperfect JSON
        # object. Retry the structured-output request once with an explicit
        # reminder instead of failing the whole upload.
        for output_attempt in range(2):
            payload = self._build_payload(
                metadata=metadata,
                image_data_url=prepared.data_url,
                measurements_block=measurements_block,
                strict=output_attempt > 0,
                compact=retry_compact,
            )
            response = self._post_with_retries(payload)
            elapsed_ms = round((perf_counter() - started_at) * 1000)

            if response.status_code != 200:
                LOGGER.warning(
                    "DeepSeek photo analysis failed model=%s status=%s "
                    "elapsed_ms=%s",
                    self.model,
                    response.status_code,
                    elapsed_ms,
                )
                raise ProviderServiceError(
                    "DeepSeek photo analysis request failed "
                    f"(model={self.model}, status={response.status_code})."
                )

            try:
                data = response.json()
            except ValueError as exc:
                last_output_error = ProviderOutputError(
                    "DeepSeek photo analysis returned a non-JSON response."
                )
                if output_attempt == 0:
                    continue
                raise last_output_error from exc

            try:
                content = _response_content(data)
            except ProviderOutputError as exc:
                last_output_error = exc
                if output_attempt == 0:
                    continue
                raise
            finish_reason = _finish_reason(data)
            truncated = finish_reason == "length"
            if truncated:
                LOGGER.warning(
                    "DeepSeek photo analysis output hit the token limit "
                    "model=%s elapsed_ms=%s completion_tokens=%s",
                    self.model,
                    elapsed_ms,
                    (data.get("usage") or {}).get("completion_tokens"),
                )
            if not content.strip():
                last_output_error = ProviderOutputError(
                    "DeepSeek photo analysis returned no content "
                    f"(finish_reason={finish_reason})."
                )
                if output_attempt == 0:
                    retry_compact = truncated
                    continue
                raise last_output_error

            try:
                structured = StructuredPhotoAnalysis.model_validate_json(
                    _strip_json_fence(content)
                )
                analysis = structured.to_domain()
            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                last_output_error = ProviderOutputError(
                    "DeepSeek photo analysis returned invalid structured output."
                )
                if output_attempt == 0:
                    retry_compact = truncated
                    continue
                raise last_output_error from exc

            usage = data.get("usage") if isinstance(data, dict) else None
            LOGGER.info(
                "DeepSeek photo analysis completed model=%s elapsed_ms=%s "
                "prompt_tokens=%s completion_tokens=%s styles=%s attempts=%s",
                self.model,
                elapsed_ms,
                (usage or {}).get("prompt_tokens"),
                (usage or {}).get("completion_tokens"),
                [style.id for style in analysis.recommended_styles],
                output_attempt + 1,
            )
            return analysis

        raise last_output_error or ProviderOutputError(
            "DeepSeek photo analysis returned invalid structured output."
        )

    def _build_payload(
        self,
        *,
        metadata: ImageMetadata,
        image_data_url: str,
        measurements_block: str = "",
        strict: bool,
        compact: bool = False,
    ) -> dict[str, Any]:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": PHOTO_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": _user_prompt(
                                metadata,
                                measurements_block=measurements_block,
                                strict=strict,
                                compact=compact,
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url},
                        },
                    ],
                },
            ],
            "max_tokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.2,
            "response_format": JSON_RESPONSE_FORMAT,
            "stream": False,
        }

    def _post_with_retries(self, payload: dict[str, Any]) -> Any:
        attempts = self.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise ProviderTimeoutError(
                        "DeepSeek photo analysis timed out."
                    ) from exc
                time.sleep(_backoff_seconds(attempt))
                continue
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise ProviderServiceError(
                        "DeepSeek photo analysis could not reach the API."
                    ) from exc
                time.sleep(_backoff_seconds(attempt))
                continue

            if (
                response.status_code in RETRYABLE_STATUS_CODES
                and attempt + 1 < attempts
            ):
                time.sleep(_retry_delay_seconds(response, attempt))
                continue
            return response
        raise ProviderServiceError(
            "DeepSeek photo analysis request failed."
        ) from last_error


def _user_prompt(
    metadata: ImageMetadata,
    *,
    measurements_block: str = "",
    strict: bool = False,
    compact: bool = False,
) -> str:
    metadata_payload = json.dumps(
        {
            "width": metadata.width,
            "height": metadata.height,
            "format": metadata.format,
            "color_mode": metadata.color_mode,
        },
        ensure_ascii=False,
    )
    schema = json.dumps(
        strict_output_schema(StructuredPhotoAnalysis),
        ensure_ascii=False,
    )
    prompt = f"prompt_version={PHOTO_PROMPT_VERSION}\n"
    if measurements_block:
        prompt += f"{measurements_block}\n\n"
    prompt += (
        "请分析附加图片。上传元数据（不可信，仅作尺寸提示）："
        f"{metadata_payload}\n\n"
        "只输出一个 JSON 对象，不要 Markdown 代码块，不要解释，不要输出 JSON "
        "之外的文字。必须严格符合以下 JSON Schema：\n"
        f"{schema}"
    )
    if strict:
        prompt += (
            "\n\n上一次输出未通过 JSON Schema 校验。请重新输出完整合法 JSON："
            "recommended_styles 必须恰好 3 个且 id 唯一；每个参数必须在范围内；"
            "tone_curve 从 x=0 到 x=255 且 x 严格递增；HSL 必须包含全部 8 个通道。"
        )
    if compact:
        prompt += (
            "\n\n上一次输出被输出长度限制截断。请大幅精简文字后再输出："
            "overall_summary 不超过 120 字；每个列表最多 4 条、每条不超过 40 字；"
            "每套风格的 description / best_fit_reason / expected_visual_impact / "
            "rationale / crop_suggestion.rationale 各不超过 40 字。"
            "数值字段不得省略，必须输出完整、可解析的 JSON。"
        )
    return prompt


def _response_content(data: Any) -> str:
    if not isinstance(data, dict):
        raise ProviderOutputError("DeepSeek photo analysis returned an invalid response.")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderOutputError("DeepSeek photo analysis returned no choices.")
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderOutputError("DeepSeek photo analysis returned an invalid choice.")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ProviderOutputError("DeepSeek photo analysis returned no message.")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(parts)
    return ""


def _finish_reason(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    return choices[0].get("finish_reason")


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    if not value.startswith("{"):
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            value = value[start : end + 1]
    return value
