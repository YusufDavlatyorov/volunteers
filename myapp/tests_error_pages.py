"""The self-contained error pages (500.html, 403_csrf.html via
templates/errors/_standalone.html): KhayrKhoh logo instead of the old "GC" tile,
all three UI languages, and nothing that could itself fail while the site is
broken (no static files, no DB, no external assets)."""

import re

from django.template.loader import render_to_string
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.models import Users

PAGES = {
    "500.html": {"ru": "Что-то пошло не так", "tj": "Хатое рух дод", "en": "Something went wrong"},
    "403_csrf.html": {"ru": "Сессия истекла", "tj": "Мӯҳлати сессия гузашт", "en": "Session expired"},
}


class StandaloneErrorPageTests(TestCase):
    def test_logo_three_languages_and_no_dependencies(self):
        for template, titles in PAGES.items():
            with self.subTest(template=template):
                html = render_to_string(template)  # no request, no context — like server_error
                self.assertNotIn(">GC<", html)
                self.assertIn('aria-label="KhayrKhoh"', html)
                self.assertEqual(html.count("<svg"), 2)  # light + dark logo, inline
                for lang, title in titles.items():
                    self.assertRegex(html, rf'<div data-l="{lang}"[^>]*>\s*<h1>{re.escape(title)}</h1>')
                for brand in ("Хайрхох", "Хайрхоҳ", "KhayrKhoh"):
                    self.assertIn(brand, html)
                # Russian without JS; the site's language / theme choice with it.
                self.assertIn('<html lang="ru">', html)
                self.assertIn("localStorage.getItem('gc-lang')", html)
                self.assertIn("localStorage.getItem('gc-theme')", html)
                # Loads nothing: no static files, stylesheets, fonts or scripts from elsewhere.
                self.assertNotRegex(html, r'(src|href)="(/static/|https?://)')
                self.assertNotIn("<link", html)
                self.assertNotIn("{%", html)
                self.assertNotIn("{#", html)

    def test_500_offers_the_contact_address(self):
        html = render_to_string("500.html")
        self.assertEqual(html.count('href="mailto:main@khayrkhoh.tj"'), 3)  # = the footer address, per language

    @override_settings(DEBUG=False)
    def test_a_real_server_error_renders_it(self):
        client = Client(raise_request_exception=False)
        with self.settings(ROOT_URLCONF="myapp.error_pages_test_urls"):
            response = client.get("/boom/")
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, "Что-то пошло не так", status_code=500)
        self.assertNotContains(response, ">GC<", status_code=500)

    def test_csrf_failure_renders_it(self):
        user = Users.objects.create_user("csrf_user", "csrf@example.com", "Csrf-Pass-2026!")
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        response = client.post(reverse("logout"))  # no token
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, "Мӯҳлати сессия гузашт", status_code=403)
        self.assertNotContains(response, ">GC<", status_code=403)
