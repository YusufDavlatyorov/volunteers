"""Function-first UX pass (2026-10-15): the paths real people take.

Locks:
- "Request help" survives sign-in / sign-up: role_required keeps ``?next=``
  for an anonymous GET, the login page explains the next step and links to a
  sign-up with "I need help" pre-selected, registration returns to ``next``
  (same-site only) and ``?role=`` pre-selects the role;
- the request form: a real prompt instead of "---------", hints tied to the
  fields (aria-describedby), "Send request", the region the request goes to;
- the confirmation after creating a request never claims volunteers were told
  when nobody was reachable;
- the client sees their own request in their words ("Looking for a
  volunteer") plus what happens next — volunteers/staff keep the usual labels;
- the volunteer feed doesn't offer a region filter that could only empty it;
- irreversible actions ask first (data-confirm), flash messages are typed,
  announced and translated (every key in EN/RU/TJ, RU = the server text);
- relative times are rendered in the UI language, not Django's English.
"""

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.messages import constants
from django.contrib.messages.storage.base import Message
from django.core.cache import cache
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Users
from myapp.forms import HelpRequestForm
from myapp.models import HelpRequest
from myapp.templatetags.flash import FLASH_KEYS, flash_key
from myapp.views import REQUEST_CREATED_NOBODY, REQUEST_CREATED_NOTIFIED
from server.i18n_prerender import translation_table

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "Ux-Flows-Tests-2026!"
LANGS = ("en", "ru", "tj")


def _make(username, region="sogd", **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region=region, **flags
    )


def _task(client, **kw):
    defaults = dict(client=client, help_type="grocery", description="Хлеб и молоко",
                    address="ул. Ленина 5", phone="+992901234567", region=client.region)
    defaults.update(kw)
    return HelpRequest.objects.create(**defaults)


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class UxBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


class RequestHelpSurvivesSignInTests(UxBase):
    def test_anonymous_request_form_keeps_next(self):
        response = self.client.get(reverse("create_request") + "?help_type=grocery")
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login") + "?next=", response["Location"])
        self.assertIn("help_type%3Dgrocery", response["Location"])

    def test_anonymous_post_to_role_view_goes_to_plain_login(self):
        response = self.client.post(reverse("create_request"), {})
        self.assertEqual(response["Location"], reverse("login"))

    def test_login_explains_and_links_to_client_sign_up(self):
        html = self.client.get(reverse("login") + "?next=" + reverse("create_request")).content.decode()
        self.assertIn('class="auth-context"', html)
        self.assertIn('data-i18n="login.help_register"', html)
        self.assertIn("/register/?next=%2Fmyapp%2Frequest%2Fcreate%2F&amp;role=client", html)

    def test_plain_login_has_no_help_box(self):
        html = self.client.get(reverse("login")).content.decode()
        self.assertNotIn('class="auth-context"', html)
        self.assertIn(f'href="{reverse("register")}" data-i18n="login.register"', html)

    def test_foreign_next_is_not_carried(self):
        html = self.client.get(reverse("login") + "?next=https://evil.example/").content.decode()
        self.assertNotIn("evil.example", html)

    def test_role_param_preselects(self):
        html = self.client.get(reverse("register") + "?role=client").content.decode()
        self.assertRegex(html, r'value="client" data-role-input\s+checked')
        self.assertIn('data-i18n="reg.btn_client"', html)
        html = self.client.get(reverse("register") + "?role=volunteer").content.decode()
        self.assertRegex(html, r'value="volunteer" data-role-input\s+checked')
        self.assertIn('data-i18n="reg.btn"', html)
        html = self.client.get(reverse("register") + "?role=admin").content.decode()
        self.assertNotRegex(html, r"data-role-input\s+checked")

    def _register(self, query):
        return self.client.post(reverse("register") + query, {
            "username": "newgran", "email": "newgran@example.com", "role": "client",
            "region": "sogd", "password": "Passw0rd!23x", "confirm_password": "Passw0rd!23x",
        })

    def test_sign_up_returns_to_the_request_form(self):
        response = self._register("?next=" + reverse("create_request"))
        self.assertRedirects(response, reverse("create_request"))
        self.assertTrue(Users.objects.get(username="newgran").is_client)

    def test_volunteer_sign_up_skips_the_client_form(self):
        response = self.client.post(reverse("register") + "?next=" + reverse("create_request"), {
            "username": "newvol", "email": "newvol@example.com", "role": "volunteer",
            "region": "sogd", "password": "Passw0rd!23x", "confirm_password": "Passw0rd!23x",
        })
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_sign_up_ignores_a_foreign_next(self):
        response = self._register("?next=https://evil.example/")
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_landing_volunteer_cta_preselects(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertIn(f'href="{reverse("register")}?role=volunteer" data-i18n="hero.join"', html)


class RequestFormTests(UxBase):
    def setUp(self):
        super().setUp()
        self.client_user = _make("ux_client", is_client=True)
        self.client.force_login(self.client_user)

    def test_form_is_plain_for_the_client(self):
        html = self.client.get(reverse("create_request")).content.decode()
        self.assertNotIn("---------", html)
        self.assertIn('data-i18n="choice.select_help_type"', html)
        self.assertIn('data-i18n="form.send_request"', html)
        self.assertIn('data-i18n="region.sogd"', html)  # where the request goes
        self.assertIn('aria-describedby="id_phone_hint"', html)
        self.assertIn('id="id_phone_hint"', html)

    def test_errors_are_tied_to_their_fields(self):
        html = self.client.post(reverse("create_request"), {"priority": "normal"}).content.decode()
        self.assertIn('aria-invalid="true"', html)
        self.assertIn('id="id_description_error"', html)
        self.assertRegex(html, r'aria-describedby="id_description_hint id_description_error"')

    def test_bot_and_ai_paths_still_validate(self):
        form = HelpRequestForm({"help_type": "grocery", "description": "x", "address": "y", "phone": "901234567"})
        self.assertTrue(form.is_valid(), form.errors)

    def _create(self):
        return self.client.post(reverse("create_request"), {
            "help_type": "grocery", "priority": "normal", "description": "Хлеб",
            "address": "ул. Ленина 5", "phone": "+992 90 123 45 67",
        }, follow=True)

    def test_nobody_reachable_is_said_plainly(self):
        messages = [str(m) for m in self._create().context["messages"]]
        self.assertEqual(messages, [REQUEST_CREATED_NOBODY])

    def test_volunteers_told_only_when_they_were(self):
        _make("ux_vol", is_volunteer=True)
        messages = [str(m) for m in self._create().context["messages"]]
        self.assertEqual(messages, [REQUEST_CREATED_NOTIFIED])


class ClientWordingTests(UxBase):
    def setUp(self):
        super().setUp()
        self.client_user = _make("ux_client2", is_client=True)
        self.task = _task(self.client_user)

    def test_client_sees_their_words_and_the_next_step(self):
        self.client.force_login(self.client_user)
        html = self.client.get(reverse("task_detail", args=[self.task.pk])).content.decode()
        self.assertIn('data-i18n="task.client_status_pending"', html)
        self.assertNotIn('data-i18n="task.status_pending"', html)
        self.assertIn('class="next-step"', html)
        dash = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('data-i18n="task.client_status_pending"', dash)

    def test_staff_keep_the_usual_labels(self):
        self.client.force_login(_make("ux_cur", is_curator=True))
        html = self.client.get(reverse("task_detail", args=[self.task.pk])).content.decode()
        self.assertIn('data-i18n="task.status_pending"', html)
        self.assertNotIn("task.client_status_", html)
        self.assertNotIn('class="next-step"', html)


class VolunteerFeedTests(UxBase):
    def test_no_region_filter_for_a_regional_volunteer(self):
        self.client.force_login(_make("ux_vol2", is_volunteer=True))
        html = self.client.get(reverse("task_list")).content.decode()
        self.assertNotIn('name="region"', html)

    def test_staff_keep_the_region_filter(self):
        self.client.force_login(_make("ux_cur2", is_curator=True))
        html = self.client.get(reverse("task_list")).content.decode()
        self.assertIn('name="region"', html)

    def test_complete_asks_first(self):
        volunteer = _make("ux_vol3", is_volunteer=True)
        _task(_make("ux_cl3", is_client=True), volunteer=volunteer, status="active")
        self.client.force_login(volunteer)
        html = self.client.get(reverse("task_list")).content.decode()
        self.assertRegex(html, r'action="/myapp/tasks/\d+/complete/" class="inline-form" data-confirm="task.complete_confirm"')


class BrandLinkTests(UxBase):
    """The logo + name (navbar and footer) lead to About: a signed-in user to
    the about page (``/`` still sends them to the dashboard), a visitor to
    ``/`` — the same content and the canonical URL."""

    BRAND = re.compile(r'<a class="brand navbar__brand" href="([^"]*)"')

    def brand_hrefs(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        return self.BRAND.findall(response.content.decode())

    def test_signed_in_logo_leads_to_about(self):
        for flags in ({"is_client": True}, {"is_volunteer": True}, {"is_curator": True}, {"is_superuser": True}):
            user = _make("brand_" + next(iter(flags)), **flags)
            self.client.force_login(user)
            for url in (reverse("dashboard"), reverse("profile"), reverse("rating"), reverse("about")):
                with self.subTest(user=user.username, url=url):
                    self.assertEqual(self.brand_hrefs(url), [reverse("about")] * 2)
            self.assertRedirects(self.client.get(reverse("home")), reverse("dashboard"), fetch_redirect_response=False)

    def test_visitor_logo_leads_to_the_landing_page(self):
        for url in (reverse("home"), reverse("about"), reverse("login"), reverse("rating")):
            with self.subTest(url=url):
                self.assertEqual(self.brand_hrefs(url), [reverse("home")] * 2)

    def test_about_is_the_current_nav_item_after_the_logo(self):
        self.client.force_login(_make("brand_nav", is_client=True))
        body = self.client.get(reverse("about")).content.decode()
        self.assertRegex(body, r'<a href="%s" data-i18n="nav.about" aria-current="page"' % re.escape(reverse("about")))


class FlashI18nTests(TestCase):
    def test_every_key_exists_and_russian_is_the_server_text(self):
        tables = {lang: translation_table(lang) for lang in LANGS}
        for text, key in FLASH_KEYS:
            for lang in LANGS:
                self.assertIn(key, tables[lang], f"{key} missing in {lang}")
            if "(?P<" not in text:
                self.assertEqual(tables["ru"][key], text, key)

    def test_every_view_message_is_known(self):
        """A new messages.*("…") literal in a view must get a key here."""
        source = "".join(Path(settings.BASE_DIR, p).read_text() for p in (
            "myapp/views.py", "myapp/views_feedback.py", "myapp/views_notifications.py", "accounts/views.py"))
        literals = re.findall(r'messages\.(?:success|error|warning|info)\(\s*request,\s*"([^"{]+)"', source)
        self.assertGreater(len(literals), 40)
        for text in literals:
            self.assertIsNotNone(flash_key(text)[0], text)
        for text in (REQUEST_CREATED_NOTIFIED, REQUEST_CREATED_NOBODY):
            self.assertIsNotNone(flash_key(text)[0], text)

    def test_dynamic_parts_become_args(self):
        self.assertEqual(flash_key("Заявка пользователя anna одобрена."), ("flash.app_approved", {"name": "anna"}))
        self.assertEqual(flash_key("Something else"), (None, None))

    def test_rendered_toast_is_typed_and_announced(self):
        html = Template("{% load flash %}{% for m in msgs %}{{ m|flash_level }}|{% flash_i18n_attrs m %}|{% endfor %}").render(
            Context({"msgs": [Message(constants.ERROR, "У вас нет доступа к этой странице"),
                              Message(constants.SUCCESS, "Профиль обновлен")]}))
        self.assertIn('error| data-i18n="flash.no_access"|', html)
        self.assertIn('success| data-i18n="flash.profile_saved"|', html)


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class FlashMarkupTests(TestCase):
    def test_error_toast_is_an_alert_with_close(self):
        _make("ux_vol4", is_volunteer=True)
        self.client.login(username="ux_vol4", password=PASSWORD)
        html = self.client.get(reverse("create_request"), follow=True).content.decode()
        self.assertIn('toast--error', html)
        self.assertIn('role="alert"', html)
        self.assertIn('data-i18n="flash.no_access"', html)
        self.assertIn("data-flash-close", html)


class TimeAgoTests(TestCase):
    def render(self, delta):
        return Template("{% load i18n_plural %}{% time_ago dt %}").render(Context({"dt": timezone.now() - delta}))

    def test_russian_and_plural_aware(self):
        self.assertIn("3 часа назад", self.render(timedelta(hours=3, minutes=20)))
        self.assertIn("5 минут назад", self.render(timedelta(minutes=5)))
        self.assertIn("1 день назад", self.render(timedelta(days=1, hours=2)))
        self.assertIn("только что", self.render(timedelta(seconds=10)))
        self.assertIn('data-i18n-plural="ago.hours"', self.render(timedelta(hours=2)))

    def test_every_form_in_every_language(self):
        for lang in LANGS:
            table = translation_table(lang)
            for base in ("ago.minutes", "ago.hours", "ago.days", "ago.months", "ago.years", "emg.n_open"):
                for cat in ("one", "few", "many", "other"):
                    self.assertIn(f"{base}.{cat}", table, f"{lang}: {base}.{cat}")
            self.assertIn("ago.just_now", table)

    def test_no_english_timesince_left_in_templates(self):
        for path in Path(settings.BASE_DIR, "templates").rglob("*.html"):
            self.assertNotIn("|timesince", path.read_text(), str(path))


class NewKeysExistTests(TestCase):
    KEYS = (
        "choice.select_help_type", "form.priority_help", "form.request_desc_help", "form.address_ph",
        "form.address_help", "form.phone_help", "form.send_request", "form.request_region",
        "task.client_status_pending", "task.client_status_active", "task.client_status_completed",
        "task.client_status_cancelled", "task.client_next_title", "task.client_next_pending",
        "login.help_title", "login.help_text", "login.help_register", "reg.role_legend", "reg.btn_client",
        "common.filters", "task.complete_confirm", "va.reject_confirm", "pet.delete_confirm",
        "ai.input_label", "unit.km", "unit.min", "rec.n_active",
    )

    def test_in_all_three_languages(self):
        for lang in LANGS:
            table = translation_table(lang)
            for key in self.KEYS:
                self.assertTrue(table.get(key), f"{lang}: {key}")
