from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageStat

from app.adapters.analysis import MockAnalysisProvider
from app.adapters.model_image import prepare_model_image
from app.imaging import normalize_to_8bit
from app.main import create_app
from app.services.storage import FileStorage


def gradient_16bit_tiff(*, width: int = 512, height: int = 64) -> bytes:
    image = Image.new("I;16", (width, height))
    image.putdata(
        [
            int(x * 65535 / (width - 1))
            for _ in range(height)
            for x in range(width)
        ]
    )
    buffer = BytesIO()
    image.save(buffer, format="TIFF")
    return buffer.getvalue()


def make_client(tmp_path: Path) -> TestClient:
    storage = FileStorage(tmp_path, max_upload_bytes=4 * 1024 * 1024)
    application = create_app(storage=storage, analysis_provider=MockAnalysisProvider())
    return TestClient(application)


@pytest.mark.parametrize("mode", ["I;16", "I;16B", "I;16N"])
def test_normalize_to_8bit_scales_sixteen_bit_modes(mode):
    image = Image.new(mode, (5, 1))
    image.putdata([0, 255, 1000, 32768, 65535])

    assert list(normalize_to_8bit(image).getdata()) == [0, 1, 4, 128, 255]


def test_normalize_to_8bit_stretches_out_of_range_int_values():
    image = Image.new("I", (5, 1))
    image.putdata([0, 1000, 32768, 65535, 70000])

    assert list(normalize_to_8bit(image).getdata()) == [0, 3, 119, 238, 255]


def test_normalize_to_8bit_handles_unit_float_values():
    image = Image.new("F", (5, 1))
    image.putdata([0.0, 0.25, 0.5, 0.75, 1.0])

    assert list(normalize_to_8bit(image).getdata()) == [0, 63, 127, 191, 255]


def test_sixteen_bit_tiff_display_preserves_tonal_range(tmp_path):
    client = make_client(tmp_path)
    content = gradient_16bit_tiff()

    upload = client.post(
        "/api/photos",
        files={"file": ("gradient.tif", content, "image/tiff")},
    )

    assert upload.status_code == 200
    body = upload.json()
    assert body["metadata"]["format"] == "TIFF"
    assert body["metadata"]["color_mode"] == "I;16"

    display = client.get(body["display_url"])
    assert display.status_code == 200
    with Image.open(BytesIO(display.content)) as rendered:
        rendered.load()
        mean = ImageStat.Stat(rendered).mean[0]
        assert 110 <= mean <= 145
        assert rendered.getpixel((1, 10))[0] < 20
        assert rendered.getpixel((rendered.width - 2, 10))[0] > 235


def test_sixteen_bit_tiff_model_input_preserves_tonal_range(tmp_path):
    image_path = tmp_path / "gradient.tif"
    image_path.write_bytes(gradient_16bit_tiff())

    prepared = prepare_model_image(image_path)
    with Image.open(BytesIO(prepared.content)) as rendered:
        rendered.load()
        mean = ImageStat.Stat(rendered).mean[0]
        assert 110 <= mean <= 145
        assert rendered.getpixel((1, 10))[0] < 20
        assert rendered.getpixel((rendered.width - 2, 10))[0] > 235


def test_sixteen_bit_tiff_preview_preserves_tonal_range(tmp_path):
    client = make_client(tmp_path)
    upload = client.post(
        "/api/photos",
        files={"file": ("gradient.tif", gradient_16bit_tiff(), "image/tiff")},
    )
    assert upload.status_code == 200
    photo_id = upload.json()["photo_id"]

    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    assert analysis.status_code == 200
    style_id = analysis.json()["recommended_styles"][0]["id"]

    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style_id, "intensity": 0.7},
    )
    assert preview.status_code == 200

    response = client.get(preview.json()["preview_url"])
    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as rendered:
        rendered.load()
        left = rendered.getpixel((1, 10))[0]
        right = rendered.getpixel((rendered.width - 2, 10))[0]
        assert left + 40 < right
        assert ImageStat.Stat(rendered).mean[0] < 200
