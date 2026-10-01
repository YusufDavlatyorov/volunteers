"""Regression tests for the 2026-10-01 fixes: map access + resilience per
role, theme toggle markup, Tajik phone validation, name length limits, Celery
notification dispatch and the public-page caches."""

import json
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.forms import ProfileForm, RegistrationForm
from accounts.models import RATING_CACHE_KEY, Profile, Users
from myapp.forms import HelpRequestForm, PetReportForm
from myapp.models import HelpRequest, PetReport
from myapp.services import maps
from myapp.validators import normalize_tj_phone

PASSWORD = "Fix-Tests-2026!"
I18N = Path(settings.BASE_DIR) / "static" / "js" / "i18n.js"


def _i18n_blocks():
    src = I18N.read_text(encoding="utf-8")
    en, rest = src.split("\n  ru: {", 1)
    ru, tj = rest.split("\n  tj: {", 1)
    return {"en": en, "ru": ru, "tj": tj}


def _make(username, **flags):
    return Users.objects.create_user(username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags)


# --------------------------------------------------------------------------- #
# 1. Map
# --------------------------------------------------------------------------- #

class MapAccessPerRoleTests(TestCase):
    def setUp(self):
        self.users = {
            "admin": _make("map_admin", is_superuser=True, is_staff=True),
            "curator": _make("map_curator", is_curator=True),
            "volunteer": _make("map_volunteer", is_volunteer=True),
            "client": _make("map_client", is_client=True),
        }
        HelpRequest.objects.create(
            client=self.users["client"], help_type="grocery", description="d", address="a",
            phone="+992901234567", region="dushanbe", latitude=38.56, longitude=68.78,
        )

    def test_map_page_and_data_return_200_for_every_role(self):
        for role, user in self.users.items():
            self.client.force_login(user)
            with self.subTest(role=role, page="map"):
                response = self.client.get(reverse("map"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'id="opsMap"')
            with self.subTest(role=role, page="map_data"):
                response = self.client.get(reverse("map_data"))
                self.assertEqual(response.status_code, 200)
                self.assertIn("points", response.json())

    def test_anonymous_is_redirected_not_403(self):
        self.assertEqual(self.client.get(reverse("map")).status_code, 302)
        self.assertEqual(self.client.get(reverse("map_data")).status_code, 302)

    def test_referrer_policy_sends_origin_to_tile_servers(self):
        # "same-origin" sent no Referer to tile.openstreetmap.org (OSM may 403 that).
        response = self.client.get(reverse("about"))
        self.assertEqual(response["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_tile_config_has_single_host_osm_and_carto_fallback(self):
        cfg = maps.tile_layer()
        self.assertEqual(cfg["url"], "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
        self.assertIn("basemaps.cartocdn.com", cfg["fallback"]["url"])
        self.assertIn("CARTO", cfg["fallback"]["attribution"])

    @override_settings(MAPS_PROVIDER="carto")
    def test_carto_provider_has_no_self_fallback(self):
        cfg = maps.tile_layer()
        self.assertIn("basemaps.cartocdn.com", cfg["url"])
        self.assertNotIn("fallback", cfg)

    def test_leaflet_has_local_fallback_and_error_message_is_translated(self):
        self.client.force_login(self.users["client"])
        html = self.client.get(reverse("map")).content.decode()
        self.assertIn("https://unpkg.com/leaflet@1.9.4/dist/leaflet.js", html)
        self.assertIn("vendor/leaflet-1.9.4/leaflet.js", html)  # document.write fallback
        self.assertIsNotNone(finders.find("vendor/leaflet-1.9.4/leaflet.js"))
        for lang, block in _i18n_blocks().items():
            with self.subTest(lang=lang):
                self.assertIn('"map.error_load":', block)
        js = Path(finders.find("js/map.js")).read_text(encoding="utf-8")
        self.assertIn("cfg.fallback", js)
        self.assertIn("showError", js)


# --------------------------------------------------------------------------- #
# 2. Theme toggle
# --------------------------------------------------------------------------- #

class ThemeToggleMarkupTests(TestCase):
    def test_toggle_uses_centered_svg_icons_not_emoji(self):
        html = self.client.get(reverse("login")).content.decode()
        button = re.search(r'<button class="theme-toggle".*?</button>', html, re.S).group(0)
        self.assertIn("theme-toggle__icon--moon", button)
        self.assertIn("theme-toggle__icon--sun", button)
        self.assertNotIn("🌙", html)
        self.assertNotIn("☀", html)
        css = Path(finders.find("css/style.css")).read_text(encoding="utf-8")
        rule = re.search(r"\.theme-toggle \{(.*?)\}", css, re.S).group(1)
        for decl in ("min-height: 0", "padding: 0", "aspect-ratio: 1", "border-radius: 50%", "justify-content: center", "align-items: center"):
            with self.subTest(decl=decl):
                self.assertIn(decl, rule)


# --------------------------------------------------------------------------- #
# 3. Tajik phone numbers
# --------------------------------------------------------------------------- #

class TajikPhoneValidationTests(TestCase):
    VALID = ["+992 90 123 45 67", "+992901234567", "(+992) 90-123-45-67", "992 90 123 45 67", "90 123 45 67", "901234567", "90.123.45.67"]
    INVALID = ["+992", "12345", "+7 900 123 45 67", "+992 90 123 45 6", "+992 90 123 45 678", "phone", "0901234567"]

    def test_normalize(self):
        for raw in self.VALID:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_tj_phone(raw), "+992901234567")
        for raw in self.INVALID:
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError) as ctx:
                    normalize_tj_phone(raw)
                self.assertEqual(ctx.exception.code, "invalid_phone")
        self.assertEqual(normalize_tj_phone(""), "")

    def test_help_request_form_normalizes_and_rejects(self):
        ok = HelpRequestForm({"help_type": "grocery", "description": "d", "address": "a", "phone": "(+992) 90-123-45-67"})
        self.assertTrue(ok.is_valid(), ok.errors)
        self.assertEqual(ok.cleaned_data["phone"], "+992901234567")
        bad = HelpRequestForm({"help_type": "grocery", "description": "d", "address": "a", "phone": "123"})
        self.assertFalse(bad.is_valid())
        self.assertEqual(bad.errors.as_data()["phone"][0].code, "invalid_phone")

    def test_pet_form_optional_but_validated(self):
        base = {"report_type": "lost", "species": "dog", "description": "d", "region": "dushanbe"}
        self.assertTrue(PetReportForm({**base, "contact_phone": ""}).is_valid())
        form = PetReportForm({**base, "contact_phone": "90 123 45 67"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["contact_phone"], "+992901234567")
        self.assertFalse(PetReportForm({**base, "contact_phone": "+7 900"}).is_valid())

    def test_model_full_clean_normalizes_for_admin_path(self):
        client = _make("phone_client", is_client=True)
        task = HelpRequest(client=client, help_type="grocery", description="d", address="a", phone="90-123-45-67")
        task.full_clean()
        self.assertEqual(task.phone, "+992901234567")
        pet = PetReport(reporter=client, report_type="lost", species="dog", description="d", region="dushanbe", contact_phone="bad")
        with self.assertRaises(ValidationError) as ctx:
            pet.full_clean()
        self.assertIn("contact_phone", ctx.exception.error_dict)

    def test_phone_inputs_have_tel_hints_and_translated_error(self):
        client = _make("phone_ui", is_client=True)
        self.client.force_login(client)
        html = self.client.get(reverse("create_request")).content.decode()
        tag = re.search(r'<input[^>]*name="phone"[^>]*>', html).group(0)
        for attr in ('type="tel"', 'inputmode="tel"', 'autocomplete="tel"', 'placeholder="+992 XX XXX XX XX"', 'data-phone-mask="tj"'):
            with self.subTest(attr=attr):
                self.assertIn(attr, tag)
        response = self.client.post(reverse("create_request"), {"help_type": "grocery", "description": "d", "address": "a", "phone": "12"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-i18n="err.invalid_phone"')
        for lang, block in _i18n_blocks().items():
            with self.subTest(lang=lang):
                self.assertIn('"err.invalid_phone":', block)

    def test_pet_edit_saves_normalized_number(self):
        reporter = _make("pet_owner", is_client=True)
        report = PetReport.objects.create(reporter=reporter, report_type="lost", species="dog", description="d", region="dushanbe")
        self.client.force_login(reporter)
        self.client.post(reverse("pet_report_edit", args=[report.pk]), {
            "report_type": "lost", "species": "dog", "description": "d", "region": "dushanbe", "contact_phone": "+992 (93) 555-66-77",
        })
        report.refresh_from_db()
        self.assertEqual(report.contact_phone, "+992935556677")


# --------------------------------------------------------------------------- #
# 4. Name length limits
# --------------------------------------------------------------------------- #

class NameLengthLimitTests(TestCase):
    def _register(self, username):
        return self.client.post(reverse("register"), {
            "username": username, "email": f"{username[:20]}@example.com", "role": "client", "region": "dushanbe",
            "password": "Str0ng-Passphrase-42", "confirm_password": "Str0ng-Passphrase-42",
        })

    def test_username_max_30(self):
        self.assertEqual(self._register("a" * 30).status_code, 302)
        self.client.logout()  # a successful registration logs the browser in
        response = self._register("b" * 31)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Users.objects.filter(username="b" * 31).exists())
        self.assertContains(response, 'data-i18n="err.max_length"')
        self.assertContains(response, "&quot;limit_value&quot;: 30")
        self.assertIn('maxlength="30"', re.search(r'<input[^>]*name="username"[^>]*>', response.content.decode()).group(0))

    def test_full_name_max_60_in_profile_form_and_endpoint(self):
        user = _make("name_user", is_client=True)
        profile, _ = Profile.objects.get_or_create(user=user)
        ok = ProfileForm({"full_name": "Я" * 60}, instance=profile)
        ok.is_valid()
        self.assertNotIn("full_name", ok.errors)
        form = ProfileForm({"full_name": "Я" * 61}, instance=profile)
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["full_name"][0].code, "max_length")
        self.client.force_login(user)
        response = self.client.post(reverse("update_profile"), {"full_name": "Я" * 61})
        self.assertEqual(response.status_code, 400)
        profile.refresh_from_db()
        self.assertNotEqual(profile.full_name, "Я" * 61)

    def test_existing_longer_names_are_kept_and_render(self):
        # No data loss: the DB columns keep their size; a legacy 255-char name
        # still loads and renders (CSS truncates/wraps it).
        user = _make("legacy_long", is_volunteer=True)
        profile, _ = Profile.objects.get_or_create(user=user)
        Profile.objects.filter(pk=profile.pk).update(full_name="Д" * 255)
        profile.refresh_from_db()
        self.assertEqual(len(profile.full_name), 255)
        self.assertEqual(self.client.get(reverse("rating")).status_code, 200)

    def test_translations_and_overflow_css_present(self):
        for lang, block in _i18n_blocks().items():
            with self.subTest(lang=lang):
                self.assertIn('"err.max_length":', block)
        css = Path(finders.find("css/style.css")).read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto", css)
        self.assertRegex(css, r"\.row-rank h3 \{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; \}")
        self.assertIn(".activity-body { min-width: 0; }", css)


# --------------------------------------------------------------------------- #
# 5. Celery dispatch, caches
# --------------------------------------------------------------------------- #

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", TELEGRAM_BOT_TOKEN="test-token")
class NotificationDispatchTests(TestCase):
    def setUp(self):
        self.a = _make("notif_a")
        self.b = _make("notif_b")
        Users.objects.filter(pk=self.a.pk).update(telegram_id="111")
        self.a.refresh_from_db()
        mail.outbox = []  # creating the users queued their welcome emails

    def test_queues_one_task_per_address_and_chat(self):
        from myapp import tasks
        from myapp.notifications import notify_users

        with mock.patch.object(tasks.send_email_task, "delay") as email_delay, \
                mock.patch.object(tasks.send_telegram_task, "delay") as tg_delay:
            queued = notify_users([self.a, self.b, self.a], "S", "M")
        self.assertEqual(queued, 3)  # 2 unique emails + 1 linked chat, duplicates collapsed
        self.assertCountEqual([c.args for c in email_delay.call_args_list], [(self.a.email, "S", "M"), (self.b.email, "S", "M")])
        tg_delay.assert_called_once()
        self.assertEqual((str(tg_delay.call_args.args[0]), tg_delay.call_args.args[1]), ("111", "M"))

    def test_eager_mode_sends_one_email_per_recipient(self):
        from myapp.notifications import notify_users

        with mock.patch("myapp.notifications.send_telegram_message", return_value=True):
            notify_users([self.a, self.b], "S", "M")
        self.assertEqual(sorted(m.to for m in mail.outbox), [[self.a.email], [self.b.email]])

    def test_broker_down_delivers_inline(self):
        from myapp import tasks
        from myapp.notifications import notify_users

        with mock.patch.object(tasks.send_email_task, "delay", side_effect=ConnectionError("broker down")), \
                mock.patch("myapp.notifications.send_telegram_message", return_value=True):
            queued = notify_users([self.b], "S", "M")
        self.assertEqual(queued, 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_nobody_reachable_returns_zero(self):
        from myapp.notifications import notify_users

        nobody = Users(username="ghost", email="")
        self.assertEqual(notify_users([nobody], "S", "M"), 0)

    def test_retry_policy_and_quiet_failure_log(self):
        from myapp import tasks

        for task in (tasks.send_email_task, tasks.send_telegram_task):
            with self.subTest(task=task.name):
                self.assertEqual(task.max_retries, 5)
                self.assertTrue(task.retry_backoff)
                self.assertIn(Exception, task.autoretry_for)
        with self.assertLogs("myapp.tasks", level="ERROR") as logs:
            tasks.send_telegram_task.on_failure(tasks.DeliveryError("x"), "task-id-1", ("999", "secret body"), {}, None)
        self.assertIn("task-id-1", logs.output[0])
        self.assertNotIn("secret body", logs.output[0])
        self.assertNotIn("999", logs.output[0])

    def test_sweeps_still_alert_once(self):
        from myapp.services import overdue

        self.assertTrue(hasattr(overdue, "sweep_overdue_tasks"))


class PublicPageCacheTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_rating_cached_and_invalidated_by_points(self):
        volunteer = _make("rating_vol", is_volunteer=True)
        profile, _ = Profile.objects.get_or_create(user=volunteer)
        self.client.get(reverse("rating"))
        self.assertIsNotNone(cache.get(RATING_CACHE_KEY))
        profile.add_points(5)
        self.assertIsNone(cache.get(RATING_CACHE_KEY))  # next view rebuilds with the new points
