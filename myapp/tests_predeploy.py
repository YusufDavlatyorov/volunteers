"""Final pre-deploy safety nets (2026-10-04).

* ``/health/ready/`` notices a database that doesn't match the code: an
  unapplied migration, a migration applied that the code doesn't have (old
  image on a newer schema), a missing table or column (half-restored dump,
  faked migration). Found by the deploy rehearsal: a broken level-2 restore
  left a corrupted database while readiness answered 200.
* ``rename_legacy_uploads`` gives pre-milestone-6 photos (stored under the
  user's own file name — often their real name — at a public URL) random
  names and updates the rows.
* ``check_demo_data`` reports, read-only, anything ``seed_demo`` writes;
  ``seed_demo`` refuses to run without DEMO_MODE (production never has it;
  the full guard set is in tests_demo_seed.py).
"""

import io
import os
import tarfile
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import Profile, Users
from myapp.management.commands import seed_demo
from myapp.management.commands.rename_legacy_uploads import is_random_name
from myapp.models import Event, HelpRequest, PetReport, PhotoReport
from myapp.services import images
from myapp.testing import TempMediaRoot
from server import health

_MEDIA = TempMediaRoot("gc-predeploy-")
# One sub-folder per test class: under ``--parallel`` with fork, classes of
# this module may run at the same time in different workers sharing _MEDIA.
RENAME_MEDIA = os.path.join(_MEDIA.path, "rename")
CLEAN_MEDIA = os.path.join(_MEDIA.path, "clean")
SEED_MEDIA = os.path.join(_MEDIA.path, "seed")
FAST_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def _jpeg(color="red", size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# /health/ready/
# --------------------------------------------------------------------------- #


class ReadinessSchemaTests(TestCase):
    def ready(self):
        return self.client.get(reverse("health_readiness"))

    def test_migrated_database_is_ready(self):
        resp = self.ready()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["checks"]["migrations"], "ok")
        self.assertEqual(resp.json()["checks"]["schema"], "ok")

    def test_unapplied_migration_is_not_ready(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT max(name) FROM django_migrations WHERE app = 'myapp'")
            latest = cursor.fetchone()[0]
            cursor.execute("DELETE FROM django_migrations WHERE app = 'myapp' AND name = %s", [latest])
        with self.assertLogs("server.health", "WARNING") as logs:
            resp = self.ready()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["migrations"], "error")
        self.assertIn(f"1 unapplied (first: myapp.{latest})", logs.output[0])
        self.assertNotIn(latest, resp.content.decode())  # names go to the log, never to the public body

    def test_migration_unknown_to_this_code_is_not_ready(self):
        # The database is ahead of the code — e.g. the old image restarted
        # without rolling the migrations back first.
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO django_migrations (app, name, applied) VALUES "
                           "('myapp', '9999_from_a_newer_release', CURRENT_TIMESTAMP)")
        with self.assertLogs("server.health", "WARNING") as logs:
            resp = self.ready()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["migrations"], "error")
        self.assertIn("myapp.9999_from_a_newer_release", logs.output[0])

    def test_dropped_column_is_not_ready(self):
        # A half-broken schema: every migration recorded, a column gone. The
        # migration table alone can't see this. (DDL inside the test
        # transaction — rolled back with it.)
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE myapp_petreport DROP COLUMN image_variants")
        with self.assertLogs("server.health", "WARNING") as logs:
            resp = self.ready()
        self.assertEqual(resp.status_code, 503)
        body = resp.json()["checks"]
        self.assertEqual(body["migrations"], "ok")
        self.assertEqual(body["schema"], "error")
        self.assertIn("tables: myapp_petreport", logs.output[0])
        self.assertNotIn("petreport", resp.content.decode())

    def test_missing_table_is_not_ready(self):
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE myapp_event RENAME TO myapp_event_gone")
        with self.assertLogs("server.health", "WARNING") as logs:
            resp = self.ready()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["schema"], "error")
        self.assertIn("myapp_event", logs.output[0])

    def test_connection_still_usable_after_a_failed_probe(self):
        # The probes run in their own atomic blocks: a failed statement must
        # not poison the connection for the rest of the request (PostgreSQL).
        with mock.patch.object(health, "_expected_schema",
                               return_value=(("myapp_helprequest", ("id", "no_such_column")),)):
            with self.assertLogs("server.health", "WARNING"):
                self.assertFalse(health._check_schema())
        self.assertEqual(HelpRequest.objects.count(), 0)

    def test_every_model_table_is_probed(self):
        expected = {table for table, _columns in health._expected_schema()}
        self.assertTrue({"accounts_users", "accounts_profile", "myapp_helprequest", "myapp_chatmessage",
                         "accounts_users_groups", "django_session"} <= expected)
        self.assertEqual(expected, set(connection.introspection.table_names()) - {"django_migrations"})

    def test_database_down_skips_schema_checks(self):
        with mock.patch("server.health._check_database", return_value=False), \
                mock.patch("server.health._check_migrations") as migrations, \
                mock.patch("server.health._check_schema") as schema:
            resp = self.ready()
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["migrations"], "skipped")
        migrations.assert_not_called()
        schema.assert_not_called()

    def test_migration_graph_is_built_once_per_process(self):
        health._disk_migrations()
        with mock.patch("server.health.MigrationLoader") as loader:
            self.assertEqual(self.ready().status_code, 200)
            self.assertEqual(self.ready().status_code, 200)
        loader.assert_not_called()

    def test_ready_stays_cheap(self):
        health._expected_schema()  # both built once per process
        health._disk_migrations()
        with self.assertNumQueries(6):
            # SELECT 1; the recorder's "table exists?" + django_migrations; the
            # one probe statement and its SAVEPOINT / RELEASE (the test runs
            # inside a transaction — in production it is BEGIN / COMMIT).
            self.assertEqual(self.ready().status_code, 200)


# --------------------------------------------------------------------------- #
# rename_legacy_uploads
# --------------------------------------------------------------------------- #


def rename(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command("rename_legacy_uploads", *args, stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


@override_settings(MEDIA_ROOT=RENAME_MEDIA, CELERY_BROKER_URL=None)
class RenameLegacyUploadsTests(TestCase):
    def setUp(self):
        self.root = Path(RENAME_MEDIA)
        for sub in ("avatars", "reports", "pets"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
            for f in (self.root / sub).iterdir():
                f.unlink()
        self.user = Users.objects.create_user(username="vol", email="v@example.com", password="x-Long-pass-1",
                                              is_volunteer=True)

    def put(self, name, data=None):
        data = data or _jpeg()
        (self.root / name).write_bytes(data)
        return data

    def test_legacy_avatar_gets_a_random_name_and_keeps_its_bytes_and_variants(self):
        data = self.put("avatars/Ivanov_Ivan.jpg")
        profile = Profile.objects.get(user=self.user)
        Profile.objects.filter(pk=profile.pk).update(image="avatars/Ivanov_Ivan.jpg")
        images.apply_variants("accounts.Profile", profile.pk)  # variants under the old name
        self.assertTrue((self.root / "avatars/Ivanov_Ivan_thumb.webp").exists())

        out = rename()

        profile.refresh_from_db()
        new = profile.image.name
        self.assertTrue(new.startswith("avatars/") and is_random_name(new), new)
        self.assertTrue(new.endswith(".jpg"))
        self.assertEqual((self.root / new).read_bytes(), data)
        self.assertEqual(sorted(os.listdir(self.root / "avatars")),
                         sorted(os.path.basename(p) for p in (new, *profile.image_variants.values())))
        self.assertTrue(images.variants_current(profile.image, profile.image_variants))
        self.assertIn("accounts.Profile: renamed 1", out)
        self.assertNotIn("Ivanov", out)  # counts only — never a file name

    def test_a_file_shared_by_two_rows_moves_once(self):
        self.put("reports/My Photo.JPG")
        report = PhotoReport.objects.create(author=self.user, title="t", image="reports/My Photo.JPG")
        pet = PetReport.objects.create(reporter=self.user, report_type="lost", species="cat",
                                       description="d", region="dushanbe", image="reports/My Photo.JPG")
        rename()
        report.refresh_from_db()
        pet.refresh_from_db()
        self.assertEqual(report.image.name, pet.image.name)
        self.assertTrue(is_random_name(report.image.name))
        self.assertTrue(report.image.name.endswith(".jpg"))  # extension lower-cased
        self.assertEqual(os.listdir(self.root / "reports"), [os.path.basename(report.image.name)])

    def test_row_whose_file_is_gone_still_loses_the_personal_name(self):
        report = PhotoReport.objects.create(author=self.user, title="t", image="reports/Petrova_Anna.png")
        out = rename()
        report.refresh_from_db()
        self.assertTrue(is_random_name(report.image.name))
        self.assertIn("file missing (name replaced) 1", out)

    def test_random_names_dry_run_and_second_run_change_nothing(self):
        random_name = f"pets/{uuid.uuid4().hex}.jpg"
        self.put(random_name)
        pet = PetReport.objects.create(reporter=self.user, report_type="found", species="dog",
                                       description="d", region="sogd", image=random_name)
        self.put("pets/Rex_owner_Karimov.jpg")
        other = PetReport.objects.create(reporter=self.user, report_type="lost", species="dog",
                                         description="d", region="sogd", image="pets/Rex_owner_Karimov.jpg")

        out = rename("--dry-run")
        self.assertIn("myapp.PetReport: would rename 1", out)
        other.refresh_from_db()
        self.assertEqual(other.image.name, "pets/Rex_owner_Karimov.jpg")
        self.assertTrue((self.root / "pets/Rex_owner_Karimov.jpg").exists())

        rename()
        out = rename()
        self.assertIn("total: renamed 0", out)
        pet.refresh_from_db()
        self.assertEqual(pet.image.name, random_name)

    def backup(self, files, corrupt=False):
        """A media.tgz like MIGRATION.md step 2 (``tar -C <parent> -czf media.tgz <media dir>``)."""
        fd, path = tempfile.mkstemp(suffix=".tgz")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        with tarfile.open(path, "w:gz") as tar:
            for rel, data in files.items():
                info = tarfile.TarInfo(f"{self.root.name}/{rel}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        if corrupt:
            with open(path, "r+b") as fh:
                fh.truncate(os.path.getsize(path) - 20)
        return path

    def quarantine_dir(self):
        path = tempfile.mkdtemp(prefix="gc-quarantine-")
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path

    def test_unreferenced_personal_files_are_only_counted_by_default(self):
        self.put("avatars/Old_Avatar_Of_Someone.jpg")  # left behind by an avatar change
        self.put(f"avatars/{uuid.uuid4().hex}.jpg")    # random — not a privacy problem, left alone
        out = rename()
        self.assertIn("unreferenced non-random files: 1 (move out with --quarantine-unreferenced)", out)
        self.assertEqual(len(os.listdir(self.root / "avatars")), 2)
        self.assertNotIn("Old_Avatar", out)

    def test_quarantine_moves_out_of_media_root_after_checking_the_backup(self):
        old = self.put("avatars/Old_Avatar_Of_Someone.jpg", _jpeg("blue"))
        pet = self.put("pets/Lost Dog Karimov.png", _jpeg("green"))
        keep = f"avatars/{uuid.uuid4().hex}.jpg"
        self.put(keep)
        archive = self.backup({"avatars/Old_Avatar_Of_Someone.jpg": old, "pets/Lost Dog Karimov.png": pet})
        target = self.quarantine_dir()

        out = rename("--quarantine-unreferenced", target, "--verify-backup", archive, "--dry-run")
        self.assertIn("backup check: OK, all 2", out)
        self.assertIn("would move 2", out)
        self.assertTrue((self.root / "avatars/Old_Avatar_Of_Someone.jpg").exists())

        out = rename("--quarantine-unreferenced", target, "--verify-backup", archive)
        self.assertIn("moved to quarantine: 2", out)
        self.assertEqual(Path(target, "avatars/Old_Avatar_Of_Someone.jpg").read_bytes(), old)
        self.assertEqual(Path(target, "pets/Lost Dog Karimov.png").read_bytes(), pet)
        self.assertEqual(os.listdir(self.root / "avatars"), [os.path.basename(keep)])
        self.assertEqual(os.listdir(self.root / "pets"), [])
        self.assertNotIn("Karimov", out)
        self.assertIn("unreferenced non-random files: 0", rename())

    def assert_nothing_moved(self, *args, message):
        with self.assertRaisesMessage(CommandError, message):
            rename(*args)
        self.assertTrue((self.root / "avatars/Old_Avatar_Of_Someone.jpg").exists())

    def test_quarantine_refuses_without_a_good_backup(self):
        data = self.put("avatars/Old_Avatar_Of_Someone.jpg", _jpeg("blue"))
        self.put("avatars/Second_Old.jpg", _jpeg("white"))
        target = self.quarantine_dir()
        self.assert_nothing_moved("--quarantine-unreferenced", target, message="needs --verify-backup")
        self.assert_nothing_moved("--quarantine-unreferenced", target, "--verify-backup",
                                  self.backup({"avatars/Old_Avatar_Of_Someone.jpg": data}),
                                  message="1 not in the archive, 0 with different bytes")
        self.assert_nothing_moved("--quarantine-unreferenced", target, "--verify-backup",
                                  self.backup({"avatars/Old_Avatar_Of_Someone.jpg": b"other bytes",
                                               "avatars/Second_Old.jpg": _jpeg("white")}),
                                  message="0 not in the archive, 1 with different bytes")
        self.assert_nothing_moved("--quarantine-unreferenced", target, "--verify-backup",
                                  self.backup({"avatars/Old_Avatar_Of_Someone.jpg": data,
                                               "avatars/Second_Old.jpg": _jpeg("white")}, corrupt=True),
                                  message="archive unreadable")
        self.assertEqual(os.listdir(target), [])

    def test_quarantine_dir_must_be_outside_media_root(self):
        self.put("avatars/Old_Avatar_Of_Someone.jpg")
        inside = self.root / "quarantine"
        inside.mkdir(exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(inside, ignore_errors=True))
        self.assert_nothing_moved("--quarantine-unreferenced", str(inside), "--verify-backup", "x.tgz",
                                  message="must be outside MEDIA_ROOT")

    def test_a_row_changed_meanwhile_is_left_alone(self):
        self.put("avatars/Name.jpg")
        profile = Profile.objects.get(user=self.user)
        Profile.objects.filter(pk=profile.pk).update(image="avatars/Name.jpg")
        from myapp.management.commands import rename_legacy_uploads as cmd

        real_copy = cmd._copy

        def copy_then_user_uploads(storage, src, dst):
            real_copy(storage, src, dst)
            Profile.objects.filter(pk=profile.pk).update(image="avatars/newer-upload.jpg")

        with mock.patch.object(cmd, "_copy", side_effect=copy_then_user_uploads):
            out = rename()
        profile.refresh_from_db()
        self.assertEqual(profile.image.name, "avatars/newer-upload.jpg")
        self.assertIn("changed meanwhile 1", out)
        self.assertEqual(os.listdir(self.root / "avatars"), ["Name.jpg"])  # copy removed, nothing lost


# --------------------------------------------------------------------------- #
# check_demo_data / seed_demo guard
# --------------------------------------------------------------------------- #


def audit():
    out = io.StringIO()
    try:
        call_command("check_demo_data", stdout=out)
    except CommandError as exc:
        return out.getvalue(), exc
    return out.getvalue(), None


@override_settings(MEDIA_ROOT=CLEAN_MEDIA, PASSWORD_HASHERS=FAST_HASHER, CELERY_BROKER_URL=None)
class CheckDemoDataCleanTests(TestCase):
    def test_real_looking_data_passes(self):
        user = Users.objects.create_user(username="aziz", email="aziz@example.com", password="Real-Pass-2026!",
                                         is_client=True)
        HelpRequest.objects.create(client=user, help_type="grocery", description="Купить хлеб и молоко",
                                   address="ул. Рудаки 12", phone="+992901234567", region="dushanbe")
        Event.objects.create(curator=user, title="Посадка деревьев", description="Наша настоящая акция.",
                             region="sogd", date="2026-11-01T10:00:00Z")
        out, error = audit()
        self.assertIsNone(error, out)
        self.assertIn("OK: no demo data", out)
        self.assertIn("help requests with demo text: 0", out)


@override_settings(MEDIA_ROOT=SEED_MEDIA, PASSWORD_HASHERS=FAST_HASHER, CELERY_BROKER_URL=None, DEMO_MODE=True)
class CheckDemoDataFindsSeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_demo", stdout=io.StringIO())

    def test_every_kind_of_seed_data_is_reported_and_nothing_is_deleted(self):
        before = (Users.objects.count(), HelpRequest.objects.count(), PhotoReport.objects.count())
        out, error = audit()
        self.assertIsNotNone(error)
        self.assertIn("do NOT delete anything", str(error))
        accounts = len(seed_demo.demo_usernames())
        for label in (f"accounts with a demo username: {accounts}", f"accounts accepting the demo password: {accounts}",
                      f"accounts flagged is_demo: {accounts}",
                      "events with demo text: 5", "broadcasts with demo text: 2",
                      # seed_demo writes no photo reports and no media files any more
                      "photo reports with demo text or demo file name: 0", "SOS reports with demo text: 2",
                      "catalogue items with demo text: 5", "donation offers from demo businesses: 2",
                      "pet reports with demo text: 5", "media files identical to a demo photo: 0"):
            self.assertIn(label, out)
        self.assertRegex(out, r"help requests with demo text: [1-9]\d")
        self.assertRegex(out, r"help requests flagged is_demo: [1-9]\d")
        self.assertRegex(out, r"reviews flagged is_demo: [1-9]\d")
        self.assertRegex(out, r"daily activity rows flagged is_demo: [1-9]\d")
        self.assertRegex(out, r"chat messages written by seed_demo: [1-9]")
        self.assertEqual(before, (Users.objects.count(), HelpRequest.objects.count(), PhotoReport.objects.count()))
        self.assertNotIn("Замира", out)            # no names
        self.assertNotIn("gc.local —", out)        # no e-mails, only ids
        self.assertNotIn(seed_demo.PASSWORD, out)

    def test_demo_photo_found_by_content_under_any_name(self):
        Path(SEED_MEDIA, "avatars").mkdir(parents=True, exist_ok=True)
        source = Path(settings.BASE_DIR, "static", "images", seed_demo.REPORT_PHOTOS[0])
        target = Path(SEED_MEDIA, "avatars", "Personal_Name.jpg")
        target.write_bytes(source.read_bytes())
        try:
            out, _error = audit()
        finally:
            target.unlink()
        self.assertIn("media files identical to a demo photo: 1 — avatars/", out)
        self.assertNotIn("Personal_Name", out)

    def test_photo_reports_of_an_older_seed_are_still_found(self):
        PhotoReport.objects.create(title=seed_demo.DEMO_REPORTS[0][0], description=seed_demo.DEMO_REPORTS[0][1],
                                   image=f"{seed_demo.DEMO_REPORT_IMAGE_PREFIX}1.jpg", region="dushanbe")
        out, _error = audit()
        self.assertIn("photo reports with demo text or demo file name: 1", out)


@override_settings(DEMO_MODE=False)
class SeedDemoGuardTests(TestCase):
    def test_refuses_without_demo_mode(self):
        with self.assertRaisesMessage(CommandError, "must never reach production"):
            call_command("seed_demo", stdout=io.StringIO())
        self.assertFalse(Users.objects.exists())
