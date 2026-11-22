from __future__ import annotations

import argparse
import json
import shutil
import sys
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from app.adapters.analysis import MockAnalysisProvider
from app.config import AppSettings
from app.main import create_app
from app.schemas import MAPPING_VERSION


def _image_bytes(
    image_format: str,
    *,
    size: tuple[int, int],
    color: tuple[int, int, int],
) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color=color).save(buffer, format=image_format)
    return buffer.getvalue()


def _cr3_bytes(*jpegs: bytes) -> bytes:
    ftyp_payload = b"crx " + b"\x00\x00\x00\x00" + b"crx "
    ftyp = (8 + len(ftyp_payload)).to_bytes(4, "big") + b"ftyp" + ftyp_payload
    return ftyp + b"\x00" * 64 + b"".join(jpegs) + b"\x00" * 64


def _run_one(
    client: TestClient,
    label: str,
    file_name: str,
    content: bytes,
    content_type: str,
) -> dict:
    upload = client.post("/api/photos", files={"file": (file_name, content, content_type)})
    upload.raise_for_status()
    uploaded = upload.json()
    photo_id = uploaded["photo_id"]

    analysis = client.post(f"/api/photos/{photo_id}/analyze")
    analysis.raise_for_status()
    analysis_body = analysis.json()
    style = analysis_body["recommended_styles"][0]

    preview = client.post(
        f"/api/photos/{photo_id}/preview",
        json={"style_id": style["id"], "intensity": 0.75},
    )
    preview.raise_for_status()

    develop = client.post(
        f"/api/photos/{photo_id}/develop",
        json={"style_id": style["id"], "intensity": 0.75},
    )
    develop.raise_for_status()

    send = client.post(
        f"/api/photos/{photo_id}/lightroom",
        json={"style_id": style["id"], "intensity": 0.75, "install_plugin": True},
    )
    send.raise_for_status()

    return {
        "label": label,
        "photo_id": photo_id,
        "metadata": uploaded["metadata"],
        "style": {"id": style["id"], "name": style["name"]},
        "preview_url": preview.json()["preview_url"],
        "develop_settings_version": develop.json()["develop_settings_version"],
        "mapping_version": develop.json()["mapping_version"],
        "process_version": develop.json()["develop_settings"]["ProcessVersion"],
        "tone_curve": develop.json()["develop_settings"]["ToneCurvePV2012"],
        "xmp_url": develop.json()["xmp_url"],
        "lightroom_status": send.json()["status"],
        "job_id": send.json()["job_id"],
        "preset_path": send.json()["preset_path"],
        "message": send.json()["message"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "data" / "runtime" / "lightroom-plugin-demo",
    )
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = AppSettings(
        data_root=output_dir / "appdata",
        lightroom_root=output_dir / "lightroom",
    )
    app = create_app(settings=settings, analysis_provider=MockAnalysisProvider())
    client = TestClient(app)

    jpeg = _image_bytes("JPEG", size=(1200, 800), color=(92, 129, 154))
    tiff = _image_bytes("TIFF", size=(1200, 800), color=(150, 112, 74))
    cr3_preview = _image_bytes("JPEG", size=(1200, 800), color=(172, 96, 66))
    cr3_thumbnail = _image_bytes("JPEG", size=(320, 213), color=(172, 96, 66))

    results = [
        _run_one(client, "JPEG", "sample.jpg", jpeg, "image/jpeg"),
        _run_one(client, "TIFF", "sample.tif", tiff, "image/tiff"),
        _run_one(
            client,
            "CR3",
            "sample.cr3",
            _cr3_bytes(cr3_thumbnail, cr3_preview),
            "application/octet-stream",
        ),
    ]

    report = {
        "mapping_version": MAPPING_VERSION,
        "server_url": "http://127.0.0.1:8000",
        "lightroom_status": client.get("/api/lightroom/status").json(),
        "results": results,
    }
    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    for result in results:
        print(
            f"[{result['label']}] {result['metadata']['format']} "
            f"{result['metadata']['width']}x{result['metadata']['height']} "
            f"render_source={result['metadata']['render_source']} "
            f"status={result['lightroom_status']} "
            f"job={result['job_id']}"
        )
    print(f"report: {report_path}")
    print(f"lightroom root: {settings.lightroom_root}")
    print(f"plugin: {settings.lightroom_root / 'Modules' / 'AIPhotoAgent.lrplugin'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
