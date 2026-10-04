"""Give photos uploaded before milestone 6 random file names.

Before milestone 6 an avatar / photo report / pet photo was stored under the
name the user's phone or computer gave it — often the person's real name
(``avatars/Ivanov_Ivan.jpg``) — and ``/media/`` is public, so that name was
part of a public URL and of every page that shows the photo. New uploads are
stored as ``<uuid>.<ext>`` (``images.clean_original``); this command brings the
old ones in line. Run once at deploy, **after the media backup** (runbook §2),
before ``backfill_image_variants``.

For every stored file whose name is not already random:

1. copy it (bytes unchanged) to ``<same dir>/<uuid4 hex>.<ext>``, and its WebP
   variants, if any, to the matching ``<uuid>_<variant>.webp``;
2. point every row that references it at the new name — a conditional
   ``UPDATE … WHERE image = <old>``, so a photo replaced in the meantime is
   left alone (and the copy removed);
3. only then delete the old file and its old variants, so the old URL stops
   working. A crash between steps leaves at most a duplicate, which the next
   run reports as "unreferenced" — never a row pointing at nothing.

A row whose file is already gone from disk gets a random name too: the page
would still print the old name in its ``<img src>``.

Files under the upload folders that no row references and whose names are not
random (left behind when a user replaced their photo — Django never deletes the
previous file) are only counted; ``--delete-unreferenced`` removes them (the
runbook's ``media.tgz`` holds a copy). Chat photos are not touched: they have
had random names since milestone 3 and are not public.

Idempotent; prints counts and row ids only, never a file name.
"""

import os
import re
import uuid
from collections import defaultdict

from django.apps import apps
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.services import images

# <32 hex>.<ext> (originals since milestone 6) and <32 hex>_<variant>.webp.
RANDOM_NAME_RE = re.compile(
    r"^[0-9a-f]{32}(?:_(?:%s))?(?:\.[a-z0-9]+)?$" % "|".join(map(re.escape, images.DEFAULT_SIZES))
)


def is_random_name(path):
    return bool(RANDOM_NAME_RE.match(os.path.basename(path)))


def _upload_dirs():
    dirs = set()
    for label in images.VARIANT_MODELS:
        upload_to = apps.get_model(label)._meta.get_field("image").upload_to
        if isinstance(upload_to, str):
            dirs.add(upload_to.split("%")[0].rstrip("/"))
    return sorted(d for d in dirs if d)


def _walk(storage, top):
    try:
        subdirs, files = storage.listdir(top)
    except (FileNotFoundError, NotADirectoryError):
        return
    for name in files:
        yield f"{top}/{name}"
    for sub in subdirs:
        yield from _walk(storage, f"{top}/{sub}")


def _copy(storage, src, dst):
    with storage.open(src, "rb") as fh:
        saved = storage.save(dst, fh)
    if saved != dst:  # a uuid4 collision or a storage that renames — never guess
        storage.delete(saved)
        raise RuntimeError("storage changed the target name")


class Command(BaseCommand):
    help = ("Rename avatars / photo reports / pet photos stored under their original (possibly personal) "
            "file names to random names and update the database (idempotent).")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only count what would be done.")
        parser.add_argument("--delete-unreferenced", action="store_true",
                            help="Also delete non-random files in the upload folders that no row references.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        storage = default_storage

        # old name -> [(model, pk, variants)] across all three models: one file
        # may be shared by several rows (and even models), and must be moved once.
        refs = defaultdict(list)
        for label in images.VARIANT_MODELS:
            model = apps.get_model(label)
            for pk, name, variants in model.objects.exclude(image="").values_list("pk", "image", "image_variants"):
                refs[name].append((model, pk, variants or {}))
        legacy = {name: rows for name, rows in refs.items() if not is_random_name(name)}

        per_model = defaultdict(lambda: {"renamed": 0, "missing": 0})
        failed = skipped = 0
        for old in sorted(legacy):
            rows = legacy[old]
            exists = storage.exists(old)
            for model, _pk, _variants in rows:
                per_model[model._meta.label]["renamed" if exists else "missing"] += 1
            if dry_run:
                continue
            try:
                if not self._rename(storage, old, rows, exists):
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 — one broken file must not stop the run
                failed += 1
                ids = ", ".join(f"{m._meta.label}#{pk}" for m, pk, _v in rows)
                self.stderr.write(f"  {ids}: {type(exc).__name__}")

        verb = "would rename" if dry_run else "renamed"
        for label in images.VARIANT_MODELS:
            counts = per_model[label]
            self.stdout.write(f"{label}: {verb} {counts['renamed']}, file missing (name replaced) {counts['missing']}")

        unreferenced = self._unreferenced(storage)
        if options["delete_unreferenced"] and not dry_run:
            for path in unreferenced:
                storage.delete(path)
            self.stdout.write(f"unreferenced non-random files deleted: {len(unreferenced)}")
        else:
            self.stdout.write(f"unreferenced non-random files: {len(unreferenced)}"
                              + (" (delete with --delete-unreferenced)" if unreferenced else ""))
        total = sum(c["renamed"] + c["missing"] for c in per_model.values())
        self.stdout.write(f"total: {verb} {total}, changed meanwhile {skipped}, failed {failed}")

    def _rename(self, storage, old, rows, exists):
        """Move one file (and its variants) to a random name. False when no
        row referenced ``old`` any more by the time of the UPDATE."""
        directory = os.path.dirname(old)
        ext = os.path.splitext(old)[1].lower()
        new = f"{directory}/{uuid.uuid4().hex}{ext}" if directory else f"{uuid.uuid4().hex}{ext}"

        old_variant_files = {images.variant_name(old, v) for v in images.DEFAULT_SIZES}
        for _m, _pk, variants in rows:
            old_variant_files.update(p for p in variants.values() if p)
        new_variants, copied = {}, []
        try:
            if exists:
                _copy(storage, old, new)
                copied.append(new)
                for variant in images.DEFAULT_SIZES:
                    src = images.variant_name(old, variant)
                    if storage.exists(src):
                        dst = images.variant_name(new, variant)
                        _copy(storage, src, dst)
                        copied.append(dst)
                        new_variants[variant] = dst
            with transaction.atomic():
                updated = 0
                by_model = defaultdict(list)
                for model, pk, _v in rows:
                    by_model[model].append(pk)
                for model, pks in by_model.items():
                    updated += model.objects.filter(pk__in=pks, image=old).update(image=new, image_variants=new_variants)
        except BaseException:
            for path in copied:
                storage.delete(path)
            raise
        if not updated:
            for path in copied:
                storage.delete(path)
            return False
        # The database now points at the new name — the old URL can go.
        for path in {old, *old_variant_files} - set(copied):
            if storage.exists(path):
                storage.delete(path)
        return True

    def _unreferenced(self, storage):
        referenced = set()
        for label in images.VARIANT_MODELS:
            for name, variants in apps.get_model(label).objects.exclude(image="").values_list("image", "image_variants"):
                referenced.add(name)
                referenced.update(images.variant_name(name, v) for v in images.DEFAULT_SIZES)
                referenced.update((variants or {}).values())
        return sorted(
            path for top in _upload_dirs() for path in _walk(storage, top)
            if path not in referenced and not is_random_name(path)
        )
