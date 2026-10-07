"""Upload validation. The server never resizes images; oversized input is rejected."""

from __future__ import annotations

import hashlib
import io
import math
import warnings
from dataclasses import dataclass
from typing import Any

from PIL import Image, UnidentifiedImageError

SUPPORTED_FORMATS: dict[str, tuple[str, str]] = {
    "PNG": ("image/png", "png"),
    "JPEG": ("image/jpeg", "jpg"),
    "WEBP": ("image/webp", "webp"),
    "BMP": ("image/bmp", "bmp"),
    "TIFF": ("image/tiff", "tiff"),
    "GIF": ("image/gif", "gif"),
}


class ImageRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


@dataclass(frozen=True)
class ImageInfo:
    format: str
    mime: str
    extension: str
    width: int
    height: int
    size_bytes: int
    sha256: str

    @property
    def pixels(self) -> int:
        return self.width * self.height


def suggest_size(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    """Largest size with the same aspect ratio whose area does not exceed ``max_pixels``."""
    factor = math.sqrt(max_pixels / (width * height))
    new_w, new_h = max(1, math.floor(width * factor)), max(1, math.floor(height * factor))
    while new_w * new_h > max_pixels:
        if new_w >= new_h:
            new_w -= 1
        else:
            new_h -= 1
    return new_w, new_h


def _too_large(width: int, height: int, max_pixels: int) -> ImageRejected:
    sw, sh = suggest_size(width, height, max_pixels)
    return ImageRejected(
        413,
        "image_too_large",
        f"Image is {width}x{height} ({width * height} pixels), which exceeds the server limit of {max_pixels} "
        f"pixels. The server does not downsample images; resize it on the client (for example to {sw}x{sh}) "
        "and resubmit.",
        width=width,
        height=height,
        pixels=width * height,
        max_pixels=max_pixels,
        suggested_width=sw,
        suggested_height=sh,
    )


def inspect_image(data: bytes, *, max_pixels: int | None) -> ImageInfo:
    """Validate an uploaded image without decoding/resizing it."""
    if not data:
        raise ImageRejected(400, "empty_image", "The uploaded image is empty.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                fmt = img.format or ""
                width, height = img.size
                if fmt not in SUPPORTED_FORMATS:
                    raise ImageRejected(
                        415,
                        "unsupported_image_format",
                        f"Image format {fmt or 'unknown'} is not supported. Supported: {', '.join(SUPPORTED_FORMATS)}.",
                    )
                if max_pixels and width * height > max_pixels:
                    raise _too_large(width, height, max_pixels)
                img.verify()
    except ImageRejected:
        raise
    except Image.DecompressionBombError as exc:
        raise ImageRejected(
            413, "image_too_large", f"Image is far too large: {exc}. Resize it on the client.", max_pixels=max_pixels
        ) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageRejected(400, "invalid_image", "The uploaded file is not a readable image.") from exc

    mime, ext = SUPPORTED_FORMATS[fmt]
    return ImageInfo(
        format=fmt,
        mime=mime,
        extension=ext,
        width=width,
        height=height,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
