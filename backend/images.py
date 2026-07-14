"""Image handling with Pillow (replaces ffmpeg for stills).

Resize to the model's budget, convert anything -> JPEG, return a base64 data URI.
Native: JPEG/PNG/WEBP/AVIF (Pillow 11.3+). HEIC/HEIF via pillow-heif.
"""

from __future__ import annotations

import base64
import io

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except Exception:  # pragma: no cover - HEIC just won't be supported
    pass

# Match the server's 1120 image-token budget (~2112px long edge).
MAX_SIDE = 2112
QUALITY = 90


def _to_jpeg_data_uri(im: Image.Image, max_side: int, quality: int) -> str:
    im = ImageOps.exif_transpose(im)          # honour phone rotation
    im = im.convert("RGB")                    # drop alpha/CMYK for JPEG
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def from_bytes(data: bytes, max_side: int = MAX_SIDE, quality: int = QUALITY) -> str:
    with Image.open(io.BytesIO(data)) as im:
        return _to_jpeg_data_uri(im, max_side, quality)


def from_data_url(data_url: str, max_side: int = MAX_SIDE, quality: int = QUALITY) -> str:
    """Accept a browser 'data:<mime>;base64,....' URL (drag-drop / paste) -> model JPEG URI."""
    head, _, b64 = data_url.partition(",")
    if not b64:
        raise ValueError("invalid data URL")
    raw = base64.b64decode(b64)
    return from_bytes(raw, max_side, quality)
