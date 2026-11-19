from __future__ import annotations

from PIL import Image


# Pillow decodes 16-bit grayscale TIFFs as I;16 variants and 32-bit/float
# samples as I/F. convert("RGB") on those modes clips values above 255 instead
# of scaling them, which turns a normal 16-bit photo into a pure white image.
def normalize_to_8bit(image: Image.Image) -> Image.Image:
    """Scale a high-bit-depth image to 8-bit without clipping highlights.

    ``I;16`` samples are mapped linearly from 0-65535 to 0-255, matching how a
    camera or editor expects a 16-bit grayscale TIFF to look. ``I`` and ``F``
    samples have no documented range, so values beyond 65535 (or beyond 1.0 for
    floats) fall back to a min/max stretch.
    """

    mode = image.mode
    if mode == "I;16N":
        # Pillow 11 converts I;16N to I by truncating instead of promoting.
        # Reinterpret the identical native bytes as I;16 first.
        image = Image.frombytes("I;16", image.size, image.tobytes())
        return _scale_16_bit(image)
    if mode in {"I;16", "I;16L", "I;16B"}:
        return _scale_16_bit(image)

    if mode == "I":
        try:
            _low, high = image.getextrema()
        except (OSError, ValueError):
            high = 65535
        if high > 65535:
            return _stretch_to_8bit(image)
        return _scale_16_bit(image)

    if mode == "F":
        try:
            low, high = image.getextrema()
        except (OSError, ValueError):
            return Image.new("L", image.size, 0)
        if high <= low:
            return Image.new("L", image.size, 0)
        if low >= 0.0 and high <= 1.0:
            return image.point(lambda value: value * 255.0).convert("L")
        return _stretch_to_8bit(image)

    return image


def flatten_to_rgb(image: Image.Image) -> Image.Image:
    """Return an opaque 8-bit RGB image suitable for JPEG output."""

    image = normalize_to_8bit(image)
    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba_image = image.convert("RGBA")
        background = Image.new("RGB", rgba_image.size, color="white")
        background.paste(rgba_image, mask=rgba_image.getchannel("A"))
        return background
    return image.convert("RGB")


def _scale_16_bit(image: Image.Image) -> Image.Image:
    # The affine expression lets Pillow build a fast C-level lookup table.
    return (
        image.convert("I")
        .point(lambda value: (value + 128) / 257.0)
        .convert("L")
    )


def _stretch_to_8bit(image: Image.Image) -> Image.Image:
    try:
        low, high = image.getextrema()
    except (OSError, ValueError):
        return Image.new("L", image.size, 0)
    if high <= low:
        return Image.new("L", image.size, 0)
    negative_low = -float(low)
    scale = 255.0 / float(high - low)
    return (
        image.convert("F")
        .point(lambda value: (value + negative_low) * scale)
        .convert("L")
    )
