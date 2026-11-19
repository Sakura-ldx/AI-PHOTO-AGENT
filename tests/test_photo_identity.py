"""LR-02 photo-identity tests: the upload filename survives to the bridge job.

The Lightroom plug-in needs a human-meaningful, path-free identity for the
selected photo so it can refuse to apply a queued job to the wrong photo. These
tests pin the cross-layer contract: upload -> session JSON -> develop response ->
bridge inbox job payload, including sanitization and backward compatibility with
sessions written before the field existed.
"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from app.adapters.analysis import MockAnalysisProvider
from app.config import AppSettings
from app.main import create_app
from app.services.lightroom_bridge import BRIDGE_DIR_NAME
from app.services.storage import _sanitize_original_filename


def jpeg_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (128, 96), color=(120, 140, 160)).save(buffer, format="JPEG")
    return buffer.getvalue()


def make_client(tmp_path: Path) -> tuple[TestClient, AppSettings]:
    settings = AppSettings(
        data_root=tmp_path / "appdata",
        lightroom_root=tmp_path / "lightroom",
    )
    application = create_app(
        settings=settings,
        analysis_provider=MockAnalysisProvider(),
    )
    return TestClient(application), settings


def upload(client: TestClient, filename: str) -> str:
    response = client.post(
        "/api/photos",
        files={"file": (filename, jpeg_bytes(), "image/jpeg")},
    )
    assert response.status_code == 200, response.text
    return response.json()["photo_id"]


def analyze(client: TestClient, photo_id: str) -> str:
    response = client.post(f"/api/photos/{photo_id}/analyze")
    assert response.status_code == 200, response.text
    return response.json()["recommended_styles"][0]["id"]


def session_json_path(settings: AppSettings, photo_id: str) -> Path:
    return settings.data_root / "data" / "sessions" / f"{photo_id}.json"


def queued_photo_payload(settings: AppSettings) -> dict:
    inbox = settings.lightroom_root / BRIDGE_DIR_NAME / "inbox"
    jobs = sorted(inbox.glob("*.json"))
    assert jobs, "no bridge job was enqueued"
    payload = json.loads(jobs[-1].read_text(encoding="utf-8"))
    assert payload["schema_version"] == "aipa_lightroom_job_v1"
    return payload["photo"]


def test_upload_records_sanitized_original_filename(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)

    photo_id = upload(client, "C:\\fakepath\\IMG_0042.CR3")

    session_path = session_json_path(settings, photo_id)
    stored = json.loads(session_path.read_text(encoding="utf-8"))
    # Only the basename is persisted; the client path prefix is dropped.
    assert stored["original_filename"] == "IMG_0042.CR3"

    develop = client.post(
        f"/api/photos/{photo_id}/develop",
        json={"style_id": analyze(client, photo_id), "intensity": 0.5},
    )
    assert develop.status_code == 200
    assert develop.json()["original_filename"] == "IMG_0042.CR3"


def test_original_filename_sanitizer_handles_hostile_input() -> None:
    assert _sanitize_original_filename("C:\\Users\\evil\\..\\photo.jpg") == "photo.jpg"
    assert _sanitize_original_filename("../../etc/passwd") == "passwd"
    assert _sanitize_original_filename("bad\x00name\x1f.jpg") == "badname.jpg"
    assert _sanitize_original_filename("  spaced name.jpg  ") == "spaced name.jpg"
    assert _sanitize_original_filename("x" * 300) == "x" * 255
    assert _sanitize_original_filename("") is None
    assert _sanitize_original_filename("   ") is None
    assert _sanitize_original_filename("dir/") is None
    assert _sanitize_original_filename("..") is None
    assert _sanitize_original_filename(None) is None
    assert _sanitize_original_filename(123) is None  # type: ignore[arg-type]


def test_job_payload_carries_the_original_filename(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    photo_id = upload(client, "holiday-portrait.jpg")
    style_id = analyze(client, photo_id)

    sent = client.post(
        f"/api/photos/{photo_id}/lightroom",
        json={"style_id": style_id, "intensity": 1.0, "install_plugin": False},
    )
    assert sent.status_code == 200, sent.text

    photo = queued_photo_payload(settings)
    assert photo["photo_id"] == photo_id
    assert photo["original_filename"] == "holiday-portrait.jpg"
    # Never leak a local path through the job payload.
    assert "\\" not in photo["original_filename"]
    assert "/" not in photo["original_filename"]


def test_job_payload_original_filename_is_null_when_unknown(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    # A browser/CLI without a usable filename: whitespace is sanitized to None.
    photo_id = upload(client, "   ")
    style_id = analyze(client, photo_id)

    session = json.loads(session_json_path(settings, photo_id).read_text(encoding="utf-8"))
    assert session["original_filename"] is None

    sent = client.post(
        f"/api/photos/{photo_id}/lightroom",
        json={"style_id": style_id, "intensity": 1.0, "install_plugin": False},
    )
    assert sent.status_code == 200, sent.text

    photo = queued_photo_payload(settings)
    assert photo["original_filename"] is None


def test_legacy_session_without_the_field_still_loads(tmp_path: Path) -> None:
    client, settings = make_client(tmp_path)
    photo_id = upload(client, "legacy.jpg")

    path = session_json_path(settings, photo_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("original_filename", None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    # The application can still load and analyze the legacy session.
    response = client.post(f"/api/photos/{photo_id}/analyze")
    assert response.status_code == 200
    style_id = response.json()["recommended_styles"][0]["id"]
    develop = client.post(
        f"/api/photos/{photo_id}/develop",
        json={"style_id": style_id, "intensity": 1.0},
    )
    assert develop.status_code == 200
    assert develop.json()["original_filename"] is None
