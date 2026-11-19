"""Regression tests for the backend review findings B01-B15.

Each test targets one externally observable contract that the review found
broken (or untested) and that the corresponding fix in ``app/`` restores.
"""

from __future__ import annotations

import json
import os
import threading
import time
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.adapters import deepseek_analysis as deepseek_module
from app.adapters.analysis import MockAnalysisProvider
from app.adapters.deepseek_analysis import DeepSeekAnalysisProvider
from app.config import AppSettings
from app.launcher import server_url
from app.main import create_app
from app.schemas import (
    AdvancedParameters,
    EditingParametersV1,
    EditingStyle,
    build_advanced_from_overrides,
)
from app.services.analysis_tasks import AnalysisTaskRegistry
from app.services.lightroom_develop import build_lightroom_develop_settings
from app.services.lightroom_bridge import LightroomBridge
from app.services.parameter_specs import (
    SPECS_BY_KEY,
    encode_for_lightroom,
    is_neutral,
)
from app.services.storage import FileStorage
from app.services.xmp import CAMERA_RAW_NS, RDF_NS, build_xmp_preset


def make_image_upload(image_format: str = "JPEG") -> tuple[str, bytes, str]:
    buffer = BytesIO()
    Image.new("RGB", (128, 96), color=(120, 140, 160)).save(buffer, format=image_format)
    content_type = "image/jpeg" if image_format == "JPEG" else "image/png"
    suffix = "jpg" if image_format == "JPEG" else "png"
    return f"sample.{suffix}", buffer.getvalue(), content_type


def make_client(tmp_path: Path, **storage_options) -> tuple[TestClient, FileStorage]:
    storage = FileStorage(tmp_path, **storage_options)
    application = create_app(storage=storage, analysis_provider=MockAnalysisProvider())
    return TestClient(application), storage


def upload_photo(client: TestClient) -> str:
    filename, content, content_type = make_image_upload()
    response = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    )
    assert response.status_code == 200
    return response.json()["photo_id"]


def wait_until(predicate, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met in time")


# ---------------------------------------------------------------------------
# B01: cancelled analyses still count against an in-flight admission cap
# ---------------------------------------------------------------------------


def test_registry_bounds_total_threads_even_when_tasks_are_cancelled() -> None:
    registry = AnalysisTaskRegistry(max_workers=1, max_in_flight=2)
    release = threading.Event()

    def slow(_report, _cancelled):
        release.wait(5.0)
        return None

    first = registry.submit("a" * 32, slow)
    registry.cancel(first.task_id)
    second = registry.submit("b" * 32, slow)
    registry.cancel(second.task_id)
    assert registry.in_flight == 2

    third = registry.submit("c" * 32, slow)
    assert third.stage == "failed"
    assert third.error is not None and "过多" in third.error

    release.set()
    wait_until(lambda: registry.in_flight == 0)

    fourth = registry.submit("d" * 32, lambda _report, _cancelled: None)
    wait_until(lambda: registry.get(fourth.task_id).finished_at is not None)
    assert registry.get(fourth.task_id).stage == "succeeded"
    registry.shutdown()


# ---------------------------------------------------------------------------
# B15: FastAPI lifespan shuts the registry down
# ---------------------------------------------------------------------------


class _RecordingRegistry(AnalysisTaskRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_called = False

    def shutdown(self) -> None:
        self.shutdown_called = True
        super().shutdown()


def test_app_lifespan_shuts_down_the_analysis_registry(tmp_path: Path) -> None:
    registry = _RecordingRegistry()
    storage = FileStorage(tmp_path)
    application = create_app(
        storage=storage,
        analysis_provider=MockAnalysisProvider(),
        analysis_tasks=registry,
    )
    with TestClient(application):
        pass
    assert registry.shutdown_called is True


# ---------------------------------------------------------------------------
# B02: launcher publishes the real server URL for Lightroom bridge jobs
# ---------------------------------------------------------------------------


def test_server_url_uses_the_loopback_alias_for_wildcard_hosts() -> None:
    assert server_url("0.0.0.0", 8123) == "http://127.0.0.1:8123"
    assert server_url("127.0.0.1", 8123) == "http://127.0.0.1:8123"
    assert server_url("::1", 8000) == "http://[::1]:8000"


def test_launcher_publishes_the_configured_port(monkeypatch) -> None:
    import app.launcher as launcher

    monkeypatch.delenv("AI_PHOTO_AGENT_SERVER_URL", raising=False)
    calls: dict[str, object] = {}
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *a, **k: calls.update(k))
    launcher.main(["--host", "0.0.0.0", "--port", "8123", "--no-browser"])

    assert os.environ["AI_PHOTO_AGENT_SERVER_URL"] == "http://127.0.0.1:8123"
    assert calls["port"] == 8123


def test_launcher_keeps_an_explicit_server_url(monkeypatch) -> None:
    import app.launcher as launcher

    monkeypatch.setenv("AI_PHOTO_AGENT_SERVER_URL", "http://127.0.0.1:9999")
    monkeypatch.setattr(launcher.uvicorn, "run", lambda *a, **k: None)
    launcher.main(["--host", "127.0.0.1", "--port", "8123", "--no-browser"])

    assert os.environ["AI_PHOTO_AGENT_SERVER_URL"] == "http://127.0.0.1:9999"


def test_create_app_can_inject_the_bridge_server_url(tmp_path: Path) -> None:
    application = create_app(
        storage=FileStorage(tmp_path),
        analysis_provider=MockAnalysisProvider(),
        server_url="http://127.0.0.1:8123",
    )
    with TestClient(application) as client:
        body = client.get("/api/lightroom/status").json()
    assert body["server_url"] == "http://127.0.0.1:8123"


def test_bridge_server_url_prefers_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("AI_PHOTO_AGENT_SERVER_URL", "http://127.0.0.1:9100/")
    bridge = LightroomBridge(settings=AppSettings(data_root=Path("unused")))
    assert bridge.server_url == "http://127.0.0.1:9100"


# ---------------------------------------------------------------------------
# B03: cross-origin browser writes are rejected, local ones keep working
# ---------------------------------------------------------------------------


def test_cross_origin_writes_are_rejected(tmp_path: Path) -> None:
    client, _storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()

    blocked = client.post(
        "/api/photos",
        headers={"Origin": "https://evil.example"},
        files={"file": (filename, content, content_type)},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Cross-origin requests are not allowed."

    allowed = client.post(
        "/api/photos",
        headers={"Origin": "http://127.0.0.1:8000"},
        files={"file": (filename, content, content_type)},
    )
    assert allowed.status_code == 200

    # Requests without an Origin header stay allowed (plugin, curl, tests).
    no_origin = client.post(
        "/api/photos", files={"file": (filename, content, content_type)}
    )
    assert no_origin.status_code == 200

    # The guard only covers unsafe methods.
    status = client.get(
        "/api/lightroom/status", headers={"Origin": "https://evil.example"}
    )
    assert status.status_code == 200


# ---------------------------------------------------------------------------
# B06: oversized bodies are rejected before Starlette spools multipart
# ---------------------------------------------------------------------------


def test_declared_oversized_body_is_rejected_before_spooling(tmp_path: Path) -> None:
    client, _storage = make_client(tmp_path, max_upload_bytes=1024)
    response = client.post(
        "/api/photos",
        headers={"Content-Length": str(10**9)},
        content=b"",
    )
    assert response.status_code == 413
    assert "exceeds" in response.json()["detail"]


# ---------------------------------------------------------------------------
# B07: fractional perspective rotation survives encoding and export
# ---------------------------------------------------------------------------


def test_perspective_rotate_keeps_fractional_values() -> None:
    spec = SPECS_BY_KEY["perspective_rotate"]
    assert spec.kind == "number"
    assert encode_for_lightroom(spec, 2.7) == 2.7
    assert encode_for_lightroom(spec, 0.9) == 0.9
    assert is_neutral(spec, 0.9) is False
    assert is_neutral(spec, 0.04) is True

    parameters = EditingParametersV1(
        advanced=AdvancedParameters(perspective_rotate=0.9)
    )
    settings = build_lightroom_develop_settings(parameters)
    assert settings["PerspectiveRotate"] == 0.9

    preset = build_xmp_preset("rotation", parameters)
    description = ElementTree.fromstring(preset).find(
        f".//{{{RDF_NS}}}Description"
    )
    assert description is not None
    assert description.attrib[f"{{{CAMERA_RAW_NS}}}PerspectiveRotate"] == "0.9"


# ---------------------------------------------------------------------------
# B09 was invalidated during implementation: ``ColorGradingWheel.saturation``
# is constrained to ``ge=0`` in app/schemas.py, so the "negative saturation is a
# no-op" branch is unreachable from the API. No renderer change was kept.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B10: hostile curve overrides degrade instead of raising
# ---------------------------------------------------------------------------


def test_build_advanced_from_overrides_never_raises_for_garbage() -> None:
    payload = build_advanced_from_overrides(
        [
            {"key": "curve_red", "points": [["a", "b"]]},
            {"key": "curve_red", "points": [[None, 3]]},
            {"key": "curve_blue", "points": 5},
            {"key": "curve_green", "points": [[1.5, 2.2], {"x": "7", "y": None}]},
            {"key": "curve_green", "points": [[-40, 900]]},
            {"key": "perspective_rotate", "value": "not-a-number"},
            {"key": "does_not_exist", "value": 3},
        ]
    )

    assert payload["black_and_white"] is False
    # Invalid curves are dropped entirely instead of raising.
    assert payload["curve_red"] is None
    assert payload["curve_blue"] is None
    assert payload["curve_green"] is None
    AdvancedParameters.model_validate(payload)


def test_build_advanced_from_overrides_clamps_valid_curve_points() -> None:
    payload = build_advanced_from_overrides(
        [{"key": "curve_green", "points": [[-40, 900], [300, -5]]}]
    )

    curve = payload["curve_green"]
    assert [(point.x, point.y) for point in curve] == [(0, 255), (255, 0)]


# ---------------------------------------------------------------------------
# B11: analysis style names may not contain XML-illegal characters
# ---------------------------------------------------------------------------


def test_style_name_rejects_xml_illegal_control_characters() -> None:
    from pydantic import ValidationError

    payload = {
        "id": "clean-look",
        "name": "bad\x00name",
        "description": "desc",
        "best_fit_reason": "reason",
        "parameters": {},
        "expected_visual_impact": "impact",
        "rationale": "rationale",
    }
    with pytest.raises(ValidationError):
        EditingStyle.model_validate(payload)


# ---------------------------------------------------------------------------
# B13/B14: analysis job endpoints validate before spawning threads
# ---------------------------------------------------------------------------


def test_analysis_job_rejects_unknown_provider_with_400(tmp_path: Path) -> None:
    client, _storage = make_client(tmp_path)
    photo_id = upload_photo(client)

    response = client.post(
        f"/api/photos/{photo_id}/analyze/jobs", json={"provider": "bogus"}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported analysis provider."

    accepted = client.post(
        f"/api/photos/{photo_id}/analyze/jobs", json={"provider": "mock"}
    )
    assert accepted.status_code == 201
    task_id = accepted.json()["task_id"]
    wait_until(lambda: client.get(
        f"/api/photos/{photo_id}/analyze/jobs/{task_id}"
    ).json()["stage"] == "succeeded")


def test_unknown_analysis_task_returns_404(tmp_path: Path) -> None:
    client, _storage = make_client(tmp_path)
    photo_id = upload_photo(client)
    missing = "0" * 32

    read = client.get(f"/api/photos/{photo_id}/analyze/jobs/{missing}")
    assert read.status_code == 404

    cancel = client.post(f"/api/photos/{photo_id}/analyze/jobs/{missing}/cancel")
    assert cancel.status_code == 404
    assert cancel.json()["detail"] == "Analysis task was not found."


# ---------------------------------------------------------------------------
# B04: documented response contract - asset responses hide local paths, the
# Lightroom status response exposes them on purpose (the plug-in displays them)
# ---------------------------------------------------------------------------


def test_asset_responses_do_not_leak_local_paths(tmp_path: Path) -> None:
    client, _storage = make_client(tmp_path)
    photo_id = upload_photo(client)
    analysis = client.post(f"/api/photos/{photo_id}/analyze").json()
    style_id = analysis["recommended_styles"][0]["id"]

    preview = client.post(
        f"/api/photos/{photo_id}/preview", json={"style_id": style_id}
    )
    export = client.post(
        f"/api/photos/{photo_id}/export", json={"style_id": style_id}
    )
    develop = client.post(
        f"/api/photos/{photo_id}/develop", json={"style_id": style_id}
    )
    assert preview.status_code == export.status_code == develop.status_code == 200

    root = str(tmp_path.resolve())
    for body in (analysis, preview.json(), export.json(), develop.json()):
        assert root not in json.dumps(body, ensure_ascii=False)

    lightroom = client.get("/api/lightroom/status").json()
    # Contract: the bridge status intentionally exposes local directories and
    # the callback URL; it is loopback-only metadata consumed by the plug-in.
    assert "lightroom_root" in lightroom
    assert "server_url" in lightroom


# ---------------------------------------------------------------------------
# B26: DeepSeek retries back off and honour Retry-After
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url: str, *, headers: dict, json: dict):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self._responses.pop(0)


def _make_deepseek_provider(client: _FakeClient, *, max_retries: int) -> DeepSeekAnalysisProvider:
    return DeepSeekAnalysisProvider(
        api_key="sk-test",
        model="deepseek-flash",
        base_url="https://api.deepseek.com",
        timeout_seconds=30.0,
        max_retries=max_retries,
        client=client,
    )


def test_deepseek_retry_honours_retry_after(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(deepseek_module.time, "sleep", sleeps.append)
    client = _FakeClient(
        [
            _FakeResponse(429, headers={"Retry-After": "1.5"}),
            _FakeResponse(200),
        ]
    )

    response = _make_deepseek_provider(client, max_retries=1)._post_with_retries({})

    assert response.status_code == 200
    assert sleeps == [1.5]


def test_deepseek_retry_backs_off_exponentially(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(deepseek_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(deepseek_module.random, "uniform", lambda _low, _high: 0.0)
    client = _FakeClient([_FakeResponse(503), _FakeResponse(500), _FakeResponse(200)])

    response = _make_deepseek_provider(client, max_retries=2)._post_with_retries({})

    assert response.status_code == 200
    assert sleeps == [0.5, 1.0]


def test_deepseek_backoff_is_capped(monkeypatch) -> None:
    monkeypatch.setattr(deepseek_module.random, "uniform", lambda _low, _high: 0.0)
    response = _FakeResponse(500, headers={"Retry-After": "900"})
    assert deepseek_module._retry_delay_seconds(response, 5) == (
        deepseek_module.MAX_RETRY_DELAY_SECONDS
    )
