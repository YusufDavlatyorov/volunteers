"""Build the WebP thumb/medium variants for photos uploaded before milestone 6
(avatars, photo reports, pet photos — chat photos already have them).

Run once at deploy, after ``migrate`` (DEPLOY checklist). Idempotent: a row
whose variants match its current image is skipped. Synchronous (no Celery) —
a few hundred photos take a minute on one vCPU.

``--strip-metadata`` additionally rewrites existing originals that still carry
EXIF / GPS / XMP, at the same path (new uploads are stripped on arrival since
milestone 6). It changes stored files, so take the media backup first.
"""

from django.apps import apps
from django.core.management.base import BaseCommand

from myapp.services import images


class Command(BaseCommand):
    help = "Create missing WebP variants for avatars / photo reports / pet photos (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Only count what would be done.")
        parser.add_argument("--strip-metadata", action="store_true",
                            help="Also remove EXIF/GPS/XMP from existing originals (rewrites files in place).")

    def handle(self, *args, **options):
        dry_run, strip = options["dry_run"], options["strip_metadata"]
        totals = {"variants": 0, "stripped": 0, "failed": 0}
        for label in images.VARIANT_MODELS:
            model = apps.get_model(label)
            built = stripped = failed = 0
            for obj in model.objects.exclude(image="").order_by("pk").iterator(chunk_size=200):
                try:
                    if strip and images.has_metadata(obj.image):
                        stripped += 1
                        if not dry_run:
                            images.strip_stored_original(obj.image)
                    if not images.variants_current(obj.image, obj.image_variants):
                        built += 1
                        if not dry_run:
                            images.apply_variants(label, obj.pk)
                except Exception as exc:  # noqa: BLE001 — a missing / broken file must not stop the run
                    failed += 1
                    self.stderr.write(f"  {label} #{obj.pk}: {type(exc).__name__}")
            verb = "would build" if dry_run else "built"
            line = f"{label}: {verb} variants for {built}"
            if strip:
                line += f", {'would strip' if dry_run else 'stripped'} metadata from {stripped}"
            if failed:
                line += f", failed {failed}"
            self.stdout.write(line)
            totals["variants"] += built
            totals["stripped"] += stripped
            totals["failed"] += failed
        self.stdout.write(f"total: variants {totals['variants']}, stripped {totals['stripped']}, failed {totals['failed']}")
