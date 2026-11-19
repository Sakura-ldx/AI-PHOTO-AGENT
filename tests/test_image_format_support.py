from __future__ import annotations

from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from app.adapters.analysis import MockAnalysisProvider
from app.main import create_app
from app.services.image_formats import _jpeg_end_offset
from app.services.storage import FileStorage


def image_bytes(
    image_format: str,
    *,
    size: tuple[int, int] = (160, 100),
    color: tuple[int, int, int] = (90, 140, 80),
    mode: str = "RGB",
) -> bytes:
    buffer = BytesIO()
    if mode == "I;16":
        image = Image.frombytes(
            "I;16",
            size,
            bytes((index * 137) % 256 for index in range(size[0] * size[1] * 2)),
        )
    else:
        image = Image.new(mode, size, color=color)
    image.save(buffer, format=image_format)
    return buffer.getvalue()


def cr3_bytes(*jpegs: bytes) -> bytes:
    ftyp_payload = b"crx " + b"\x00\x00\x00\x00" + b"crx "
    ftyp = (8 + len(ftyp_payload)).to_bytes(4, "big") + b"ftyp" + ftyp_payload
    return ftyp + b"\x00" * 64 + b"".join(jpegs) + b"\x00" * 64


def oriented_jpeg_bytes(
    *,
    size: tuple[int, int] = (80, 40),
    orientation: int = 6,
) -> bytes:
    buffer = BytesIO()
    exif = Image.Exif()
    exif[274] = orientation
    Image.new("RGB", size, color=(120, 150, 180)).save(
        buffer,
        format="JPEG",
        exif=exif,
    )
    return buffer.getvalue()


def cr3_with_orientation(jpeg: bytes, orientation: int) -> bytes:
    tiff = (
        b"II*\x00"
        + (8).to_bytes(4, "little")
        + (1).to_bytes(2, "little")
        + (0x0112).to_bytes(2, "little")
        + (3).to_bytes(2, "little")
        + (1).to_bytes(4, "little")
        + orientation.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
    )
    cmt1 = (8 + len(tiff)).to_bytes(4, "big") + b"CMT1" + tiff
    return cr3_bytes(cmt1 + jpeg)


def make_client(tmp_path) -> tuple[TestClient, FileStorage]:
    storage = FileStorage(tmp_path, max_upload_bytes=4 * 1024 * 1024)
    application = create_app(storage=storage, analysis_provider=MockAnalysisProvider())
    return TestClient(application), storage


def test_tiff_upload_analysis_preview_and_export(tmp_path):
    client, storage = make_client(tmp_path)
    content = image_bytes("TIFF")

    upload = client.post(
        "/api/photos",
        files={"file": ("portrait.tif", content, "image/tiff")},
    )
    assert upload.status_code == 200
    body = upload.json()
    assert body["metadata"]["format"] == "TIFF"
    assert body["metadata"]["content_type"] == "image/tiff"
    assert body["metadata"]["render_source"] == "original"
    photo_id = body["photo_id"]
    assert body["display_url"] == f"/api/photos/{photo_id}/display"

    display = client.get(body["display_url"])
    assert display.status_code == 200
    assert display.headers["content-type"].startswith("image/jpeg")
    with Image.open(BytesIO(display.content)) as rendered:
        assert rendered.format == "JPEG"

    session = storage.load_session(photo_id)
    assert session.original_file.name == "original.tif"
    assert session.render_file is None
    assert session.display_file is not None
    assert session.display_file.name == "display.jpg"

    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    style_id = analysis.json()["recommended_styles"][0]["id"]

    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style_id, "intensity": 0.7},
    )
    assert preview.status_code == 200
    assert preview.json()["preview_url"].endswith(".jpg")

    export = client.post(
        f"/api/photos/{photo_id}/export",
        json={"style_id": style_id, "intensity": 0.7},
    )
    assert export.status_code == 200
    download_url = export.json()["download_url"]
    assert download_url.endswith(".xmp")
    download = client.get(download_url)
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment;")
    assert b"xmpmeta" in download.content


def test_sixteen_bit_tiff_is_accepted(tmp_path):
    client, _storage = make_client(tmp_path)
    content = image_bytes("TIFF", mode="I;16")

    upload = client.post(
        "/api/photos",
        files={"file": ("scan.tif", content, "application/octet-stream")},
    )

    assert upload.status_code == 200
    assert upload.json()["metadata"]["format"] == "TIFF"
    photo_id = upload.json()["photo_id"]
    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    style_id = analysis.json()["recommended_styles"][0]["id"]
    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style_id, "intensity": 0.6},
    )
    assert preview.status_code == 200


def test_cr3_upload_uses_largest_embedded_jpeg_and_keeps_original(tmp_path):
    client, storage = make_client(tmp_path)
    small = image_bytes("JPEG", size=(64, 48), color=(10, 20, 30))
    large = image_bytes("JPEG", size=(320, 200), color=(180, 100, 60))

    upload = client.post(
        "/api/photos",
        files={"file": ("capture.cr3", cr3_bytes(small, large), "application/octet-stream")},
    )

    assert upload.status_code == 200
    body = upload.json()
    assert body["metadata"]["format"] == "CR3"
    assert body["metadata"]["content_type"] == "image/x-canon-cr3"
    assert body["metadata"]["render_source"] == "embedded_preview"
    assert (body["metadata"]["width"], body["metadata"]["height"]) == (320, 200)

    photo_id = body["photo_id"]
    display = client.get(body["display_url"])
    assert display.status_code == 200
    assert display.headers["content-type"].startswith("image/jpeg")
    with Image.open(BytesIO(display.content)) as rendered:
        assert rendered.size == (320, 200)

    session = storage.load_session(photo_id)
    assert session.original_file.name == "original.cr3"
    assert session.original_file.read_bytes()[4:8] == b"ftyp"
    assert session.render_file is not None
    assert session.render_file.name == "embedded.jpg"
    assert session.render_file.is_file()
    assert session.display_file == session.render_file

    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    style_id = analysis.json()["recommended_styles"][0]["id"]

    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style_id, "intensity": 0.8},
    )
    assert preview.status_code == 200
    preview_url = preview.json()["preview_url"]
    assert client.get(preview_url).status_code == 200

    develop = client.post(
        f"/api/photos/{photo_id}/develop",
        json={"style_id": style_id, "intensity": 0.8},
    )
    assert develop.status_code == 200
    assert develop.json()["develop_settings"]["ProcessVersion"] == "15.4"


def test_jpeg_upload_display_and_metadata_use_exif_orientation(tmp_path):
    client, storage = make_client(tmp_path)
    content = oriented_jpeg_bytes(size=(80, 40), orientation=6)

    upload = client.post(
        "/api/photos",
        files={"file": ("portrait.jpg", content, "image/jpeg")},
    )

    assert upload.status_code == 200
    body = upload.json()
    assert (body["metadata"]["width"], body["metadata"]["height"]) == (40, 80)
    display = client.get(body["display_url"])
    assert display.status_code == 200
    with Image.open(BytesIO(display.content)) as rendered:
        assert rendered.size == (40, 80)
    session = storage.load_session(body["photo_id"])
    assert session.display_file is not None
    assert session.display_file.name == "display.jpg"


def test_cr3_cmt1_orientation_rotates_display_and_preview(tmp_path):
    client, _storage = make_client(tmp_path)
    jpeg = image_bytes("JPEG", size=(320, 200), color=(40, 90, 140))
    content = cr3_with_orientation(jpeg, 8)

    upload = client.post(
        "/api/photos",
        files={"file": ("portrait.cr3", content, "application/octet-stream")},
    )

    assert upload.status_code == 200
    body = upload.json()
    assert (body["metadata"]["width"], body["metadata"]["height"]) == (200, 320)
    display = client.get(body["display_url"])
    assert display.status_code == 200
    with Image.open(BytesIO(display.content)) as rendered:
        assert rendered.size == (200, 320)

    photo_id = body["photo_id"]
    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    style_id = analysis.json()["recommended_styles"][0]["id"]
    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style_id, "intensity": 0.7},
    )
    assert preview.status_code == 200
    preview_response = client.get(preview.json()["preview_url"])
    assert preview_response.status_code == 200
    with Image.open(BytesIO(preview_response.content)) as rendered:
        assert rendered.height > rendered.width


def test_cr3_jpeg_walker_skips_embedded_exif_thumbnail():
    main = image_bytes("JPEG", size=(320, 200))
    thumbnail = image_bytes("JPEG", size=(32, 24))
    app1_payload = b"Exif\x00\x00" + thumbnail
    app1 = b"\xff\xe1" + (len(app1_payload) + 2).to_bytes(2, "big") + app1_payload
    jpeg_with_thumbnail = main[:2] + app1 + main[2:]

    # The walker must skip APP1's payload and reach the outer JPEG's EOI.
    assert _jpeg_end_offset(jpeg_with_thumbnail, 0) == len(jpeg_with_thumbnail)


def test_cr3_without_embedded_preview_is_rejected(tmp_path):
    client, _storage = make_client(tmp_path)
    ftyp_payload = b"crx " + b"\x00\x00\x00\x00" + b"crx "
    ftyp = (8 + len(ftyp_payload)).to_bytes(4, "big") + b"ftyp" + ftyp_payload

    upload = client.post(
        "/api/photos",
        files={"file": ("broken.cr3", ftyp + b"\x00" * 512, "application/octet-stream")},
    )

    assert upload.status_code == 400
    assert "JPEG preview" in upload.json()["detail"]


def test_cr3_embedded_preview_respects_pixel_limit(tmp_path):
    storage = FileStorage(tmp_path, max_image_pixels=100 * 100)
    client = TestClient(create_app(storage=storage, analysis_provider=MockAnalysisProvider()))
    preview = image_bytes("JPEG", size=(320, 200))

    upload = client.post(
        "/api/photos",
        files={"file": ("big.cr3", cr3_bytes(preview), "application/octet-stream")},
    )

    assert upload.status_code == 413
    assert "pixel limit" in upload.json()["detail"]


def test_learning_reference_still_rejects_raw_and_tiff(tmp_path):
    client, _storage = make_client(tmp_path)
    session_id = client.post("/api/learning/sessions", json={"topic": "格式边界"}).json()["id"]

    tiff = client.post(
        f"/api/learning/sessions/{session_id}/references",
        files={"file": ("scan.tif", image_bytes("TIFF"), "image/tiff")},
    )
    cr3 = client.post(
        f"/api/learning/sessions/{session_id}/references",
        files={
            "file": (
                "capture.cr3",
                cr3_bytes(image_bytes("JPEG")),
                "application/octet-stream",
            )
        },
    )

    assert tiff.status_code == 400
    assert cr3.status_code == 400
    assert "JPEG and PNG" in tiff.json()["detail"]
