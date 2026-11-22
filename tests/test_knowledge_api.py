from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app.adapters.analysis import MockAnalysisProvider
from app.errors import StorageOperationError
from app.main import create_app
from app.services import workflows as workflows_module
from app.services.storage import FileStorage
from app.services.workflows import PhotoWorkflow


def make_image_upload() -> tuple[str, bytes, str]:
    buffer = BytesIO()
    Image.new("RGB", (128, 96), color=(120, 140, 160)).save(
        buffer,
        format="JPEG",
    )
    return "sample.jpg", buffer.getvalue(), "image/jpeg"


def make_client(tmp_path) -> tuple[TestClient, FileStorage]:
    storage = FileStorage(tmp_path)
    application = create_app(storage=storage, analysis_provider=MockAnalysisProvider())
    return TestClient(application), storage


def upload_and_analyze(client: TestClient) -> str:
    filename, content, content_type = make_image_upload()
    upload = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    )
    assert upload.status_code == 200
    photo_id = upload.json()["photo_id"]
    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    return photo_id


def test_analyze_automatically_builds_learning_summary(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)

    summary = client.get(f"/api/knowledge/summaries/{photo_id}")
    assert summary.status_code == 200
    body = summary.json()
    assert body["id"] == photo_id
    assert body["photo_id"] == photo_id
    assert body["version"] == "photo_learning_summary_v1"
    assert body["image"]["display_url"] == f"/api/photos/{photo_id}/display"
    assert body["image"]["format"] == "JPEG"
    assert body["analysis"]["overall_summary"]
    assert body["primary_category"]
    assert isinstance(body["tags"], list)
    assert len(body["directions"]) == 3
    assert len({direction["style_id"] for direction in body["directions"]}) == 3
    assert body["overview"]
    assert "归类为" in body["overview"]
    for direction in body["directions"]:
        assert direction["style_name"]
        assert 0.3 <= direction["recommended_intensity"] <= 1.0
        assert direction["intensity_reason"]
        assert direction["parameters_summary"]
        assert direction["preview_url"]
        assert direction["preview_intensity"] == direction["recommended_intensity"]
        preview = client.get(direction["preview_url"])
        assert preview.status_code == 200
        assert preview.headers["content-type"] == "image/jpeg"


def test_knowledge_list_and_overview_cover_analyzed_photos(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)

    listing = client.get("/api/knowledge/summaries")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["photo_id"] == photo_id
    assert item["image"]["display_url"] == f"/api/photos/{photo_id}/display"
    assert item["primary_category"]
    assert len(item["style_names"]) == 3
    assert len(item["recommended_intensities"]) == 3
    assert item["preview_url"]
    assert body["facets"], "analyzed mock photo must produce at least one facet"
    assert body["primary_categories"], "primary categories must be aggregated"

    overview = client.get("/api/knowledge/overview")
    assert overview.status_code == 200
    overview_body = overview.json()
    assert overview_body["total_summaries"] == 1
    assert overview_body["covered_categories"] == len(body["primary_categories"])
    assert overview_body["average_intensity"] is not None
    assert 0.3 <= overview_body["average_intensity"] <= 1.0
    assert overview_body["latest_generated_at"]


def test_knowledge_list_filters_by_tag_and_category(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)

    listing = client.get("/api/knowledge/summaries").json()
    facet = listing["facets"][0]
    tag = facet["tags"][0]
    filtered = client.get(
        "/api/knowledge/summaries",
        params={"dimension": facet["dimension"], "tag": tag["label"]},
    )
    assert filtered.status_code == 200
    filtered_body = filtered.json()
    assert filtered_body["total"] >= 1
    assert filtered_body["items"][0]["photo_id"] == photo_id
    assert any(
        item["dimension"] == facet["dimension"] and item["label"] == tag["label"]
        for item in filtered_body["items"][0]["tags"]
    )
    # Facets always describe the full knowledge base, not only the page.
    assert len(filtered_body["facets"]) == len(listing["facets"])

    category = listing["primary_categories"][0]["category"]
    by_category = client.get("/api/knowledge/summaries", params={"category": category})
    assert by_category.status_code == 200
    assert by_category.json()["total"] >= 1

    missing_category = client.get(
        "/api/knowledge/summaries",
        params={"category": "不存在的分类"},
    )
    assert missing_category.status_code == 200
    assert missing_category.json()["total"] == 0
    assert missing_category.json()["items"] == []


def test_knowledge_list_rejects_invalid_filters(tmp_path):
    client, _storage = make_client(tmp_path)

    bad_dimension = client.get(
        "/api/knowledge/summaries",
        params={"dimension": "mood", "tag": "x"},
    )
    assert bad_dimension.status_code == 400

    missing_tag = client.get(
        "/api/knowledge/summaries",
        params={"dimension": "subject"},
    )
    assert missing_tag.status_code == 400

    bad_limit = client.get("/api/knowledge/summaries", params={"limit": 0})
    assert bad_limit.status_code == 422


def test_knowledge_pagination(tmp_path):
    client, _storage = make_client(tmp_path)
    first_id = upload_and_analyze(client)
    second_id = upload_and_analyze(client)

    page_one = client.get("/api/knowledge/summaries", params={"limit": 1, "offset": 0})
    page_two = client.get("/api/knowledge/summaries", params={"limit": 1, "offset": 1})
    assert page_one.status_code == 200
    assert page_two.status_code == 200
    assert page_one.json()["total"] == 2
    assert page_two.json()["total"] == 2
    ids = {page_one.json()["items"][0]["photo_id"], page_two.json()["items"][0]["photo_id"]}
    assert ids == {first_id, second_id}


def test_rebuild_requires_analyzed_photo(tmp_path):
    client, _storage = make_client(tmp_path)
    filename, content, content_type = make_image_upload()
    upload = client.post(
        "/api/photos",
        files={"file": (filename, content, content_type)},
    )
    photo_id = upload.json()["photo_id"]

    response = client.post(f"/api/knowledge/summaries/{photo_id}/rebuild")
    assert response.status_code == 409

    missing = client.post("/api/knowledge/summaries/" + "0" * 32 + "/rebuild")
    assert missing.status_code == 404


def test_rebuild_and_reanalysis_keep_one_summary_per_photo(tmp_path):
    client, storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    previews_after_analysis = sorted(storage.summary_previews_dir.rglob("*.jpg"))
    assert len(previews_after_analysis) == 3

    rebuild = client.post(f"/api/knowledge/summaries/{photo_id}/rebuild")
    assert rebuild.status_code == 200
    assert rebuild.json()["photo_id"] == photo_id
    assert len(rebuild.json()["directions"]) == 3
    # Rebuilding replaces the per-photo preview directory instead of piling up
    # orphaned renderings.
    assert len(list(storage.summary_previews_dir.rglob("*.jpg"))) == 3

    reanalysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert reanalysis.status_code == 200
    refreshed = client.get(f"/api/knowledge/summaries/{photo_id}")
    assert refreshed.status_code == 200
    assert refreshed.json()["photo_id"] == photo_id
    assert len(list(storage.summary_previews_dir.rglob("*.jpg"))) == 3

    listing = client.get("/api/knowledge/summaries").json()
    assert listing["total"] == 1
    assert len(listing["items"]) == 1


def test_rebuild_reuses_measurements_across_directions(tmp_path, monkeypatch):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    calls: list[object] = []
    original = workflows_module.measure_file

    def counting_measure(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(workflows_module, "measure_file", counting_measure)

    rebuild = client.post(f"/api/knowledge/summaries/{photo_id}/rebuild")

    assert rebuild.status_code == 200
    assert len(rebuild.json()["directions"]) == 3
    assert len(calls) == 1


def test_corrupt_summary_record_returns_server_error(tmp_path):
    client, storage = make_client(tmp_path)
    upload_and_analyze(client)
    [path] = list(storage.knowledge_dir.glob("*.json"))
    path.write_text("{broken json", encoding="utf-8")

    response = client.get("/api/knowledge/summaries")
    assert response.status_code == 500
    assert "detail" in response.json()
    assert "broken" not in response.json()["detail"].lower()


def test_tampered_summary_url_fails_closed(tmp_path):
    client, storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    path = storage.knowledge_summary_path(photo_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["image"]["display_url"] = "javascript:alert(1)"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    response = client.get("/api/knowledge/summaries")

    assert response.status_code == 500
    assert "javascript" not in response.text.lower()


def test_naive_summary_timestamp_is_normalized_to_utc(tmp_path):
    client, storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    path = storage.knowledge_summary_path(photo_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generated_at"] = "2026-01-02T03:04:05"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    response = client.get("/api/knowledge/summaries")

    assert response.status_code == 200
    generated_at = response.json()["items"][0]["generated_at"]
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == UTC.utcoffset(parsed)


def test_failed_rebuild_keeps_previous_summary_and_previews(tmp_path, monkeypatch):
    client, storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    previous = client.get(f"/api/knowledge/summaries/{photo_id}").json()
    preview_urls = [
        direction["preview_url"]
        for direction in previous["directions"]
        if direction["preview_url"]
    ]
    assert len(preview_urls) == 3

    def fail_direction(*_args, **_kwargs):
        raise StorageOperationError("simulated render failure")

    monkeypatch.setattr(
        PhotoWorkflow,
        "_build_learning_direction",
        fail_direction,
    )

    failed = client.post(f"/api/knowledge/summaries/{photo_id}/rebuild")
    assert failed.status_code == 500

    unchanged = client.get(f"/api/knowledge/summaries/{photo_id}")
    assert unchanged.status_code == 200
    assert unchanged.json()["generated_at"] == previous["generated_at"]
    for preview_url in preview_urls:
        assert client.get(preview_url).status_code == 200

    monkeypatch.undo()
    rebuilt = client.post(f"/api/knowledge/summaries/{photo_id}/rebuild")
    assert rebuilt.status_code == 200
    preview_dir = storage.summary_previews_dir / photo_id
    assert len(list(preview_dir.glob("*.jpg"))) == 3



def test_knowledge_list_exposes_style_tags_and_style_filter(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)

    listing = client.get("/api/knowledge/summaries").json()
    item = listing["items"][0]
    style_tags = listing["style_tags"]
    assert style_tags, "style tags must aggregate the summary style names"
    assert {entry["name"] for entry in style_tags} == set(item["style_names"])
    for entry in style_tags:
        assert entry["count"] == 1

    overview = client.get("/api/knowledge/overview").json()
    assert {entry["name"] for entry in overview["style_tags"]} == set(
        item["style_names"]
    )

    style = item["style_names"][0]
    filtered = client.get("/api/knowledge/summaries", params={"style": style})
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["photo_id"] == photo_id

    missing_style = client.get(
        "/api/knowledge/summaries",
        params={"style": "不存在的风格"},
    )
    assert missing_style.status_code == 200
    assert missing_style.json()["total"] == 0
    assert missing_style.json()["items"] == []


def test_knowledge_search_matches_tags_styles_and_filenames(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    listing = client.get("/api/knowledge/summaries").json()
    item = listing["items"][0]

    category = item["primary_category"]
    by_category = client.get("/api/knowledge/summaries", params={"q": category})
    assert by_category.status_code == 200
    assert by_category.json()["total"] == 1
    assert by_category.json()["items"][0]["photo_id"] == photo_id

    by_filename = client.get("/api/knowledge/summaries", params={"q": "sample.jpg"})
    assert by_filename.status_code == 200
    assert by_filename.json()["total"] == 1

    tag_label = item["tags"][0]["label"]
    by_tag = client.get("/api/knowledge/summaries", params={"q": tag_label})
    assert by_tag.json()["total"] == 1

    # Multiple tokens are combined with AND semantics.
    two_tokens = f"{tag_label} {item['style_names'][0]}"
    by_tokens = client.get("/api/knowledge/summaries", params={"q": two_tokens})
    assert by_tokens.json()["total"] == 1
    no_match = client.get(
        "/api/knowledge/summaries",
        params={"q": f"{tag_label} 完全不存在"},
    )
    assert no_match.status_code == 200
    assert no_match.json()["total"] == 0
    assert no_match.json()["items"] == []


def test_knowledge_search_supports_approximate_typos(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    listing = client.get("/api/knowledge/summaries").json()
    item = listing["items"][0]

    tag_label = next(
        tag["label"] for tag in item["tags"] if len(tag["label"]) >= 3
    )
    typo = tag_label[:-1] + ("乙" if tag_label[-1] != "乙" else "丙")
    response = client.get("/api/knowledge/summaries", params={"q": typo})
    assert response.status_code == 200
    found_ids = {entry["photo_id"] for entry in response.json()["items"]}
    assert photo_id in found_ids, (
        f"approximate search for {typo!r} should still find {tag_label!r}"
    )


def test_knowledge_search_ignores_single_character_fragments(tmp_path):
    client, _storage = make_client(tmp_path)
    upload_and_analyze(client)
    listing = client.get("/api/knowledge/summaries").json()
    tag_label = listing["items"][0]["tags"][0]["label"]

    # 单字片段不应把无关查询也拖进来；两字查询差一个字也不该命中所有记录。
    fragment = tag_label[0] + ("乙" if tag_label[0] != "乙" else "丙")
    response = client.get("/api/knowledge/summaries", params={"q": fragment})
    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["items"] == []


def test_knowledge_search_combines_with_directory_filters(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    listing = client.get("/api/knowledge/summaries").json()
    item = listing["items"][0]
    tag = item["tags"][0]

    combined = client.get(
        "/api/knowledge/summaries",
        params={
            "q": item["primary_category"],
            "dimension": tag["dimension"],
            "tag": tag["label"],
            "style": item["style_names"][0],
        },
    )
    assert combined.status_code == 200
    assert combined.json()["total"] == 1
    assert combined.json()["items"][0]["photo_id"] == photo_id

    contradiction = client.get(
        "/api/knowledge/summaries",
        params={"q": "完全不存在", "style": item["style_names"][0]},
    )
    assert contradiction.json()["total"] == 0


def test_summary_detail_is_not_found_for_unknown_photo(tmp_path):
    client, _storage = make_client(tmp_path)
    response = client.get("/api/knowledge/summaries/" + "a" * 32)
    assert response.status_code == 404


def test_preview_response_exposes_adaptive_strength_details(tmp_path):
    client, _storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    style_id = client.get(f"/api/knowledge/summaries/{photo_id}").json()["directions"][0][
        "style_id"
    ]

    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style_id, "intensity": 0.8},
    )
    assert preview.status_code == 200
    body = preview.json()
    assert isinstance(body["verified"], bool)
    assert isinstance(body["warnings"], list)
    assert isinstance(body["baseline_notes"], list)
    assert body["strength"] is not None


def test_manual_preview_and_export_are_recorded_in_summary_detail(tmp_path):
    client, storage = make_client(tmp_path)
    photo_id = upload_and_analyze(client)
    detail = client.get(f"/api/knowledge/summaries/{photo_id}").json()
    assert detail["manual_edit"] is None
    styles = [direction["style_id"] for direction in detail["directions"]]

    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": styles[0], "intensity": 0.65},
    )
    assert preview.status_code == 200
    preview_body = preview.json()

    detail = client.get(f"/api/knowledge/summaries/{photo_id}").json()
    manual = detail["manual_edit"]
    assert manual is not None
    assert manual["style_id"] == styles[0]
    assert manual["style_name"] == detail["directions"][0]["style_name"]
    assert manual["intensity"] == 0.65
    assert manual["source"] == "preview"
    assert manual["parameters_summary"]
    assert manual["preview_url"] == preview_body["preview_url"]
    assert client.get(manual["preview_url"]).status_code == 200

    export = client.post(
        f"/api/photos/{photo_id}/export",
        json={"style_id": styles[1], "intensity": 0.4},
    )
    assert export.status_code == 200

    detail = client.get(f"/api/knowledge/summaries/{photo_id}").json()
    manual = detail["manual_edit"]
    assert manual["style_id"] == styles[1]
    assert manual["intensity"] == 0.4
    assert manual["source"] == "export"
    assert storage.load_session(photo_id).manual_edit is not None

    rebuilt = client.post(f"/api/knowledge/summaries/{photo_id}/rebuild")
    assert rebuilt.status_code == 200
    assert rebuilt.json()["manual_edit"]["style_id"] == styles[1]
