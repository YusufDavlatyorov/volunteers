"""Generic image helpers (Feature Pack v2): upload validation + WebP variants.

Used by the chat (milestone 3) and, in milestone 6, by avatars / photo reports /
pet reports — nothing here knows about a particular model.

* ``validate_upload(file) -> str | None`` — ``None`` when the upload is a real
  image in an allowed format and ≤ ``MAX_UPLOAD_SIZE_MB``; otherwise an error
  code: ``too_large``, ``invalid_image`` (not decodable / truncated /
  decompression-bomb sized), ``unsupported_format``.
* ``make_webp_variants(field_file, sizes=...) -> {name: storage path}`` —
  fixes EXIF orientation, strips every metadata block (EXIF incl. GPS, ICC,
  XMP — WebP is written without them), never upscales, stores
  ``<dir>/<stem>_<name>.webp`` next to the original through the file's own
  storage. Raises on a broken source; the Celery task that calls it logs
  and gives up (the original stays usable).
"""

from __future__ import annotations

import io
import os

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from accounts.models import validate_file_size

DEFAULT_SIZES = {"thumb": 320, "medium": 960}
WEBP_QUALITY = 80
# Pillow's own bomb guard warns at ~89 MP; uploads here are phone photos,
# 40 MP is plenty and caps the decode memory on the 3.8 GB box.
MAX_PIXELS = 40_000_000

ERR_TOO_LARGE = "too_large"
ERR_INVALID = "invalid_image"
ERR_FORMAT = "unsupported_format"


def allowed_formats():
    """JPEG / PNG / WebP always; HEIC/HEIF only when a Pillow plugin
    (pillow-heif) registered a decoder — otherwise HEIC is rejected as
    ``unsupported_format`` instead of failing later in the variant task."""
    Image.init()
    formats = {"JPEG", "PNG", "WEBP"}
    if "HEIF" in Image.OPEN:
        formats.add("HEIF")
    return formats


def validate_upload(upload):
    """Return None if ``upload`` is acceptable, else an error code. Leaves
    the file positioned at 0 so it can still be saved."""
    if upload is None:
        return ERR_INVALID
    try:
        validate_file_size(upload)
    except ValidationError:
        return ERR_TOO_LARGE
    try:
        upload.seek(0)
        with Image.open(upload) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
            if fmt not in allowed_formats():
                return ERR_FORMAT
            if width * height > MAX_PIXELS:
                return ERR_INVALID
            img.verify()  # structural check (truncated / corrupt data)
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        return ERR_INVALID
    finally:
        try:
            upload.seek(0)
        except Exception:  # noqa: BLE001
            pass
    return None


def _prepared(img):
    """Orientation applied, mode WebP can store (RGB / RGBA)."""
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        return img.convert("RGBA")
    return img.convert("RGB")


def variant_name(original_name, variant):
    stem, _ = os.path.splitext(original_name)
    return f"{stem}_{variant}.webp"


def make_webp_variants(field_file, sizes=None, quality=WEBP_QUALITY):
    """Write one WebP per ``sizes`` entry (longest side ≤ px, never upscaled)
    next to ``field_file`` and return ``{variant: storage path}``."""
    sizes = DEFAULT_SIZES if sizes is None else sizes
    storage = field_file.storage
    field_file.open("rb")
    try:
        with Image.open(field_file) as src:
            if src.size[0] * src.size[1] > MAX_PIXELS:
                raise ValueError("image too large to process")
            base = _prepared(src)
    finally:
        field_file.close()

    out = {}
    for variant, px in sizes.items():
        img = base.copy()
        img.thumbnail((px, px), Image.Resampling.LANCZOS)  # shrinks only
        buf = io.BytesIO()
        # No exif= / icc_profile= / xmp= → the variant carries no metadata.
        img.save(buf, format="WEBP", quality=quality, method=4)
        target = variant_name(field_file.name, variant)
        if storage.exists(target):
            storage.delete(target)
        out[variant] = storage.save(target, ContentFile(buf.getvalue()))
    return out


def delete_variants(storage, variants):
    """Remove stored variant files (best effort)."""
    for path in (variants or {}).values():
        try:
            storage.delete(path)
        except Exception:  # noqa: BLE001
            pass
