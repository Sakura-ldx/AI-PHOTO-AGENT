from __future__ import annotations

import importlib
import os

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.adapters.analysis import MockAnalysisProvider
from app.adapters.codex_analysis import CodexAnalysisProvider
from app.adapters.codex_learning import CodexLearningAnalysisProvider
from app.adapters.deepseek_analysis import DeepSeekAnalysisProvider
from app.adapters.learning import MockLearningAnalysisProvider
from app.adapters.openai_analysis import OpenAIAnalysisProvider
from app.adapters.openai_learning import OpenAILearningAnalysisProvider
from app.config import (
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_TIMEOUT_SECONDS,
    DEFAULT_DEEPSEEK_BASE_URL,
    DEFAULT_DEEPSEEK_MAX_RETRIES,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_DEEPSEEK_TIMEOUT_SECONDS,
    DEFAULT_MAX_IMAGE_PIXELS,
    DEFAULT_MAX_UPLOAD_BYTES,
    DEFAULT_OPENAI_MAX_RETRIES,
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_TIMEOUT_SECONDS,
    AppSettings,
    load_settings,
)
from app.main import create_app
from app.services.storage import FileStorage


def test_injected_storage_does_not_read_unrelated_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_PHOTO_AGENT_MAX_UPLOAD_BYTES", "not-an-integer")
    storage = FileStorage(tmp_path)

    client = TestClient(create_app(storage=storage))

    assert client.get("/").status_code == 200


def test_main_module_import_is_configuration_side_effect_free(monkeypatch) -> None:
    monkeypatch.setenv("AI_PHOTO_AGENT_MAX_UPLOAD_BYTES", "not-an-integer")

    reloaded = importlib.reload(main_module)

    assert callable(reloaded.create_app)
    assert not hasattr(reloaded, "app")


def test_application_factories_bind_static_files_to_their_own_storage(tmp_path) -> None:
    first_storage = FileStorage(tmp_path / "first")
    second_storage = FileStorage(tmp_path / "second")
    asset = first_storage.generated_dir / "factory.txt"
    asset.write_text("first", encoding="utf-8")

    first_client = TestClient(create_app(storage=first_storage))
    second_client = TestClient(create_app(storage=second_storage))

    assert first_client.get("/generated/factory.txt").text == "first"
    assert second_client.get("/generated/factory.txt").status_code == 404


def test_corrupt_private_state_returns_a_generic_error(tmp_path) -> None:
    storage = FileStorage(tmp_path)
    session = storage.create_learning_session("corrupt state")
    storage.learning_session_path(session.id).write_text("{broken", encoding="utf-8")
    client = TestClient(create_app(storage=storage))

    response = client.get(f"/api/learning/sessions/{session.id}")

    assert response.status_code == 500
    assert response.json() == {"detail": "Stored application data could not be read."}
    assert str(storage.learning_session_path(session.id)) not in response.text


def test_frontend_avoids_dynamic_html_and_guards_stale_actions(tmp_path) -> None:
    client = TestClient(create_app(storage=FileStorage(tmp_path)))

    html = client.get("/").text

    assert "innerHTML" not in html
    assert "sameOriginUrl(result.download_url)" in html
    assert "state.photoId !== photoId" in html
    assert 'id="learningSection"' not in html
    assert "function setActiveView(view)" in html
    assert "if (isKnowledge && !state.knowledgeList && !knowledgeViewLoading)" in html
    assert "if (sequence !== knowledgeLoadSequence) return;" in html
    assert 'el("fileInput").addEventListener("change", uploadAndAnalyze)' in html
    assert 'id="uploadBtn"' not in html
    assert "state.uploading = true" in html


def test_frontend_has_timeouts_accessibility_and_ordered_status(tmp_path) -> None:
    client = TestClient(create_app(storage=FileStorage(tmp_path)))

    html = client.get("/").text

    # Every request is cancellable: a hung service must not freeze the page.
    assert "const REQUEST_TIMEOUT_MS = 600000" in html
    assert "const STATUS_TIMEOUT_MS = 15000" in html
    assert "function fetchWithTimeout(url, options = {}, timeoutMs = REQUEST_TIMEOUT_MS)" in html
    assert "new AbortController()" in html
    assert 'fetchWithTimeout(url, { method: "POST", body })' in html
    assert '"/api/lightroom/status",' in html
    assert "STATUS_TIMEOUT_MS," in html
    # A stale status response may not overwrite a newer one.
    assert "lightroomStatusSequence += 1" in html
    assert "if (sequence !== lightroomStatusSequence) return;" in html
    # Keyboard users can pick a style, intensity has a label, status is announced.
    assert 'card.tabIndex = 0;' in html
    assert 'card.setAttribute("role", "button");' in html
    assert 'card.setAttribute("aria-pressed", selected ? "true" : "false");' in html
    assert 'event.key === "Enter" || event.key === " "' in html
    assert '<label for="intensity">' in html
    assert html.count('aria-live="polite"') >= 4
    # Network failures show a start-the-service hint, not raw browser wording.
    assert "const NETWORK_ERROR_MESSAGE" in html
    assert 'error.name === "AbortError"' in html


def test_factory_preserves_falsy_provider_and_rejects_ambiguous_storage_config(tmp_path) -> None:
    class FalsyProvider(MockAnalysisProvider):
        def __bool__(self) -> bool:
            return False

    class FalsyLearningProvider(MockLearningAnalysisProvider):
        def __bool__(self) -> bool:
            return False

    class FalsySettings(AppSettings):
        def __bool__(self) -> bool:
            return False

    provider = FalsyProvider()
    learning_provider = FalsyLearningProvider()
    storage = FileStorage(tmp_path)

    application = create_app(
        storage=storage,
        analysis_provider=provider,
        learning_provider=learning_provider,
    )

    assert application.state.services.photos.analysis_provider is provider
    assert application.state.services.learning.analysis_provider is learning_provider

    settings = FalsySettings(data_root=tmp_path / "settings")
    settings_application = create_app(settings=settings)
    assert settings_application.state.services.storage.root == settings.data_root.resolve()

    with pytest.raises(ValueError, match="either settings or storage"):
        create_app(
            settings=AppSettings(data_root=tmp_path / "other"),
            storage=storage,
        )


def test_settings_defaults_overrides_and_blank_data_directory(monkeypatch, tmp_path) -> None:
    for name in (
        "AI_PHOTO_AGENT_DATA_DIR",
        "AI_PHOTO_AGENT_MAX_UPLOAD_BYTES",
        "AI_PHOTO_AGENT_MAX_IMAGE_PIXELS",
        "AI_PHOTO_AGENT_ANALYSIS_PROVIDER",
        "AI_PHOTO_AGENT_OPENAI_MODEL",
        "AI_PHOTO_AGENT_OPENAI_TIMEOUT_SECONDS",
        "AI_PHOTO_AGENT_OPENAI_MAX_RETRIES",
        "AI_PHOTO_AGENT_CODEX_EXECUTABLE",
        "AI_PHOTO_AGENT_CODEX_MODEL",
        "AI_PHOTO_AGENT_CODEX_TIMEOUT_SECONDS",
        "AI_PHOTO_AGENT_DEEPSEEK_MODEL",
        "AI_PHOTO_AGENT_DEEPSEEK_BASE_URL",
        "AI_PHOTO_AGENT_DEEPSEEK_TIMEOUT_SECONDS",
        "AI_PHOTO_AGENT_DEEPSEEK_MAX_RETRIES",
        "AI_PHOTO_AGENT_DEEPSEEK_API_KEY",
        "AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE",
        "AI_PHOTO_AGENT_LIGHTROOM_ROOT",
        "AI_PHOTO_AGENT_OPENAI_API_KEY",
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE",
        str(tmp_path / "missing-credentials.yaml"),
    )
    default_root = tmp_path / "default"

    defaults = load_settings(default_root)

    assert defaults.data_root == default_root.resolve()
    assert defaults.max_upload_bytes == DEFAULT_MAX_UPLOAD_BYTES
    assert defaults.max_image_pixels == DEFAULT_MAX_IMAGE_PIXELS
    assert defaults.analysis_provider == "deepseek"
    assert defaults.openai_api_key is None
    assert defaults.openai_model == DEFAULT_OPENAI_MODEL
    assert defaults.openai_timeout_seconds == DEFAULT_OPENAI_TIMEOUT_SECONDS
    assert defaults.openai_max_retries == DEFAULT_OPENAI_MAX_RETRIES
    assert defaults.deepseek_api_key is None
    assert defaults.deepseek_model == DEFAULT_DEEPSEEK_MODEL
    assert defaults.deepseek_base_url == DEFAULT_DEEPSEEK_BASE_URL
    assert defaults.deepseek_timeout_seconds == DEFAULT_DEEPSEEK_TIMEOUT_SECONDS
    assert defaults.deepseek_max_retries == DEFAULT_DEEPSEEK_MAX_RETRIES
    assert defaults.codex_executable is None
    assert defaults.codex_model == DEFAULT_CODEX_MODEL
    assert defaults.codex_timeout_seconds == DEFAULT_CODEX_TIMEOUT_SECONDS

    configured_root = tmp_path / "configured"
    monkeypatch.setenv("AI_PHOTO_AGENT_DATA_DIR", str(configured_root))
    monkeypatch.setenv("AI_PHOTO_AGENT_MAX_UPLOAD_BYTES", "123")
    monkeypatch.setenv("AI_PHOTO_AGENT_MAX_IMAGE_PIXELS", "456")
    monkeypatch.setenv("AI_PHOTO_AGENT_ANALYSIS_PROVIDER", "OPENAI")
    monkeypatch.setenv("OPENAI_API_KEY", "  test-key  ")
    monkeypatch.setenv("AI_PHOTO_AGENT_OPENAI_MODEL", " gpt-test ")
    monkeypatch.setenv("AI_PHOTO_AGENT_OPENAI_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("AI_PHOTO_AGENT_OPENAI_MAX_RETRIES", "1")

    configured = load_settings(default_root)

    assert configured == AppSettings(
        data_root=configured_root.resolve(),
        max_upload_bytes=123,
        max_image_pixels=456,
        analysis_provider="openai",
        openai_api_key="test-key",
        openai_model="gpt-test",
        openai_timeout_seconds=12.5,
        openai_max_retries=1,
    )

    monkeypatch.setenv("AI_PHOTO_AGENT_DATA_DIR", "   ")
    assert load_settings(default_root).data_root == default_root.resolve()


def test_data_dir_is_a_storage_root_not_the_private_data_folder(
    monkeypatch, tmp_path
) -> None:
    # AI_PHOTO_AGENT_DATA_DIR is the storage root. FileStorage adds the
    # private "data" segment itself; setting the env var to ".../data" would
    # otherwise produce ".../data/data/sessions".
    root = tmp_path / "store"
    monkeypatch.setenv("AI_PHOTO_AGENT_DATA_DIR", str(root))

    storage = FileStorage(load_settings(tmp_path / "fallback").data_root)

    assert storage.root == root.resolve()
    assert storage.sessions_dir == (root / "data" / "sessions").resolve()
    assert storage.uploads_dir == (root / "uploads").resolve()


def test_path_settings_expand_environment_variables(monkeypatch, tmp_path) -> None:
    for name in (
        "AI_PHOTO_AGENT_DATA_DIR",
        "AI_PHOTO_AGENT_LIGHTROOM_ROOT",
        "AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE",
        "AI_PHOTO_AGENT_DEEPSEEK_API_KEY",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AIPA_TEST_ROOT", str(tmp_path))
    token = "%AIPA_TEST_ROOT%" if os.name == "nt" else "${AIPA_TEST_ROOT}"
    monkeypatch.setenv("AI_PHOTO_AGENT_DATA_DIR", f"{token}/data")
    monkeypatch.setenv("AI_PHOTO_AGENT_LIGHTROOM_ROOT", f"{token}/lightroom")

    settings = load_settings(tmp_path / "default")

    assert settings.data_root == (tmp_path / "data").resolve()
    assert settings.lightroom_root == (tmp_path / "lightroom").resolve()


def test_file_storage_expands_environment_variables(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AIPA_TEST_ROOT", str(tmp_path))
    token = "%AIPA_TEST_ROOT%" if os.name == "nt" else "${AIPA_TEST_ROOT}"

    storage = FileStorage(f"{token}/storage")

    assert storage.root == (tmp_path / "storage").resolve()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("AI_PHOTO_AGENT_MAX_UPLOAD_BYTES", "not-an-integer", "must be an integer"),
        ("AI_PHOTO_AGENT_MAX_UPLOAD_BYTES", "0", "must be positive"),
        ("AI_PHOTO_AGENT_MAX_IMAGE_PIXELS", "-1", "must be positive"),
    ],
)
def test_settings_reject_invalid_numeric_environment(
    monkeypatch,
    tmp_path,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.delenv("AI_PHOTO_AGENT_MAX_UPLOAD_BYTES", raising=False)
    monkeypatch.delenv("AI_PHOTO_AGENT_MAX_IMAGE_PIXELS", raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        load_settings(tmp_path)


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        (
            "AI_PHOTO_AGENT_ANALYSIS_PROVIDER",
            "other",
            "must be 'mock', 'openai', 'codex', or 'deepseek'",
        ),
        (
            "AI_PHOTO_AGENT_OPENAI_TIMEOUT_SECONDS",
            "slow",
            "must be a number",
        ),
        (
            "AI_PHOTO_AGENT_OPENAI_TIMEOUT_SECONDS",
            "0.5",
            "must be between 1 and 300",
        ),
        (
            "AI_PHOTO_AGENT_OPENAI_MAX_RETRIES",
            "6",
            "must be between 0 and 5",
        ),
        (
            "AI_PHOTO_AGENT_CODEX_TIMEOUT_SECONDS",
            "10",
            "must be between 30 and 900",
        ),
    ],
)
def test_settings_reject_invalid_provider_environment(
    monkeypatch,
    tmp_path,
    name: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=message):
        load_settings(tmp_path)


def test_factory_builds_openai_provider_and_requires_key(tmp_path) -> None:
    configured = AppSettings(
        data_root=tmp_path / "configured",
        analysis_provider="openai",
        openai_api_key="test-key",
        openai_model="gpt-test",
        openai_timeout_seconds=15.0,
        openai_max_retries=1,
    )

    application = create_app(settings=configured)
    provider = application.state.services.photos.analysis_provider
    learning_provider = application.state.services.learning.analysis_provider

    assert isinstance(provider, OpenAIAnalysisProvider)
    assert isinstance(learning_provider, OpenAILearningAnalysisProvider)
    assert provider.model == "gpt-test"
    assert learning_provider.model == "gpt-test"
    assert provider.timeout_seconds == 15.0
    assert provider.max_retries == 1

    missing_key = AppSettings(
        data_root=tmp_path / "missing-key",
        analysis_provider="openai",
    )
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        create_app(settings=missing_key)


def test_factory_builds_codex_provider_without_api_key(tmp_path) -> None:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"test")
    configured = AppSettings(
        data_root=tmp_path / "configured",
        analysis_provider="codex",
        codex_executable=executable,
        codex_model="gpt-test",
        codex_timeout_seconds=180.0,
    )

    application = create_app(settings=configured)
    provider = application.state.services.photos.analysis_provider
    learning_provider = application.state.services.learning.analysis_provider

    assert isinstance(provider, CodexAnalysisProvider)
    assert isinstance(learning_provider, CodexLearningAnalysisProvider)
    assert provider.executable == executable
    assert learning_provider.executable == executable
    assert provider.model == "gpt-test"
    assert learning_provider.model == "gpt-test"
    assert provider.timeout_seconds == 180.0


def test_factory_builds_deepseek_provider_and_requires_key(tmp_path) -> None:
    configured = AppSettings(
        data_root=tmp_path / "configured",
        analysis_provider="deepseek",
        deepseek_api_key="sk-test",
        deepseek_model="deepseek-flash",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_timeout_seconds=120.0,
        deepseek_max_retries=0,
    )

    application = create_app(settings=configured)
    provider = application.state.services.photos.analysis_provider
    learning_provider = application.state.services.learning.analysis_provider

    assert isinstance(provider, DeepSeekAnalysisProvider)
    assert isinstance(learning_provider, MockLearningAnalysisProvider)
    assert provider.model == "deepseek-flash"
    assert provider.base_url == "https://api.deepseek.com"
    assert provider.timeout_seconds == 120.0
    assert provider.max_retries == 0

    missing_key = AppSettings(
        data_root=tmp_path / "missing-key",
        analysis_provider="deepseek",
    )
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        create_app(settings=missing_key)


def test_explicit_credentials_file_is_used(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("AI_PHOTO_AGENT_ANALYSIS_PROVIDER", raising=False)
    monkeypatch.delenv("AI_PHOTO_AGENT_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    credentials = tmp_path / "credentials.yaml"
    credentials.write_text(
        "version: 1\nrecords:\nrefs:\n  DEEPSEEK_API_KEY: sk-from-dsh\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE", str(credentials))

    settings = load_settings(tmp_path / "data")

    assert settings.analysis_provider == "deepseek"
    assert settings.deepseek_api_key == "sk-from-dsh"
    application = create_app(settings=settings)
    assert isinstance(
        application.state.services.photos.analysis_provider,
        DeepSeekAnalysisProvider,
    )


def test_implicit_dsh_credentials_are_not_read(monkeypatch, tmp_path) -> None:
    for name in (
        "AI_PHOTO_AGENT_DEEPSEEK_API_KEY",
        "AI_PHOTO_AGENT_DEEPSEEK_CREDENTIALS_FILE",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    credentials = home / ".dsh" / ".credentials.yaml"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        "version: 1\nrecords:\nrefs:\n  DEEPSEEK_API_KEY: sk-implicit\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    settings = load_settings(tmp_path / "data")

    assert settings.deepseek_api_key is None


def test_prefixed_openai_key_takes_precedence(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AI_PHOTO_AGENT_OPENAI_API_KEY", "  prefixed-key  ")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-key")

    settings = load_settings(tmp_path)

    assert settings.openai_api_key == "prefixed-key"
