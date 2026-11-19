from __future__ import annotations

from dataclasses import dataclass

from app.adapters.analysis import AnalysisProvider, MockAnalysisProvider
from app.adapters.codex_analysis import CodexAnalysisProvider
from app.adapters.codex_learning import CodexLearningAnalysisProvider
from app.adapters.deepseek_analysis import DeepSeekAnalysisProvider
from app.adapters.learning import LearningAnalysisProvider, MockLearningAnalysisProvider
from app.adapters.openai_analysis import OpenAIAnalysisProvider
from app.adapters.openai_learning import OpenAILearningAnalysisProvider
from app.config import AppSettings


@dataclass(frozen=True, slots=True)
class AnalysisProviderBundle:
    photo: AnalysisProvider
    learning: LearningAnalysisProvider


def build_analysis_providers(settings: AppSettings) -> AnalysisProviderBundle:
    if settings.analysis_provider == "mock":
        return AnalysisProviderBundle(
            photo=MockAnalysisProvider(),
            learning=MockLearningAnalysisProvider(),
        )
    if settings.analysis_provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError(
                "OPENAI_API_KEY is required when "
                "AI_PHOTO_AGENT_ANALYSIS_PROVIDER=openai."
            )
        return AnalysisProviderBundle(
            photo=OpenAIAnalysisProvider(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                timeout_seconds=settings.openai_timeout_seconds,
                max_retries=settings.openai_max_retries,
            ),
            learning=OpenAILearningAnalysisProvider(
                api_key=settings.openai_api_key,
                model=settings.openai_model,
                timeout_seconds=settings.openai_timeout_seconds,
                max_retries=settings.openai_max_retries,
            ),
        )
    if settings.analysis_provider == "deepseek":
        if settings.deepseek_api_key is None:
            raise ValueError(
                "DEEPSEEK_API_KEY is required when "
                "AI_PHOTO_AGENT_ANALYSIS_PROVIDER=deepseek. "
                "Set AI_PHOTO_AGENT_DEEPSEEK_API_KEY or DEEPSEEK_API_KEY, "
                "or point AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE at a "
                "YAML file containing DEEPSEEK_API_KEY."
            )
        return AnalysisProviderBundle(
            photo=DeepSeekAnalysisProvider(
                api_key=settings.deepseek_api_key,
                model=settings.deepseek_model,
                base_url=settings.deepseek_base_url,
                timeout_seconds=settings.deepseek_timeout_seconds,
                max_retries=settings.deepseek_max_retries,
            ),
            # The learning workflow is outside this repair; keep its
            # deterministic local implementation while photos use real vision.
            learning=MockLearningAnalysisProvider(),
        )
    if settings.analysis_provider == "codex":
        return AnalysisProviderBundle(
            photo=CodexAnalysisProvider(
                data_root=settings.data_root,
                executable=settings.codex_executable,
                model=settings.codex_model,
                timeout_seconds=settings.codex_timeout_seconds,
            ),
            learning=CodexLearningAnalysisProvider(
                data_root=settings.data_root,
                executable=settings.codex_executable,
                model=settings.codex_model,
                timeout_seconds=settings.codex_timeout_seconds,
            ),
        )
    raise ValueError(f"Unsupported analysis provider: {settings.analysis_provider}")


def build_analysis_provider(settings: AppSettings) -> AnalysisProvider:
    """Backward-compatible single-provider helper for Phase 1 callers."""

    return build_analysis_providers(settings).photo


def build_learning_analysis_provider(settings: AppSettings) -> LearningAnalysisProvider:
    return build_analysis_providers(settings).learning
