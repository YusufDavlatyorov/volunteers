"""The staff compose pages — "New event" (create_event) and "Message to
volunteers" (broadcast) — in the login/register look (auth.css .auth-scene,
templates/myapp/staff_compose_form.html). Same forms, URLs, permissions and
results as before; texts in all three UI languages."""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from accounts.models import Users
from myapp.models import Broadcast, Event

PASSWORD = "Staff-Compose-2026!"
PAGES = {
    "create_event": {"title": "form.title_new_event", "sub": "form.event_sub", "submit": "admin.create_event",
                     "fields": ("title", "description", "region", "date")},
    "broadcast": {"title": "form.title_broadcast", "sub": "form.broadcast_sub", "submit": "admin.broadcast",
                  "fields": ("subject", "message", "region")},
}


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


def _i18n_block(lang):
    source = Path(settings.BASE_DIR, "static/js/i18n.js").read_text(encoding="utf-8")
    starts = {m.group(1): m.start() for m in re.finditer(r"^\s{2}(en|ru|tj): \{", source, re.M)}
    order = sorted(starts, key=starts.get)
    i = order.index(lang)
    return source[starts[lang]: starts[order[i + 1]] if i + 1 < len(order) else len(source)]


class StaffComposePagesTests(TestCase):
    def setUp(self):
        self.curator = _make("compose_curator", is_curator=True)
        self.admin = Users.objects.create_superuser("compose_admin", "compose_admin@example.com", PASSWORD)
        self.volunteer = _make("compose_vol", is_volunteer=True)

    def test_both_pages_use_the_login_register_scene(self):
        self.client.force_login(self.curator)
        for name, page in PAGES.items():
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "myapp/staff_compose_form.html")
                html = response.content.decode()
                for needle in ('class="auth-shell auth-scene"', "data-auth-bg", 'class="auth-card auth-card--compose',
                               "css/auth-tokens.css", "css/auth.css", "js/auth-bg.js",
                               f'data-i18n="{page["title"]}"', f'data-i18n="{page["sub"]}"', f'data-i18n="{page["submit"]}"',
                               'class="primary auth-submit"', "csrfmiddlewaretoken"):
                    self.assertIn(needle, html)
                for field in page["fields"]:
                    self.assertIn(f'name="{field}"', html)
                # Posts back to its own URL, as before (no action attribute).
                self.assertRegex(html, r'<form method="post" class="auth-form">')
                self.assertNotIn("style=", re.search(r'<section class="auth-shell.*?</section>', html, re.S).group(0))

    def test_new_event_is_created_as_before(self):
        self.client.force_login(self.curator)
        response = self.client.post(reverse("create_event"), {
            "title": "Субботник", "description": "Уборка парка", "region": "dushanbe", "date": "2026-11-01T10:00",
        })
        self.assertRedirects(response, reverse("admin_panel"), fetch_redirect_response=False)
        event = Event.objects.get()
        self.assertEqual((event.title, event.curator, event.region), ("Субботник", self.curator, "dushanbe"))
        self.assertTrue(event.notifications_sent)

    def test_broadcast_is_sent_as_before(self):
        _make("compose_vol2", is_volunteer=True)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("broadcast"), {"subject": "Сбор", "message": "Завтра в 10:00", "region": "all"})
        self.assertRedirects(response, reverse("admin_panel"), fetch_redirect_response=False)
        broadcast = Broadcast.objects.get()
        self.assertEqual((broadcast.subject, broadcast.sender, broadcast.region), ("Сбор", self.admin, "all"))

    def test_validation_errors_render_in_the_scene(self):
        self.client.force_login(self.curator)
        response = self.client.post(reverse("broadcast"), {"subject": "", "message": "", "region": "all"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="field-error"')
        self.assertContains(response, 'data-i18n="err.required"')
        self.assertFalse(Broadcast.objects.exists())
        response = self.client.post(reverse("create_event"), {"title": "X", "description": "", "region": "all", "date": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="field-error"')
        self.assertFalse(Event.objects.exists())

    def test_permissions_unchanged(self):
        for user in (self.volunteer, None):
            if user:
                self.client.force_login(user)
            else:
                self.client.logout()
            for name in PAGES:
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 302, name)
                self.assertNotIn("auth-card--compose", response.content.decode())

    def test_texts_in_three_languages(self):
        for lang in ("en", "ru", "tj"):
            block = _i18n_block(lang)
            for page in PAGES.values():
                for key in (page["title"], page["sub"], page["submit"]):
                    with self.subTest(lang=lang, key=key):
                        self.assertRegex(block, rf'"{re.escape(key)}":\s*"[^"]+"')

    def test_styles_are_reused_not_copied(self):
        auth_css = Path(settings.BASE_DIR, "static/css/auth.css").read_text(encoding="utf-8")
        self.assertEqual(auth_css.count(".auth-scene .auth-card--compose {"), 1)  # width: one shared rule with the donate card
        self.assertRegex(auth_css, r"\.auth-scene \.auth-card--donate,\s*\.auth-scene \.auth-card--compose \{ max-width: 440px; \}")

    def test_the_shared_task_form_is_untouched_for_other_pages(self):
        client_user = _make("compose_client", is_client=True)
        self.client.force_login(client_user)
        response = self.client.get(reverse("create_request"))
        self.assertTemplateUsed(response, "myapp/task_form.html")
        self.assertNotContains(response, "auth-scene")
