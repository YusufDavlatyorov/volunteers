"""Read-only audit: is there any ``seed_demo`` data on this database / media?

Production must hold no demo data at all — no demo accounts (their password is
public), no demo requests, events, broadcasts, photo reports, SOS, donation
offers, pet reports, chat lines, and no demo photo files. The deploy runbook
runs this **before** the deploy (with the new image, before ``migrate``) and
**after** it. If anything is found, nothing is deleted: the owner decides.

What counts as demo data is exactly what ``seed_demo`` writes — its texts,
usernames, e-mail domain and bundled photos are imported from it, so the two
can't drift apart. Rows are matched on text written by ``seed_demo`` that a
real user would not type word for word (whole description / title + body
pairs), files on byte-identical content.

Safe on both schemas: only columns that already existed in the code before
Feature Pack v2 are read, and a table that doesn't exist yet (chat before
``migrate``) is skipped. Never writes — passwords are compared without
re-saving the hash (see ``check_demo_password``). Prints counts and row ids
only, never a name, e-mail, phone or file name. Exit code 1 when anything is
found, so it can gate a script.
"""

import hashlib
import os
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Q

from myapp.management.commands import seed_demo
from myapp.management.commands.check_demo_password import DEMO_USERNAMES, demo_password_accounts
from myapp.models import (
    Broadcast,
    ChatMessage,
    Donation,
    EmergencyReport,
    Event,
    HelpRequest,
    PetReport,
    PhotoReport,
    Product,
)

MAX_IDS = 20


def _demo_photo_hashes():
    """sha256 → size of every bundled photo ``seed_demo`` copies into media/."""
    root = Path(settings.BASE_DIR) / "static" / "images"
    out = {}
    for rel in set(seed_demo.REPORT_PHOTOS):
        data = (root / rel).read_bytes()
        out[hashlib.sha256(data).hexdigest()] = len(data)
    return out


def _media_files_matching(hashes):
    """Paths (relative to MEDIA_ROOT) of files byte-identical to a demo photo.
    Only files of exactly a demo photo's size are read."""
    sizes = set(hashes.values())
    root = Path(settings.MEDIA_ROOT)
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            try:
                if path.stat().st_size not in sizes:
                    continue
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            if digest in hashes:
                found.append(str(path.relative_to(root)))
    return sorted(found)


def collect_findings():
    """``[(label, [ids or paths])]`` for every check, empty lists included."""
    tables = set(connection.introspection.table_names())
    users = get_user_model()
    results = []

    def ids(qs):
        return list(qs.order_by("pk").values_list("pk", flat=True))

    def check(label, model, q):
        if model._meta.db_table in tables:
            results.append((label, ids(model.objects.filter(q))))
        else:
            results.append((label + " (table not created yet — skipped)", []))

    check("accounts with a demo username", users, Q(username__in=DEMO_USERNAMES))
    check(f"accounts with an @{seed_demo.DEMO_EMAIL_DOMAIN} e-mail", users,
          Q(email__iendswith="@" + seed_demo.DEMO_EMAIL_DOMAIN))
    results.append(("accounts accepting the demo password", [pk for pk, _u, _a in demo_password_accounts()]))

    check("help requests with demo text", HelpRequest,
          Q(description__in=[row[1] for row in seed_demo.DEMO_REQUESTS])
          | Q(description__startswith=seed_demo.DEMO_HISTORY_PREFIX))
    pairs = Q(pk__in=[])
    for title, description, _region in seed_demo.DEMO_EVENTS:
        pairs |= Q(title=title, description=description)
    check("events with demo text", Event, pairs)
    pairs = Q(pk__in=[])
    for subject, message, _region, _count in seed_demo.DEMO_BROADCASTS:
        pairs |= Q(subject=subject, message=message)
    check("broadcasts with demo text", Broadcast, pairs)
    pairs = Q(image__startswith=seed_demo.DEMO_REPORT_IMAGE_PREFIX)
    for title, description, _region in seed_demo.DEMO_REPORTS:
        pairs |= Q(title=title, description=description)
    check("photo reports with demo text or demo file name", PhotoReport, pairs)
    check("SOS reports with demo text", EmergencyReport, Q(reason__in=seed_demo.DEMO_EMERGENCY_REASONS))
    pairs = Q(pk__in=[])
    for name, description, _category, _unit in seed_demo.DEMO_CATALOGUE:
        pairs |= Q(name=name, description=description)
    check("catalogue items with demo text", Product, pairs)
    check("donation offers from demo businesses", Donation,
          Q(organization_name__in=seed_demo.DEMO_DONATION_ORGS))
    check("pet reports with demo text", PetReport, Q(description__in=[row[6] for row in seed_demo.DEMO_PETS]))
    check("chat messages written by seed_demo", ChatMessage,
          Q(client_msg_id__startswith=seed_demo.DEMO_CHAT_MSG_ID_PREFIX))

    results.append(("media files identical to a demo photo", _media_files_matching(_demo_photo_hashes())))
    return results


class Command(BaseCommand):
    help = "Read-only: report any seed_demo data (accounts, rows, photo files) on this database. Exit code 1 if any."

    def handle(self, *args, **options):
        found = 0
        for label, items in collect_findings():
            found += len(items)
            line = f"{label}: {len(items)}"
            if items:
                if label.startswith("media"):  # a file name may be personal: folders only
                    shown = sorted({(os.path.dirname(path) or ".") + "/" for path in items})
                else:
                    shown = [f"#{pk}" for pk in items]
                line += " — " + ", ".join(shown[:MAX_IDS]) + (" …" if len(shown) > MAX_IDS else "")
            self.stdout.write(line)
        if found:
            raise CommandError(
                "demo data found — do NOT delete anything yourself: stop and tell the owner (runbook §1)"
            )
        self.stdout.write(self.style.SUCCESS("OK: no demo data"))
