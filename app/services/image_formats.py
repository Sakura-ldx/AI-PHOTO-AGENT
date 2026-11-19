from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from app.imaging import flatten_to_rgb


ImageFormatName = Literal["JPEG", "PNG", "TIFF", "CR3"]
RenderSource = Literal["original", "embedded_preview"]

CR3_MAJOR_BRAND_PREFIX = b"crx"
JPEG_SOI = b"\xff\xd8\xff"
MAX_EMBEDDED_PREVIEWS = 32
MAX_EMBEDDED_PREVIEW_BYTES = 64 * 1024 * 1024
_ORIENTATION_SWAPS_DIMENSIONS = {5, 6, 7, 8}


class ImageFormatError(ValueError):
    """The uploaded file is not a complete, supported image container."""


class ImagePixelLimitError(ValueError):
    """The uploaded image exceeds the configured pixel budget."""


@dataclass(frozen=True, slots=True)
class EmbeddedPreview:
    content: bytes
    width: int
    height: int
    color_mode: str
    orientation: int = 1


@dataclass(frozen=True, slots=True)
class InspectedImage:
    format: ImageFormatName
    width: int
    height: int
    color_mode: str
    content_type: str
    render_source: RenderSource
    preview: EmbeddedPreview | None = None


def sniff_image_container(path: Path) -> ImageFormatName | None:
    """Identify a supported container from magic bytes, not from the filename."""

    try:
        with path.open("rb") as source:
            header = source.read(16)
    except OSError as exc:
        raise ImageFormatError("Unable to read the uploaded image file.") from exc

    if header.startswith(JPEG_SOI):
        return "JPEG"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if header[:4] in {b"II*\x00", b"MM\x00*"}:
        return "TIFF"
    if (
        len(header) >= 12
        and header[4:8] == b"ftyp"
        and header[8:12].startswith(CR3_MAJOR_BRAND_PREFIX)
    ):
        return "CR3"
    return None


def inspect_image(path: Path, *, max_image_pixels: int) -> InspectedImage:
    """Validate an image container and return its persisted metadata.

    CR3 is a Canon RAW container. Pillow cannot decode the RAW sensor data, so
    the largest embedded JPEG preview is validated and used as the render/analysis
    source. The original .cr3 file remains untouched on disk.
    """

    container = sniff_image_container(path)
    if container == "CR3":
        return inspect_cr3(path, max_image_pixels=max_image_pixels)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                image_format = _canonical_image_format(image.format)
                _validate_format(image_format)
                _validate_dimensions(image.width, image.height, max_image_pixels)
                image.verify()

            # verify() intentionally invalidates the decoder. Reopen and fully
            # load the pixels so a valid header followed by truncated data is
            # rejected rather than accepted.
            with Image.open(path) as image:
                loaded_format = _canonical_image_format(image.format)
                _validate_format(loaded_format)
                _validate_dimensions(image.width, image.height, max_image_pixels)
                orientation = image.getexif().get(274)
                image.load()
                width, height = image.size
                if orientation in _ORIENTATION_SWAPS_DIMENSIONS:
                    width, height = height, width
                color_mode = image.mode
    except ImagePixelLimitError:
        raise
    except ImageFormatError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImagePixelLimitError(
            f"Uploaded image exceeds the {max_image_pixels} pixel limit."
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageFormatError(
            "Unable to identify a complete uploaded image file."
        ) from exc

    return InspectedImage(
        format=loaded_format,
        width=width,
        height=height,
        color_mode=color_mode,
        content_type=_content_type_for(loaded_format),
        render_source="original",
    )


def inspect_cr3(path: Path, *, max_image_pixels: int) -> InspectedImage:
    try:
        with path.open("rb") as source:
            header = source.read(12)
    except OSError as exc:
        raise ImageFormatError("Unable to read the uploaded CR3 file.") from exc
    if (
        len(header) < 12
        or header[4:8] != b"ftyp"
        or not header[8:12].startswith(CR3_MAJOR_BRAND_PREFIX)
    ):
        raise ImageFormatError("The uploaded file is not a valid Canon CR3 container.")

    preview = extract_cr3_preview(path)
    orientation = _cr3_exif_orientation(path) or preview.orientation
    width, height = preview.width, preview.height
    if orientation in _ORIENTATION_SWAPS_DIMENSIONS:
        width, height = height, width
    _validate_dimensions(width, height, max_image_pixels)
    return InspectedImage(
        format="CR3",
        width=width,
        height=height,
        color_mode=preview.color_mode,
        content_type="image/x-canon-cr3",
        render_source="embedded_preview",
        preview=replace(
            preview,
            width=width,
            height=height,
            orientation=orientation,
        ),
    )


def write_oriented_jpeg(content: bytes, orientation: int) -> bytes:
    """Apply an EXIF orientation value to JPEG bytes for browser/render output."""

    if orientation not in {2, 3, 4, 5, 6, 7, 8}:
        return content
    try:
        with Image.open(BytesIO(content)) as source:
            source.load()
            image = _transpose_for_orientation(source, orientation)
            if image.mode != "RGB":
                image = image.convert("RGB")
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=95, optimize=True)
            return buffer.getvalue()
    except (OSError, SyntaxError, ValueError):
        return content


def write_display_jpeg(
    source_path: Path,
    output_path: Path,
    *,
    max_size: tuple[int, int] = (2048, 2048),
) -> Path:
    """Create a browser-displayable JPEG for TIFF (and similar) sources."""

    temp_path = output_path.with_name(f".{output_path.name}.{uuid4().hex}.tmp")
    try:
        with Image.open(source_path) as source:
            source.load()
            image = ImageOps.exif_transpose(source)
            image.thumbnail(max_size, Image.Resampling.LANCZOS)
            image = flatten_to_rgb(image)
            image.save(temp_path, format="JPEG", quality=90, optimize=True)
        os.replace(temp_path, output_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return output_path


def extract_cr3_preview(path: Path) -> EmbeddedPreview:
    """Extract the largest complete JPEG preview stored inside a CR3 container.

    CR3 is an ISO base-media file. Canon stores one or more JPEG previews in
    metadata boxes. Instead of trusting undocumented box offsets, scan for JPEG
    start markers and walk each JPEG segment properly (so an EXIF thumbnail's
    EOI inside APP1 does not truncate the outer preview). The largest decodable
    candidate is used.
    """

    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageFormatError("Unable to read the uploaded CR3 file.") from exc

    candidates: list[tuple[int, int]] = []
    search_from = 0
    while len(candidates) < MAX_EMBEDDED_PREVIEWS:
        start = data.find(JPEG_SOI, search_from)
        if start < 0:
            break
        end = _jpeg_end_offset(data, start)
        if end is not None and end - start <= MAX_EMBEDDED_PREVIEW_BYTES:
            candidates.append((start, end))
            search_from = end
        else:
            search_from = start + len(JPEG_SOI)

    best: EmbeddedPreview | None = None
    best_area = 0
    for start, end in candidates:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data[start:end])) as image:
                    if image.format not in {"JPEG", "MPO"}:
                        continue
                    image.load()
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        continue
                    area = width * height
                    if area <= best_area:
                        continue
                    orientation = image.getexif().get(274)
                    best = EmbeddedPreview(
                        content=data[start:end],
                        width=width,
                        height=height,
                        color_mode=image.mode,
                        orientation=orientation or 1,
                    )
                    best_area = area
        except (OSError, SyntaxError, ValueError, UnidentifiedImageError):
            continue

    if best is None:
        raise ImageFormatError(
            "The CR3 container does not include a complete JPEG preview. "
            "Export the photo as JPEG/TIFF first or update the RAW decoder."
        )
    return best


def _transpose_for_orientation(image: Image.Image, orientation: int) -> Image.Image:
    operations = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    return image.transpose(operations[orientation])


def _cr3_exif_orientation(path: Path) -> int:
    """Read Orientation from the CR3 CMT1 EXIF box.

    Canon's embedded JPEG previews often have no EXIF orientation even though
    the CR3 metadata does. Reading CMT1 lets the web preview use the same
    camera orientation as Lightroom.
    """

    try:
        with path.open("rb") as source:
            data = source.read(1024 * 1024)
    except OSError:
        return 1
    marker = data.find(b"CMT1")
    if marker >= 0:
        for tiff_marker in (b"II*\x00", b"MM\x00*"):
            start = data.find(tiff_marker, marker, marker + 128)
            if start >= 0:
                value = _tiff_orientation(data, start)
                if value in range(1, 9):
                    return value
    # Some CR3 writers place the TIFF header before the CMT1 label.
    for tiff_marker in (b"II*\x00", b"MM\x00*"):
        start = data.find(tiff_marker, 0, 4096)
        if start >= 0:
            value = _tiff_orientation(data, start)
            if value in range(1, 9):
                return value
    return 1


def _tiff_orientation(data: bytes, start: int) -> int:
    if start + 8 > len(data):
        return 1
    endian = "little" if data[start : start + 2] == b"II" else "big"
    try:
        ifd_offset = int.from_bytes(data[start + 4 : start + 8], endian)
        ifd = start + ifd_offset
        if ifd + 2 > len(data):
            return 1
        count = int.from_bytes(data[ifd : ifd + 2], endian)
        if count > 512:
            return 1
        for index in range(count):
            entry = ifd + 2 + index * 12
            if entry + 12 > len(data):
                return 1
            tag = int.from_bytes(data[entry : entry + 2], endian)
            if tag != 0x0112:
                continue
            field_type = int.from_bytes(data[entry + 2 : entry + 4], endian)
            value_count = int.from_bytes(data[entry + 4 : entry + 8], endian)
            if field_type != 3 or value_count < 1:
                return 1
            if value_count == 1:
                value_offset = entry + 8
            else:
                value_offset = start + int.from_bytes(
                    data[entry + 8 : entry + 12], endian
                )
            if value_offset + 2 > len(data):
                return 1
            return int.from_bytes(data[value_offset : value_offset + 2], endian)
    except (ValueError, OverflowError):
        return 1
    return 1


def _jpeg_end_offset(data: bytes, start: int) -> int | None:
    if not data.startswith(b"\xff\xd8", start):
        return None
    # Keep the 0xFF marker prefix visible to the segment walker. Starting after
    # JPEG_SOI (FF D8 FF) would point at the marker code and skip the first
    # APP0/APP1 segment incorrectly.
    cursor = start + 2
    length = len(data)
    while cursor + 1 < length:
        if data[cursor] != 0xFF:
            cursor += 1
            continue
        marker = data[cursor + 1]
        if marker == 0xFF:
            # Fill bytes are allowed before a marker.
            cursor += 1
            continue
        if marker == 0xD9:
            return cursor + 2
        if marker == 0xD8 or marker == 0x01 or 0xD0 <= marker <= 0xD7:
            cursor += 2
            continue
        if cursor + 3 >= length:
            return None
        segment_length = int.from_bytes(data[cursor + 2 : cursor + 4], "big")
        if segment_length < 2:
            return None
        cursor += 2 + segment_length
        if marker != 0xDA:
            continue

        # Skip entropy-coded scan data. 0xFF 0x00 is byte stuffing and
        # 0xFF 0xD0..0xD7 are restart markers.
        while cursor + 1 < length:
            if data[cursor] != 0xFF:
                cursor += 1
                continue
            following = data[cursor + 1]
            if following == 0x00 or 0xD0 <= following <= 0xD7:
                cursor += 2
                continue
            if following == 0xD9:
                return cursor + 2
            break
    return None


def _canonical_image_format(image_format: str | None) -> ImageFormatName | None:
    if image_format == "MPO":
        return "JPEG"
    if image_format in {"JPEG", "PNG", "TIFF"}:
        return image_format  # type: ignore[return-value]
    return None


def _validate_format(image_format: ImageFormatName | None) -> None:
    if image_format not in {"JPEG", "PNG", "TIFF"}:
        raise ImageFormatError("Only JPEG, PNG, TIFF, and Canon CR3 images are supported.")


def _validate_dimensions(width: int, height: int, max_image_pixels: int) -> None:
    if width <= 0 or height <= 0:
        raise ImageFormatError("Uploaded image dimensions must be positive.")
    if width * height > max_image_pixels:
        raise ImagePixelLimitError(
            f"Uploaded image exceeds the {max_image_pixels} pixel limit."
        )


def _content_type_for(image_format: ImageFormatName) -> str:
    return {
        "JPEG": "image/jpeg",
        "PNG": "image/png",
        "TIFF": "image/tiff",
        "CR3": "image/x-canon-cr3",
    }[image_format]
