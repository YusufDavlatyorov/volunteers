"""Milestone 7 prep — deploy safety nets.

* ``check_demo_password``: read-only audit of every account against seed_demo's
  public password + demo usernames.
* ``create_initial_production_accounts`` refuses the demo password before it
  creates anything.
* ``collectstatic`` with the production storage (``ManifestStaticFilesStorage``)
  succeeds — found by the 2026-10-04 deploy rehearsal: the vendored Chart.js
  ended with ``//# sourceMappingURL=chart.umd.js.map`` but the map was not
  vendored, so collectstatic failed at runbook step 5, *after* the migrations.
* (the same rehearsal found Docker services without the shared ``./media``
  mount; since the move to systemd every unit shares one WorkingDirectory —
  locked in ``myapp.tests_deploy``.)
"""

import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.contrib.auth.hashers import PBKDF2PasswordHasher
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from accounts.models import Users
from myapp.management.commands.seed_demo import PASSWORD as DEMO_PASSWORD

OTHER_PASSWORD = "Prod-Only-Pass-2026!"


def run_check():
    out = io.StringIO()
    try:
        call_command("check_demo_password", stdout=out)
    except CommandError as exc:
        return out.getvalue(), exc
    return out.getvalue(), None


class CheckDemoPasswordTests(TestCase):
    def test_clean_database_passes(self):
        Users.objects.create_user(username="prod_admin", email="a@example.com", password=OTHER_PASSWORD)
        Users.objects.create_user(username="no_pass", email="n@example.com", password=None)  # unusable
        out, error = run_check()
        self.assertIsNone(error)
        self.assertIn("checked accounts: 2", out)
        self.assertIn("demo password accepted by: 0", out)
        self.assertIn("demo usernames present: 0", out)
        self.assertNotIn(OTHER_PASSWORD, out)

    def test_account_with_demo_password_fails_and_is_named(self):
        Users.objects.create_user(username="real_person", email="r@example.com", password=DEMO_PASSWORD,
                                  is_active=False)
        out, error = run_check()
        self.assertIsNotNone(error)
        self.assertIn("demo password accepted by: 1 — real_person (inactive)", out)
        self.assertNotIn(DEMO_PASSWORD, out)

    def test_demo_username_fails_even_with_another_password(self):
        Users.objects.create_user(username="malika_s", email="m@example.com", password=OTHER_PASSWORD)
        out, error = run_check()
        self.assertIsNotNone(error)
        self.assertIn("demo usernames present: 1 — malika_s", out)

    def test_never_writes_even_for_an_outdated_hash(self):
        # A weaker-than-current hash: Users.check_password would re-hash and
        # save it; the audit must leave the row exactly as it was.
        legacy = PBKDF2PasswordHasher().encode(DEMO_PASSWORD, "legacysalt1234", iterations=1000)
        user = Users.objects.create_user(username="legacy_user", email="l@example.com")
        Users.objects.filter(pk=user.pk).update(password=legacy)
        out, error = run_check()
        self.assertIsNotNone(error)
        self.assertIn("legacy_user (active)", out)
        user.refresh_from_db()
        self.assertEqual(user.password, legacy)


def bootstrap_env(**overrides):
    env = {}
    for prefix in ("INITIAL_ADMIN", "INITIAL_CURATOR", "INITIAL_VOLUNTEER", "INITIAL_CLIENT"):
        role = prefix.split("_")[1].lower()
        env[f"{prefix}_USERNAME"] = f"prod_{role}"
        env[f"{prefix}_EMAIL"] = f"{role}@example.com"
        env[f"{prefix}_PASSWORD"] = OTHER_PASSWORD
    env.update(overrides)
    return env


class BootstrapRefusesDemoPasswordTests(TestCase):
    def test_demo_password_refused_before_anything_is_created(self):
        with mock.patch.dict(os.environ, bootstrap_env(INITIAL_CLIENT_PASSWORD=DEMO_PASSWORD)):
            with self.assertRaisesMessage(CommandError, "INITIAL_CLIENT_PASSWORD is the public demo password"):
                call_command("create_initial_production_accounts", stdout=io.StringIO())
        self.assertFalse(Users.objects.exists())  # not even the admin listed first

    def test_other_passwords_still_work(self):
        with mock.patch.dict(os.environ, bootstrap_env()):
            call_command("create_initial_production_accounts", stdout=io.StringIO())
        self.assertEqual(Users.objects.count(), 4)
        out, error = run_check()
        self.assertIsNone(error, out)


MANIFEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}


class ProductionCollectstaticTests(SimpleTestCase):
    """The exact production static pipeline (DJANGO_STATIC_MANIFEST=True): every
    url(...) in CSS and every sourceMappingURL in JS/CSS must resolve to a real
    file, or collectstatic raises and the deploy stops halfway."""

    def test_collectstatic_with_manifest_storage(self):
        with tempfile.TemporaryDirectory(prefix="m7-static-") as root:
            with override_settings(STATIC_ROOT=root, STORAGES=MANIFEST_STORAGES):
                call_command("collectstatic", interactive=False, verbosity=0)
                manifest = json.loads((Path(root) / "staticfiles.json").read_text())["paths"]
                for name in ("vendor/chartjs-4.4.7/chart.umd.min.js", "vendor/leaflet-1.9.4/leaflet.js",
                             "js/i18n.js", "js/realtime.js", "js/chat.js", "css/style.css"):
                    self.assertIn(name, manifest)
                    self.assertTrue((Path(root) / manifest[name]).is_file())
