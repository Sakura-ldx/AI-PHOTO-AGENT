from __future__ import annotations

import asyncio
import json
import os
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from PIL import Image

from app.services.series import (
    SignatureCache,
    _sequence_gap,
    _visual_score,
    compute_signature,
    dhash64,
    hamming_distance,
)
from app.services.storage import FileStorage


def gradient_image(size: tuple[int, int] = (200, 200)) -> Image.Image:
    return Image.linear_gradient("L").resize(size).convert("RGB")


def radial_image(size: tuple[int, int] = (200, 200)) -> Image.Image:
    return Image.radial_gradient("L").resize(size).convert("RGB")


def save_image(path: Path, image: Image.Image, image_format: str = "PNG") -> None:
    image.save(path, format=image_format)


def test_dhash_is_stable_for_the_same_pixels(tmp_path: Path) -> None:
    path = tmp_path / "photo.png"
    save_image(path, gradient_image())

    first = compute_signature("a" * 32, path)
    second = compute_signature("a" * 32, path)

    assert first.dhash == second.dhash
    assert first.aspect_ratio == second.aspect_ratio


def test_dhash_survives_jpeg_reencoding(tmp_path: Path) -> None:
    png_path = tmp_path / "photo.png"
    jpg_path = tmp_path / "photo.jpg"
    image = gradient_image()
    save_image(png_path, image, "PNG")
    save_image(jpg_path, image, "JPEG")

    assert hamming_distance(
        dhash64(Image.open(png_path)),
        dhash64(Image.open(jpg_path)),
    ) <= 6


def test_dhash_separates_different_compositions(tmp_path: Path) -> None:
    left = tmp_path / "left.png"
    right = tmp_path / "right.png"
    save_image(left, gradient_image())
    save_image(right, radial_image())

    assert hamming_distance(dhash64(Image.open(left)), dhash64(Image.open(right))) > 6


def test_sequence_gap_matches_only_the_same_numbered_stem() -> None:
    assert _sequence_gap("IMG_1001.CR3", "IMG_1003.CR3") == 2
    assert _sequence_gap("IMG_1001.CR3", "DSC_1003.CR3") is None
    assert _sequence_gap("IMG_1001.CR3", "IMG_1100.CR3") is None
    assert _sequence_gap("photo.jpg", "photo-2.jpg") is None


def test_visual_score_ranks_close_photos_above_different_ones(tmp_path: Path) -> None:
    left = tmp_path / "left.png"
    right = tmp_path / "right.png"
    save_image(left, gradient_image())
    save_image(right, radial_image())

    left_signature = compute_signature("a" * 32, left)
    right_signature = compute_signature("b" * 32, right)

    assert _visual_score(left_signature, left_signature) == 1.0
    assert _visual_score(left_signature, right_signature) < 1.0


def test_signature_cache_hits_and_invalidates_on_source_change(tmp_path: Path) -> None:
    source = tmp_path / "photo.png"
    cache = SignatureCache(tmp_path / "signatures")
    photo_id = "c" * 32
    save_image(source, gradient_image())

    first = cache.get(photo_id, source)
    assert first is not None
    assert (cache.directory / f"{photo_id}.json").exists()

    cached = cache.get(photo_id, source)
    assert cached is not None
    assert cached.dhash == first.dhash
    assert cached.mtime_ns == first.mtime_ns

    save_image(source, radial_image())
    os.utime(source, ns=(first.mtime_ns + 1_000_000_000, first.mtime_ns + 1_000_000_000))

    refreshed = cache.get(photo_id, source)
    assert refreshed is not None
    assert refreshed.dhash != first.dhash


def test_signature_cache_rejects_malformed_numeric_fields(tmp_path: Path) -> None:
    source = tmp_path / "photo.png"
    cache = SignatureCache(tmp_path / "signatures")
    photo_id = "e" * 32
    save_image(source, gradient_image())
    first = cache.get(photo_id, source)
    assert first is not None
    path = cache.directory / f"{photo_id}.json"
    valid_payload = json.loads(path.read_text(encoding="utf-8"))

    variants = [
        ("dhash", "f" * 17),
        ("luma_mean", float("nan")),
        ("saturation_mean", -1.0),
        ("aspect_ratio", float("inf")),
    ]
    for field, value in variants:
        payload = dict(valid_payload)
        payload[field] = value
        path.write_text(json.dumps(payload), encoding="utf-8")

        refreshed = cache.get(photo_id, source)

        assert refreshed is not None
        assert refreshed.dhash == first.dhash
        assert 0.0 <= refreshed.luma_mean <= 255.0
        assert 0.0 <= refreshed.saturation_mean <= 255.0


def test_signature_cache_rejects_an_invalid_photo_id(tmp_path: Path) -> None:
    source = tmp_path / "photo.png"
    cache = SignatureCache(tmp_path / "signatures")
    save_image(source, gradient_image())

    assert cache.get("../../outside", source) is None
    assert not cache.directory.exists()


def test_list_sessions_skips_a_corrupt_record(tmp_path: Path) -> None:
    storage = FileStorage(tmp_path)
    upload = UploadFile(file=BytesIO(_png_bytes(gradient_image())), filename="photo.png")
    session = asyncio.run(storage.save_upload(upload))

    corrupt = storage.sessions_dir / f"{'d' * 32}.json"
    corrupt.write_text(json.dumps({"id": "not-a-session"}), encoding="utf-8")

    sessions = storage.list_sessions()

    assert [item.id for item in sessions] == [session.id]


def _png_bytes(image: Image.Image) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
