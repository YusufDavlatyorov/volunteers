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
import threading

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

from accounts.models import validate_file_size

DEFAULT_SIZES = {"thumb": 320, "medium": 960}
WEBP_QUALITY = 80
# Pillow's own bomb guard warns at ~89 MP; uploads here are phone photos,
# 40 MP is plenty and caps the decode memory on the 3.8 GB box.
MAX_PIXELS = 40_000_000

# One image decode at a time per process. The Celery worker runs 4 threads
# under a 300 MB limit; four 12 MP photos decoded together peaked at ~690 MB
# (measured 2026-10-03) — an OOM kill, and with acks_late the same task would
# come back and kill the worker again. Serialised + JPEG draft: ~80 MB.
_DECODE_LOCK = threading.BoundedSemaphore(1)

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
    encoded = {}
    with _DECODE_LOCK:
        field_file.open("rb")
        try:
            with Image.open(field_file) as src:
                if src.size[0] * src.size[1] > MAX_PIXELS:
                    raise ValueError("image too large to process")
                # JPEG only (no-op otherwise): decode at 1/2, 1/4 or 1/8 scale,
                # still ≥ the largest variant — a 12 MP photo needs ~1/6 the
                # memory and no full-size resample.
                largest = max(sizes.values())
                src.draft("RGB", (largest, largest))
                base = _prepared(src)
        finally:
            field_file.close()
        for variant, px in sizes.items():
            img = base.copy()
            img.thumbnail((px, px), Image.Resampling.LANCZOS)  # shrinks only
            buf = io.BytesIO()
            # No exif= / icc_profile= / xmp= → the variant carries no metadata.
            img.save(buf, format="WEBP", quality=quality, method=4)
            encoded[variant] = buf.getvalue()
        del base

    out = {}
    for variant, data in encoded.items():
        target = variant_name(field_file.name, variant)
        if storage.exists(target):
            storage.delete(target)
        out[variant] = storage.save(target, ContentFile(data))
    return out


def delete_variants(storage, variants):
    """Remove stored variant files (best effort)."""
    for path in (variants or {}).values():
        try:
            storage.delete(path)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# milestone 6: variants for avatars / photo reports / pet photos
# --------------------------------------------------------------------------- #

# Models with ``image`` + ``image_variants`` that get thumb/medium WebPs. The
# chat keeps its own path (services/chat.apply_image_variants: it also pushes a
# WS update), the rest share this one.
VARIANT_MODELS = ("accounts.Profile", "myapp.PhotoReport", "myapp.PetReport")


def variants_current(field_file, variants):
    """True when ``variants`` were built from the file ``field_file`` holds now
    (no image → no variants is also current)."""
    variants = variants or {}
    if not field_file:
        return not variants
    return all(variants.get(name) == variant_name(field_file.name, name) for name in DEFAULT_SIZES)


def queue_variants(instance):
    """Call after saving an upload: builds the variants in the background
    (Celery; inline without a broker). Returns True when something was queued."""
    if variants_current(instance.image, instance.image_variants):
        return False
    from ..notifications import _dispatch
    from ..tasks import make_image_variants

    _dispatch(make_image_variants, instance._meta.label, instance.pk)
    return True


def apply_variants(label, pk):
    """Celery body: (re)build the variants of one row, drop stale variant
    files. The final UPDATE is conditional on the image still being the one
    the variants were built from, so a newer upload is never overwritten."""
    from django.apps import apps

    if label not in VARIANT_MODELS:
        raise ValueError(f"no image variants for {label}")
    model = apps.get_model(label)
    obj = model.objects.filter(pk=pk).first()
    if obj is None:
        return None
    old = dict(obj.image_variants or {})
    storage = obj.image.storage
    if variants_current(obj.image, old):
        return old
    new = make_webp_variants(obj.image) if obj.image else {}
    delete_variants(storage, {k: v for k, v in old.items() if v not in new.values()})
    model.objects.filter(pk=pk, image=obj.image.name or "").update(image_variants=new)
    return new


UPLOAD_ERROR_TEXT = "Не удалось обработать фото. Загрузите JPEG, PNG или WebP до 5 МБ."


def clean_original(upload):
    """Validate an avatar / photo-report / pet upload and return
    ``(ContentFile, None)`` — re-encoded without EXIF (incl. GPS) / XMP, named
    ``<uuid>.<ext>`` (the user's file name can carry their real name, and media
    URLs are not access-controlled) — or ``(None, error_code)``.

    Photo reports are shown to anonymous visitors on the landing page: a phone
    original would otherwise publish where it was taken (often a client's home).
    """
    import logging
    import uuid

    err = validate_upload(upload)
    if err:
        return None, err
    try:
        content, ext = strip_metadata(upload)
    except Exception:  # noqa: BLE001 — validated above; a codec failure
        logging.getLogger(__name__).warning("image metadata strip failed")
        return None, ERR_INVALID
    content.name = f"{uuid.uuid4().hex}.{ext}"
    return content, None


def clean_new_upload(instance):
    """Django-admin path: if ``instance.image`` holds a fresh, not yet stored
    upload, swap it for the metadata-free copy (``clean_original``). The admin
    form already validated the image; on a codec failure the upload is kept
    as is (logged) rather than losing the admin's edit."""
    field_file = instance.image
    if not field_file or getattr(field_file, "_committed", True):
        return False
    clean, err = clean_original(field_file.file)
    if err:
        import logging

        logging.getLogger(__name__).warning("admin upload kept unstripped model=%s id=%s error=%s",
                                            instance._meta.label, instance.pk, err)
        return False
    instance.image = clean
    return True


class CleanImageAdminMixin:
    """ModelAdmin mixin: strip metadata from a new ``image`` upload and queue
    its WebP variants — the same as the site's upload views."""

    def save_model(self, request, obj, form, change):
        has_image = hasattr(obj, "image_variants")
        if has_image:
            clean_new_upload(obj)
        super().save_model(request, obj, form, change)
        if has_image:
            queue_variants(obj)

    def save_formset(self, request, form, formset, change):
        # Inlines (the Profile inline on the user page).
        if not hasattr(formset.model, "image_variants"):
            return super().save_formset(request, form, formset, change)
        instances = formset.save(commit=False)
        for obj in formset.deleted_objects:
            obj.delete()
        for obj in instances:
            clean_new_upload(obj)
            obj.save()
            queue_variants(obj)
        formset.save_m2m()


def has_metadata(field_file):
    """True when the stored file carries EXIF / XMP / IPTC (GPS lives in EXIF)."""
    field_file.open("rb")
    try:
        with Image.open(field_file) as img:
            info = img.info
            return bool(info.get("exif") or info.get("xmp") or info.get("XML:com.adobe.xmp")
                        or "photoshop" in info or len(img.getexif()) > 0)
    finally:
        field_file.close()


def strip_stored_original(field_file):
    """Rewrite an already stored original without metadata, **at the same
    path** (URLs and DB rows unchanged). Only for files whose format is kept
    (JPEG / PNG / WebP — the extension must still match). Returns True when
    the file was rewritten."""
    ext = os.path.splitext(field_file.name)[1].lower().lstrip(".")
    if ext not in {"jpg", "jpeg", "png", "webp"} or not has_metadata(field_file):
        return False
    field_file.open("rb")
    try:
        clean, out_ext = strip_metadata(field_file)
    finally:
        field_file.close()
    if out_ext != {"jpeg": "jpg"}.get(ext, ext):
        return False
    storage, name = field_file.storage, field_file.name
    storage.delete(name)
    saved = storage.save(name, clean)
    if saved != name:  # someone else took the name in between — keep both, never lose data
        raise RuntimeError("stored original changed name")
    return True
