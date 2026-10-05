"""Give photos uploaded before milestone 6 random file names.

Before milestone 6 an avatar / photo report / pet photo was stored under the
name the user's phone or computer gave it — often the person's real name
(``avatars/Ivanov_Ivan.jpg``) — and ``/media/`` is public, so that name was
part of a public URL and of every page that shows the photo. New uploads are
stored as ``<uuid>.<ext>`` (``images.clean_original``); this command brings the
old ones in line. Run once at deploy, **after the media backup** (deploy/MIGRATION.md step 2),
with the app services stopped (deploy/MIGRATION.md — no upload or profile save can
race the rename), before ``backfill_image_variants``.

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
previous file) are only counted. ``--quarantine-unreferenced DIR`` moves them out
of MEDIA_ROOT (so nginx no longer serves them) into ``DIR``, keeping their
relative paths — never deletes; the runbook deletes ``DIR`` 30 days later. It
requires ``--verify-backup media.tgz`` (MIGRATION.md step 2): the archive must read to
the end without a gzip/tar error and hold every one of those files with the
same bytes, otherwise nothing is moved (exit code 1). Chat photos are not
touched: they have had random names since milestone 3 and are not public.

Idempotent; prints counts and row ids only, never a file name.
"""

import hashlib
import os
import re
import shutil
import tarfile
import uuid
from collections import defaultdict
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
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
        parser.add_argument("--quarantine-unreferenced", metavar="DIR",
                            help="Move non-random files that no row references out of MEDIA_ROOT into DIR "
                                 "(must be outside MEDIA_ROOT). Requires --verify-backup.")
        parser.add_argument("--verify-backup", metavar="MEDIA_TGZ",
                            help="The runbook's media.tgz: must be readable and contain every file to be moved.")

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        storage = default_storage
        quarantine = options["quarantine_unreferenced"]
        if quarantine:
            quarantine = self._check_quarantine_dir(quarantine, options["verify_backup"])

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

        total = sum(c["renamed"] + c["missing"] for c in per_model.values())
        self.stdout.write(f"total: {verb} {total}, changed meanwhile {skipped}, failed {failed}")

        unreferenced = self._unreferenced(storage)
        if not quarantine:
            self.stdout.write(f"unreferenced non-random files: {len(unreferenced)}"
                              + (" (move out with --quarantine-unreferenced)" if unreferenced else ""))
            return
        problems = self._verify_backup(options["verify_backup"], storage, unreferenced)
        if problems:
            raise CommandError(f"backup check failed: {problems} — nothing moved")
        self.stdout.write(f"backup check: OK, all {len(unreferenced)} unreferenced files are in it with the same bytes")
        if dry_run:
            self.stdout.write(f"unreferenced non-random files: would move {len(unreferenced)}")
            return
        media_root = Path(settings.MEDIA_ROOT)
        for rel in unreferenced:
            target = quarantine / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():  # never overwrite an earlier quarantine
                raise CommandError("a quarantined file already exists at the target — nothing more moved")
            shutil.copy2(media_root / rel, target)
            if target.stat().st_size != (media_root / rel).stat().st_size:
                raise CommandError("copy to quarantine is incomplete — original left in place")
            storage.delete(rel)
        self.stdout.write(f"unreferenced non-random files moved to quarantine: {len(unreferenced)}")

    def _check_quarantine_dir(self, path, backup):
        if not backup:
            raise CommandError("--quarantine-unreferenced needs --verify-backup <media.tgz>")
        target = Path(path).resolve()
        media_root = Path(settings.MEDIA_ROOT).resolve()
        if target == media_root or media_root in target.parents:
            raise CommandError("the quarantine directory must be outside MEDIA_ROOT (nginx serves MEDIA_ROOT)")
        if not target.is_dir() or not os.access(target, os.W_OK):
            raise CommandError("the quarantine directory must exist and be writable")
        return target

    def _verify_backup(self, archive, storage, paths):
        """Return "" when ``archive`` reads to the end and holds every path
        (under ``<MEDIA_ROOT name>/``) with identical bytes, else a short
        reason with counts only."""
        prefix = Path(settings.MEDIA_ROOT).name + "/"
        wanted = {prefix + rel: rel for rel in paths}
        found = {}
        try:
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar:
                    name = member.name[2:] if member.name.startswith("./") else member.name
                    if name in wanted and member.isfile():
                        with tar.extractfile(member) as fh:
                            found[wanted[name]] = hashlib.sha256(fh.read()).hexdigest()
                tar.fileobj.read()  # to the very end: gzip checks its CRC / length there
        except (OSError, EOFError, tarfile.TarError):
            return "archive unreadable"
        missing = differ = 0
        for rel in paths:
            if rel not in found:
                missing += 1
                continue
            with storage.open(rel, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() != found[rel]:
                    differ += 1
        if missing or differ:
            return f"{missing} not in the archive, {differ} with different bytes"
        return ""

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
