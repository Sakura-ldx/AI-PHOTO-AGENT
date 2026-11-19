from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

import app.services.storage as storage_module
from app.adapters.analysis import MockAnalysisProvider
from app.adapters.learning import MockLearningAnalysisProvider
from app.errors import ProviderServiceError, ProviderTimeoutError
from app.main import create_app
from app.services.storage import FileStorage


def make_image_upload(image_format: str = "JPEG") -> tuple[str, bytes, str]:
    buffer = BytesIO()
    Image.new("RGB", (128, 96), color=(120, 140, 160)).save(buffer, format=image_format)
    content_type = "image/jpeg" if image_format == "JPEG" else "image/png"
    suffix = "jpg" if image_format == "JPEG" else "png"
    return f"sample.{suffix}", buffer.getvalue(), content_type


def make_mpo_jpg_upload() -> tuple[str, bytes, str]:
    buffer = BytesIO()
    Image.new("RGB", (128, 96), color=(120, 140, 160)).save(buffer, format="MPO")
    return "sample.jpg", buffer.getvalue(), "application/octet-stream"


def make_client(tmp_path, **storage_options) -> tuple[TestClient, FileStorage]:
    storage = FileStorage(tmp_path, **storage_options)
    application = create_app(storage=storage, analysis_provider=MockAnalysisProvider())
    return TestClient(application), storage


def test_full_upload_analyze_preview_export_flow(tmp_path):
    client, _storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()

    upload = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    )
    assert upload.status_code == 200
    photo_id = upload.json()["photo_id"]

    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    styles = analysis.json()["recommended_styles"]
    assert len(styles) == 3
    style_id = styles[0]["id"]

    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style_id, "intensity": 0.7},
    )
    assert preview.status_code == 200
    preview_body = preview.json()
    assert preview_body["preview_url"].endswith(".jpg")
    assert preview_body["preview_is_approximate"] is True
    assert preview_body["parameters_summary"], "preview must describe the applied values"
    assert client.get(preview_body["preview_url"]).status_code == 200

    export = client.post(
        f"/api/photos/{photo_id}/export",
        json={"style_id": style_id, "intensity": 0.7},
    )
    assert export.status_code == 200
    export_body = export.json()
    assert export_body["mapping_version"] == "lightroom_xmp_v2"
    assert export_body["download_url"].endswith(".xmp")
    assert client.get(export_body["download_url"]).status_code == 200


def test_upload_rejects_invalid_format(tmp_path):
    client, _storage = make_client(tmp_path)

    response = client.post(
        "/api/photos",
        files={"file": ("notes.txt", b"not an image", "text/plain")},
    )

    assert response.status_code == 400
    assert "image file" in response.json()["detail"]


def test_jpg_upload_accepts_generic_content_type(tmp_path):
    client, _storage = make_client(tmp_path)
    filename, content, _ = make_image_upload("JPEG")

    response = client.post(
        "/api/photos",
        files={"file": (filename, content, "application/octet-stream")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["format"] == "JPEG"
    assert body["metadata"]["content_type"] == "image/jpeg"


def test_mpo_jpg_upload_is_treated_as_jpeg(tmp_path):
    client, _storage = make_client(tmp_path)
    filename, content, content_type = make_mpo_jpg_upload()

    response = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["format"] == "JPEG"
    assert body["metadata"]["content_type"] == "image/jpeg"


def test_preview_requires_existing_photo_and_valid_intensity(tmp_path):
    client, _storage = make_client(tmp_path)

    missing = client.post(
        "/api/photos/missing/preview",
        json={"style_id": "natural-documentary", "intensity": 0.7},
    )
    invalid_intensity = client.post(
        "/api/photos/missing/preview",
        json={"style_id": "natural-documentary", "intensity": 2.0},
    )

    assert missing.status_code == 404
    assert invalid_intensity.status_code == 422


def test_learning_session_create_upload_analyze_and_get_flow(tmp_path):
    client, _storage = make_client(tmp_path)

    created = client.post("/api/learning/sessions", json={"topic": "街头摄影"})
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert created.json()["topic"] == "街头摄影"

    jpg_name, jpg_content, jpg_content_type = make_image_upload("JPEG")
    first_reference = client.post(
        f"/api/learning/sessions/{session_id}/references",
        files={"file": (jpg_name, jpg_content, jpg_content_type)},
        data={
            "source_url": "https://example.com/photo",
            "author": "Sample Author",
            "license": "CC BY",
        },
    )
    assert first_reference.status_code == 200
    first_reference_body = first_reference.json()
    assert first_reference_body["metadata"]["format"] == "JPEG"
    assert first_reference_body["source_url"] == "https://example.com/photo"
    assert "image_file" not in first_reference_body
    assert client.get(first_reference_body["image_url"]).status_code == 200

    png_name, png_content, png_content_type = make_image_upload("PNG")
    second_reference = client.post(
        f"/api/learning/sessions/{session_id}/references",
        files={"file": (png_name, png_content, png_content_type)},
    )
    assert second_reference.status_code == 200
    assert second_reference.json()["metadata"]["format"] == "PNG"

    analyzed = client.post(f"/api/learning/sessions/{session_id}/analyze")
    assert analyzed.status_code == 200
    analyzed_body = analyzed.json()
    assert analyzed_body["summary"]["style_overview"].startswith("本次主题“街头摄影”")
    assert len(analyzed_body["references"]) == 2
    assert all(reference["analysis"] is not None for reference in analyzed_body["references"])

    fetched = client.get(f"/api/learning/sessions/{session_id}")
    assert fetched.status_code == 200
    assert fetched.json()["summary"]["style_overview"] == analyzed_body["summary"]["style_overview"]


def test_learning_reference_accepts_mpo_jpg(tmp_path):
    client, _storage = make_client(tmp_path)

    created = client.post("/api/learning/sessions", json={"topic": "人像光线"})
    session_id = created.json()["id"]
    filename, content, content_type = make_mpo_jpg_upload()

    response = client.post(
        f"/api/learning/sessions/{session_id}/references",
        files={"file": (filename, content, content_type)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["format"] == "JPEG"
    assert body["metadata"]["content_type"] == "image/jpeg"


def test_learning_session_missing_and_empty_analysis_errors(tmp_path):
    client, _storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()

    missing_upload = client.post(
        "/api/learning/sessions/missing/references",
        files={"file": (filename, content, content_type)},
    )
    missing_analyze = client.post("/api/learning/sessions/missing/analyze")
    created = client.post("/api/learning/sessions", json={"topic": "色彩练习"})
    empty_analyze = client.post(f"/api/learning/sessions/{created.json()['id']}/analyze")

    assert missing_upload.status_code == 404
    assert missing_analyze.status_code == 404
    assert empty_analyze.status_code == 409


def test_private_session_state_is_not_static_and_paths_are_not_in_responses(tmp_path):
    client, storage = make_client(tmp_path)
    created = client.post("/api/learning/sessions", json={"topic": "privacy boundary"})

    assert created.status_code == 200
    session_id = created.json()["id"]
    assert "image_file" not in created.text
    assert storage.learning_session_path(session_id).exists()
    assert client.get(f"/generated/learning_sessions/{session_id}.json").status_code == 404
    assert client.get(f"/generated/sessions/{session_id}.json").status_code == 404


def test_reference_source_url_must_be_http_or_https(tmp_path):
    client, _storage = make_client(tmp_path)
    created = client.post("/api/learning/sessions", json={"topic": "sources"})
    filename, content, content_type = make_image_upload()

    response = client.post(
        f"/api/learning/sessions/{created.json()['id']}/references",
        files={"file": (filename, content, content_type)},
        data={"source_url": "file:///local/reference.jpg"},
    )
    malformed = client.post(
        f"/api/learning/sessions/{created.json()['id']}/references",
        files={"file": (filename, content, content_type)},
        data={"source_url": "http://["},
    )

    invalid_authorities = []
    for source_url in ("https://user@", "https://example.com:bad", "https://exa mple.com"):
        invalid_authorities.append(
            client.post(
                f"/api/learning/sessions/{created.json()['id']}/references",
                files={"file": (filename, content, content_type)},
                data={"source_url": source_url},
            )
        )

    assert response.status_code == 400
    assert malformed.status_code == 400
    assert all(result.status_code == 400 for result in invalid_authorities)
    assert "http or https" in response.json()["detail"]


def test_openapi_declares_analysis_response_schema(tmp_path):
    client, _storage = make_client(tmp_path)

    schema = client.get("/openapi.json").json()
    response_schema = schema["paths"]["/api/photos/{photo_id}/analyze"]["post"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]

    assert response_schema["$ref"].endswith("/ImageAnalysis")
    upload_responses = schema["paths"]["/api/photos"]["post"]["responses"]
    assert upload_responses["413"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/ErrorResponse"
    )
    analysis_errors = schema["paths"]["/api/photos/{photo_id}/analyze"]["post"][
        "responses"
    ]
    learning_analysis_errors = schema["paths"][
        "/api/learning/sessions/{session_id}/analyze"
    ]["post"]["responses"]
    for status_code in ("502", "504"):
        assert analysis_errors[status_code]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/ErrorResponse")
        assert learning_analysis_errors[status_code]["content"]["application/json"][
            "schema"
        ]["$ref"].endswith("/ErrorResponse")
    learning_upload_errors = schema["paths"][
        "/api/learning/sessions/{session_id}/references"
    ]["post"]["responses"]
    assert learning_upload_errors["409"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("/ErrorResponse")
    state_endpoints = (
        ("/api/photos", "post"),
        ("/api/photos/{photo_id}/analyze", "post"),
        ("/api/photos/{photo_id}/preview", "post"),
        ("/api/photos/{photo_id}/export", "post"),
        ("/api/learning/sessions", "post"),
        ("/api/learning/sessions/{session_id}", "get"),
        ("/api/learning/sessions/{session_id}/references", "post"),
        ("/api/learning/sessions/{session_id}/analyze", "post"),
    )
    for path, method in state_endpoints:
        error_schema = schema["paths"][path][method]["responses"]["500"]["content"][
            "application/json"
        ]["schema"]
        assert error_schema["$ref"].endswith("/ErrorResponse")


def test_encoded_windows_path_segments_are_rejected_as_resource_ids(tmp_path):
    client, _storage = make_client(tmp_path)

    photo_response = client.post("/api/photos/..%5Coutside/analyze")
    learning_response = client.get("/api/learning/sessions/..%5Coutside")

    assert photo_response.status_code == 404
    assert learning_response.status_code == 404


def test_upload_limit_and_truncated_image_have_controlled_api_errors(tmp_path):
    limited_client, _storage = make_client(tmp_path / "limited", max_upload_bytes=8)
    too_large = limited_client.post(
        "/api/photos",
        files={"file": ("large.jpg", b"123456789", "image/jpeg")},
    )

    normal_client, normal_storage = make_client(tmp_path / "truncated")
    _name, jpeg_content, content_type = make_image_upload()
    truncated = normal_client.post(
        "/api/photos",
        files={"file": ("truncated.jpg", jpeg_content[:40], content_type)},
    )

    assert too_large.status_code == 413
    assert truncated.status_code == 400
    assert list(normal_storage.uploads_dir.iterdir()) == []


def test_tampered_photo_resource_id_returns_generic_storage_error(tmp_path):
    client, storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()
    first = client.post("/api/photos", files={"file": (filename, content, content_type)}).json()
    second = client.post("/api/photos", files={"file": (filename, content, content_type)}).json()
    second_session = storage.load_session(second["photo_id"])
    storage.session_path(first["photo_id"]).write_text(
        second_session.model_dump_json(indent=2),
        encoding="utf-8",
    )

    response = client.post(f"/api/photos/{first['photo_id']}/analyze")

    assert response.status_code == 500
    assert response.json() == {"detail": "Stored application data could not be read."}
    assert storage.load_session(second["photo_id"]).analysis is None


def test_tampered_reference_cannot_read_another_session_image(tmp_path):
    client, storage = make_client(tmp_path)
    first_session = client.post("/api/learning/sessions", json={"topic": "first"}).json()
    second_session = client.post("/api/learning/sessions", json={"topic": "second"}).json()
    filename, content, content_type = make_image_upload()
    first_reference = client.post(
        f"/api/learning/sessions/{first_session['id']}/references",
        files={"file": (filename, content, content_type)},
    ).json()
    second_reference = client.post(
        f"/api/learning/sessions/{second_session['id']}/references",
        files={"file": (filename, content, content_type)},
    ).json()
    first_state = storage.load_learning_session(first_session["id"])
    second_state = storage.load_learning_session(second_session["id"])
    first_state.references[0].image_file = second_state.references[0].image_file
    storage.save_learning_session(first_state)

    response = client.get(first_reference["image_url"])

    assert first_reference["id"] != second_reference["id"]
    assert response.status_code == 500
    assert response.json() == {"detail": "Stored application data could not be read."}


def test_photo_provider_receives_the_current_existing_original_path(tmp_path):
    class SpyProvider(MockAnalysisProvider):
        seen_path: Path | None = None

        def analyze(self, image_path, metadata):
            self.seen_path = image_path
            assert image_path.is_file()
            return super().analyze(image_path, metadata)

    storage = FileStorage(tmp_path)
    provider = SpyProvider()
    client = TestClient(create_app(storage=storage, analysis_provider=provider))
    filename, content, content_type = make_image_upload()
    uploaded = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    ).json()

    response = client.post(f"/api/photos/{uploaded['photo_id']}/analyze")

    assert response.status_code == 200
    assert provider.seen_path == storage.load_session(uploaded["photo_id"]).original_file


def test_invalid_photo_provider_output_is_rejected_before_persistence(tmp_path):
    class InvalidStyleProvider(MockAnalysisProvider):
        def analyze(self, image_path, metadata):
            analysis = super().analyze(image_path, metadata)
            analysis.recommended_styles[0].name = "bad\x00name"
            return analysis

    storage = FileStorage(tmp_path)
    client = TestClient(create_app(storage=storage, analysis_provider=InvalidStyleProvider()))
    filename, content, content_type = make_image_upload()
    uploaded = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    ).json()

    response = client.post(f"/api/photos/{uploaded['photo_id']}/analyze")

    assert response.status_code == 500
    assert response.json() == {"detail": "Analysis provider returned invalid data."}
    assert storage.load_session(uploaded["photo_id"]).analysis is None


def test_photo_provider_failures_are_explicit_and_do_not_fallback_to_mock(tmp_path):
    class FailingProvider(MockAnalysisProvider):
        def __init__(self, error) -> None:
            self.error = error

        def analyze(self, image_path, metadata):
            raise self.error

    filename, content, content_type = make_image_upload()
    cases = (
        (
            ProviderServiceError("upstream unavailable"),
            502,
            {"detail": "Analysis provider request failed."},
        ),
        (
            ProviderTimeoutError("upstream timed out"),
            504,
            {"detail": "Analysis provider request timed out."},
        ),
    )
    for error, expected_status, expected_body in cases:
        storage = FileStorage(tmp_path / str(expected_status))
        client = TestClient(
            create_app(
                storage=storage,
                analysis_provider=FailingProvider(error),
            )
        )
        uploaded = client.post(
            "/api/photos",
            files={"file": (filename, content, content_type)},
        ).json()

        response = client.post(f"/api/photos/{uploaded['photo_id']}/analyze")

        assert response.status_code == expected_status
        assert response.json() == expected_body
        assert storage.load_session(uploaded["photo_id"]).analysis is None


def test_learning_provider_cannot_change_identity_order_or_return_empty_analysis(tmp_path):
    class InvalidLearningProvider(MockLearningAnalysisProvider):
        def __init__(self, mode: str) -> None:
            self.mode = mode

        def analyze(self, session):
            if self.mode == "incomplete":
                return session
            analyzed = super().analyze(session)
            if self.mode == "identity":
                changed = analyzed.references[0].model_copy(
                    update={"reference_id": "f" * 32},
                    deep=True,
                )
                return analyzed.model_copy(
                    update={"references": [changed, *analyzed.references[1:]]},
                    deep=True,
                )
            return analyzed.model_copy(
                update={"references": list(reversed(analyzed.references))},
                deep=True,
            )

    filename, content, content_type = make_image_upload()
    for mode in ("identity", "order", "incomplete"):
        storage = FileStorage(tmp_path / mode)
        client = TestClient(
            create_app(
                storage=storage,
                learning_provider=InvalidLearningProvider(mode),
            )
        )
        created = client.post(
            "/api/learning/sessions",
            json={"topic": f"invalid provider {mode}"},
        ).json()
        for _ in range(2):
            uploaded = client.post(
                f"/api/learning/sessions/{created['id']}/references",
                files={"file": (filename, content, content_type)},
            )
            assert uploaded.status_code == 200

        response = client.post(f"/api/learning/sessions/{created['id']}/analyze")

        assert response.status_code == 500
        assert response.json() == {"detail": "Analysis provider returned invalid data."}
        persisted = storage.load_learning_session(created["id"])
        assert persisted.summary is None
        assert all(reference.analysis is None for reference in persisted.references)


def test_learning_provider_service_and_timeout_errors_keep_http_semantics(tmp_path):
    class FailingLearningProvider(MockLearningAnalysisProvider):
        def __init__(self, error: Exception) -> None:
            self.error = error

        def analyze(self, session):
            del session
            raise self.error

    filename, content, content_type = make_image_upload()
    cases = (
        (
            ProviderServiceError("upstream unavailable"),
            502,
            {"detail": "Analysis provider request failed."},
        ),
        (
            ProviderTimeoutError("upstream timed out"),
            504,
            {"detail": "Analysis provider request timed out."},
        ),
    )
    for error, expected_status, expected_body in cases:
        storage = FileStorage(tmp_path / str(expected_status))
        client = TestClient(
            create_app(
                storage=storage,
                learning_provider=FailingLearningProvider(error),
            )
        )
        created = client.post(
            "/api/learning/sessions",
            json={"topic": "provider errors"},
        ).json()
        uploaded = client.post(
            f"/api/learning/sessions/{created['id']}/references",
            files={"file": (filename, content, content_type)},
        )
        assert uploaded.status_code == 200

        response = client.post(f"/api/learning/sessions/{created['id']}/analyze")

        assert response.status_code == expected_status
        assert response.json() == expected_body
        persisted = storage.load_learning_session(created["id"])
        assert persisted.summary is None
        assert all(reference.analysis is None for reference in persisted.references)


def test_learning_upload_rejects_ninth_reference(tmp_path):
    storage = FileStorage(tmp_path)
    client = TestClient(create_app(storage=storage))
    created = client.post(
        "/api/learning/sessions",
        json={"topic": "bounded session"},
    ).json()
    filename, content, content_type = make_image_upload()
    for _ in range(8):
        uploaded = client.post(
            f"/api/learning/sessions/{created['id']}/references",
            files={"file": (filename, content, content_type)},
        )
        assert uploaded.status_code == 200

    rejected = client.post(
        f"/api/learning/sessions/{created['id']}/references",
        files={"file": (filename, content, content_type)},
    )

    assert rejected.status_code == 409
    assert "at most 8" in rejected.json()["detail"]
    persisted = storage.load_learning_session(created["id"])
    assert len(persisted.references) == 8


def test_learning_analysis_rejects_legacy_oversized_session_before_provider_call(
    monkeypatch,
    tmp_path,
):
    class CountingLearningProvider(MockLearningAnalysisProvider):
        def __init__(self) -> None:
            self.calls = 0

        def analyze(self, session):
            self.calls += 1
            return super().analyze(session)

    provider = CountingLearningProvider()
    storage = FileStorage(tmp_path)
    client = TestClient(create_app(storage=storage, learning_provider=provider))
    created = client.post(
        "/api/learning/sessions",
        json={"topic": "bounded batch"},
    ).json()
    filename, content, content_type = make_image_upload()
    uploaded = client.post(
        f"/api/learning/sessions/{created['id']}/references",
        files={"file": (filename, content, content_type)},
    )
    assert uploaded.status_code == 200
    stored = storage.load_learning_session(created["id"])
    template = stored.references[0]
    oversized = stored.model_copy(
        update={
            "references": [
                template.model_copy(update={"id": f"{index + 1:032x}"}, deep=True)
                for index in range(9)
            ]
        },
        deep=True,
    )
    monkeypatch.setattr(storage, "load_learning_session", lambda _session_id: oversized)

    response = client.post(f"/api/learning/sessions/{created['id']}/analyze")

    assert response.status_code == 409
    assert "at most 8" in response.json()["detail"]
    assert provider.calls == 0


def test_storage_write_failure_returns_json_and_cleans_temp_file(monkeypatch, tmp_path):
    client, storage = make_client(tmp_path)

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("injected disk failure")

    monkeypatch.setattr(storage_module.os, "replace", fail_replace)

    response = client.post("/api/learning/sessions", json={"topic": "disk failure"})

    assert response.status_code == 500
    assert response.json() == {"detail": "Application data could not be persisted."}
    assert list(storage.data_dir.rglob("*.tmp")) == []


def test_analysis_job_endpoints_report_progress(tmp_path):
    import time

    client, storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()
    upload = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    )
    photo_id = upload.json()["photo_id"]

    created = client.post(f"/api/photos/{photo_id}/analyze/jobs")
    assert created.status_code == 201
    body = created.json()
    assert body["photo_id"] == photo_id
    assert body["stage"] == "queued"
    task_id = body["task_id"]

    deadline = time.monotonic() + 10.0
    payload = None
    while time.monotonic() < deadline:
        polled = client.get(f"/api/photos/{photo_id}/analyze/jobs/{task_id}")
        assert polled.status_code == 200
        payload = polled.json()
        assert payload["stage"] in {
            "queued",
            "measuring",
            "provider",
            "validating",
            "succeeded",
            "failed",
        }
        if payload["stage"] in {"succeeded", "failed"}:
            break
        time.sleep(0.02)

    assert payload is not None
    assert payload["stage"] == "succeeded", payload
    assert payload["analysis"] is not None
    assert len(payload["analysis"]["recommended_styles"]) == 3
    assert payload["error"] is None
    assert payload["elapsed_seconds"] >= 0.0
    assert storage.load_session(photo_id).analysis is not None


def test_analysis_job_accepts_any_body_shape(tmp_path):
    # Lightroom's Lua JSON encoder turns an empty table into `[]`, and a strict
    # body model used to answer 422 — which made the plug-in fall back to the
    # blocking endpoint and lose all progress reporting.
    client, _ = make_client(tmp_path)
    filename, content, content_type = make_image_upload()
    upload = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    )
    photo_id = upload.json()["photo_id"]

    for payload in ([], {"provider": "mock"}, None, "ignore-me"):
        response = client.post(
            f"/api/photos/{photo_id}/analyze/jobs",
            json=payload,
        )
        assert response.status_code == 201, (payload, response.text)
        assert response.json()["stage"] == "queued"


def test_analysis_job_rejects_unknown_photo(tmp_path):
    client, _storage = make_client(tmp_path)
    response = client.post("/api/photos/does-not-exist/analyze/jobs")
    assert response.status_code == 404


def test_analysis_job_status_requires_matching_photo(tmp_path):
    client, _storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()
    first = client.post("/api/photos", files={"file": (filename, content, content_type)})
    second = client.post("/api/photos", files={"file": (filename, content, content_type)})
    task_id = client.post(f"/api/photos/{first.json()['photo_id']}/analyze/jobs").json()["task_id"]

    mismatch = client.get(f"/api/photos/{second.json()['photo_id']}/analyze/jobs/{task_id}")
    assert mismatch.status_code == 404

    walk_in = client.get(f"/api/photos/{first.json()['photo_id']}/analyze/jobs/unknown-task")
    assert walk_in.status_code == 404


def test_analysis_job_cancel_endpoint(tmp_path):
    client, _storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()
    photo_id = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    ).json()["photo_id"]
    task_id = client.post(f"/api/photos/{photo_id}/analyze/jobs").json()["task_id"]

    cancelled = client.post(f"/api/photos/{photo_id}/analyze/jobs/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["stage"] in {"cancelled", "succeeded"}
