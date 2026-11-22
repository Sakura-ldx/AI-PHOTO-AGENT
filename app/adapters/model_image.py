from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from app.errors import ProviderServiceError
from app.imaging import flatten_to_rgb, normalize_to_8bit

LOGGER = logging.getLogger(__name__)
MODEL_IMAGE_MAX_SIZE = (2048, 2048)
MODEL_IMAGE_JPEG_QUALITY = 88


@dataclass(frozen=True, slots=True)
class PreparedModelImage:
    content: bytes
    width: int
    height: int
    sha256: str
    media_type: str = "image/jpeg"

    @property
    def data_url(self) -> str:
        encoded = base64.b64encode(self.content).decode("ascii")
        return f"data:{self.media_type};base64,{encoded}"

    def write_to(self, path: Path) -> Path:
        path.write_bytes(self.content)
        return path


def prepare_model_image(image_path: Path) -> PreparedModelImage:
    """Create a bounded, orientation-corrected, metadata-free sRGB JPEG."""

    try:
        with Image.open(image_path) as source:
            source.load()
            icc_profile = source.info.get("icc_profile")
            image = ImageOps.exif_transpose(source)
            image.thumbnail(MODEL_IMAGE_MAX_SIZE, Image.Resampling.LANCZOS)
            image = normalize_to_8bit(image)
            color_managed_image = _convert_to_srgb(image, icc_profile)
            rgb_image = _flatten_to_rgb(color_managed_image)
            buffer = BytesIO()
            rgb_image.save(
                buffer,
                format="JPEG",
                quality=MODEL_IMAGE_JPEG_QUALITY,
                optimize=True,
            )
    except (OSError, SyntaxError, ValueError, ImageCms.PyCMSError) as exc:
        raise ProviderServiceError(
            "Unable to prepare the uploaded image for model analysis."
        ) from exc

    content = buffer.getvalue()
    return PreparedModelImage(
        content=content,
        width=rgb_image.width,
        height=rgb_image.height,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    return flatten_to_rgb(image)


def _convert_to_srgb(image: Image.Image, icc_profile: object) -> Image.Image:
    if not isinstance(icc_profile, bytes) or not icc_profile:
        return image
    try:
        source_profile = ImageCms.ImageCmsProfile(BytesIO(icc_profile))
        target_profile = ImageCms.createProfile("sRGB")
        working_image = (
            _flatten_to_rgb(image)
            if image.mode in {"RGBA", "LA"}
            or (image.mode == "P" and "transparency" in image.info)
            else image
        )
        return ImageCms.profileToProfile(
            working_image,
            source_profile,
            target_profile,
            outputMode="RGB",
        )
    except (OSError, ValueError, ImageCms.PyCMSError):
        LOGGER.warning("Ignoring an invalid embedded ICC profile during model preparation.")
        return image
