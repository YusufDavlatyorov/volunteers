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
* ``strip_metadata(upload) -> (ContentFile, ext)`` — the *original* re-encoded
  without EXIF (incl. GPS), XMP, IPTC or comments, orientation baked in.
  Called before an upload is stored (chat photos), so the full-size file a
  party can open carries no location either — not only the thumbnails.
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


# Re-encode settings for the stripped original: visually lossless for photos.
ORIGINAL_JPEG_QUALITY = 90
ORIGINAL_WEBP_QUALITY = 90
_STRIP_OUTPUT = {"JPEG": ("JPEG", "jpg"), "PNG": ("PNG", "png"), "WEBP": ("WEBP", "webp")}


def strip_metadata(upload):
    """Return ``(ContentFile, ext)`` — ``upload`` re-encoded with no metadata.

    EXIF orientation is applied to the pixels first (so dropping the tag never
    leaves a sideways photo). JPEG/PNG/WebP keep their format; anything else
    the decoder accepted (HEIC via pillow-heif) becomes JPEG. Only the ICC
    colour profile is kept — it describes colours, not the person or place.
    The caller has already run ``validate_upload``; a decode error raises."""
    upload.seek(0)
    with Image.open(upload) as src:
        fmt = (src.format or "").upper()
        if src.size[0] * src.size[1] > MAX_PIXELS:
            raise ValueError("image too large to process")
        icc = src.info.get("icc_profile")
        img = ImageOps.exif_transpose(src)  # first frame; a copy without the EXIF orientation
        img.load()
    out_format, ext = _STRIP_OUTPUT.get(fmt, ("JPEG", "jpg"))
    if out_format == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif out_format == "PNG" and img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        img = img.convert("RGBA")
    elif out_format == "WEBP" and img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.mode or "transparency" in img.info else "RGB")
    options = {}
    if icc:
        options["icc_profile"] = icc
    if out_format == "JPEG":
        options.update(quality=ORIGINAL_JPEG_QUALITY, optimize=True)
    elif out_format == "WEBP":
        options.update(quality=ORIGINAL_WEBP_QUALITY, method=4)
    else:
        options.update(optimize=True)
    # No exif= / xmp= / pnginfo= → nothing but pixels (+ ICC) is written.
    img.info.pop("exif", None)
    img.info.pop("xmp", None)
    buf = io.BytesIO()
    img.save(buf, format=out_format, **options)
    upload.seek(0)
    return ContentFile(buf.getvalue()), ext


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
