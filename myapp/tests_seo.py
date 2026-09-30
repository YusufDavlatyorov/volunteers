"""SEO: robots.txt, sitemap.xml, per-page head metadata, noindex on private
pages, JSON-LD, and the server-side Russian pre-render of i18n.js text."""

import json
import re
from xml.etree import ElementTree

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Users
from server.i18n_prerender import render_html
from server.seo import CONTACT_PHONE, PUBLIC_PAGES

SITE = "https://khayrkhoh.tj"
NOINDEX = '<meta name="robots" content="noindex, nofollow">'


def _canonical(html):
    match = re.search(r'<link rel="canonical" href="([^"]+)">', html)
    return match.group(1) if match else None


def _json_ld(html):
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    return [json.loads(block) for block in blocks]


@override_settings(SITE_URL=SITE)
class RobotsTxtTests(TestCase):
    def test_rules_and_sitemap_line(self):
        response = self.client.get("/robots.txt")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/plain; charset=utf-8")
        lines = response.content.decode().splitlines()
        self.assertEqual(lines[0], "User-agent: *")
        for path in ("/admin/", "/myapp/", "/profile/", "/update-profile/",
                     "/reset-password/", "/confirm-email/", "/forgot-password/"):
            with self.subTest(path=path):
                self.assertIn(f"Disallow: {path}", lines)
        # public pages that live under the disallowed /myapp/ prefix
        self.assertIn(f"Allow: {reverse('about')}", lines)
        self.assertIn(f"Allow: {reverse('rating')}", lines)
        self.assertIn(f"Sitemap: {SITE}/sitemap.xml", lines)
        self.assertNotIn("Disallow: /\n", response.content.decode())


@override_settings(SITE_URL=SITE)
class SitemapTests(TestCase):
    NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    def _locs(self):
        response = self.client.get("/sitemap.xml")
        self.assertEqual(response.status_code, 200)
        root = ElementTree.fromstring(response.content)
        return [loc.text for loc in root.findall("sm:url/sm:loc", self.NS)]

    def test_only_public_pages_as_absolute_https_urls(self):
        self.assertEqual(
            sorted(self._locs()),
            sorted([f"{SITE}/", f"{SITE}/login/", f"{SITE}/register/", f"{SITE}{reverse('rating')}"]),
        )

    def test_no_private_or_duplicate_urls(self):
        locs = self._locs()
        self.assertEqual(len(locs), len(set(locs)))
        for loc in locs:
            with self.subTest(loc=loc):
                self.assertTrue(loc.startswith(f"{SITE}/"))
                path = loc[len(SITE):]
                self.assertFalse(path.startswith(("/admin/", "/profile/", "/reset-password/", "/confirm-email/")))
                if path.startswith("/myapp/"):
                    self.assertEqual(path, reverse("rating"))
        # About's canonical is the home page, so it isn't listed separately.
        self.assertNotIn(f"{SITE}{reverse('about')}", locs)

    def test_host_header_does_not_leak_into_urls(self):
        response = self.client.get("/sitemap.xml", HTTP_HOST="localhost")
        self.assertNotIn(b"localhost", response.content)


@override_settings(SITE_URL=SITE)
class PublicPageHeadTests(TestCase):
    PAGES = {  # url -> expected canonical
        "/": f"{SITE}/",
        "/myapp/about/": f"{SITE}/",
        "/myapp/rating/": f"{SITE}/myapp/rating/",
        "/register/": f"{SITE}/register/",
        "/login/": f"{SITE}/login/",
    }

    def test_canonical_title_description_and_og(self):
        titles = set()
        for url, canonical in self.PAGES.items():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                html = response.content.decode()
                self.assertEqual(_canonical(html), canonical)
                self.assertNotIn(NOINDEX, html)
                title = re.search(r"<title>(.*?)</title>", html).group(1)
                titles.add(title)
                self.assertIn("KhayrKhoh", title)
                self.assertRegex(html, r'<meta name="description" content="[^"]*[а-яё][^"]*">')
                for prop in ("og:title", "og:description", "og:image", "og:type"):
                    self.assertIn(f'<meta property="{prop}"', html)
                self.assertIn(f'<meta property="og:url" content="{canonical}">', html)
                self.assertIn('<meta property="og:site_name" content="KhayrKhoh">', html)
                self.assertIn('<meta property="og:locale" content="ru_RU">', html)
                self.assertIn(f'<meta property="og:image" content="{SITE}/static/images/brand/og-image.png">', html)
                self.assertIn('<meta name="twitter:card" content="summary_large_image">', html)
                self.assertIn('<html lang="ru"', html)
                self.assertIn('rel="apple-touch-icon"', html)
                self.assertIn('images/brand/favicon.svg', html)
        self.assertEqual(len(titles), len(self.PAGES), "every public page needs a unique <title>")

    def test_home_and_about_titles_start_with_brand(self):
        for name in ("home", "about"):
            self.assertTrue(PUBLIC_PAGES[name]["title"].startswith("KhayrKhoh — "))

    def test_home_has_single_h1_with_brand_and_russian_text(self):
        html = self.client.get("/").content.decode()
        h1s = re.findall(r"<h1\b.*?</h1>", html, re.S)
        self.assertEqual(len(h1s), 1)
        self.assertIn("KhayrKhoh", h1s[0])
        # i18n.js text is pre-rendered in Russian, not the English fallback
        self.assertIn("Помоги.", h1s[0])
        self.assertNotIn("Change lives", html)

    def test_signed_in_home_redirects_to_dashboard(self):
        Users.objects.create_user(username="seo_user", email="seo@example.com", password="Volunteer2026!", is_client=True)
        self.client.login(username="seo_user", password="Volunteer2026!")
        self.assertRedirects(self.client.get("/"), reverse("profile"), fetch_redirect_response=False)


@override_settings(SITE_URL=SITE)
class StructuredDataTests(TestCase):
    def test_home_and_about_carry_ngo_and_website(self):
        for url in ("/", "/myapp/about/"):
            with self.subTest(url=url):
                blocks = _json_ld(self.client.get(url).content.decode())
                self.assertEqual(len(blocks), 1)
                data = blocks[0]
                self.assertEqual(data["@context"], "https://schema.org")
                by_type = {node["@type"]: node for node in data["@graph"]}
                org, site = by_type["NGO"], by_type["WebSite"]
                self.assertEqual(org["name"], "KhayrKhoh")
                self.assertEqual(org["url"], f"{SITE}/")
                for alt in ("Khayr Khoh", "Хайрхох", "Хайрхоҳ", "khayrkhoh.tj"):
                    self.assertIn(alt, org["alternateName"])
                self.assertEqual(org["areaServed"], "Tajikistan")
                self.assertTrue(org["logo"]["url"].startswith(f"{SITE}/static/"))
                self.assertEqual(org["contactPoint"]["telephone"], CONTACT_PHONE)
                self.assertNotIn("sameAs", org)  # no real social profiles linked yet
                self.assertEqual(site["name"], "KhayrKhoh")
                self.assertEqual(site["publisher"]["@id"], org["@id"])

    def test_other_public_pages_have_no_json_ld(self):
        self.assertEqual(_json_ld(self.client.get("/login/").content.decode()), [])

    def test_footer_phone_matches_structured_data(self):
        html = self.client.get("/").content.decode()
        self.assertIn(f"<span>{CONTACT_PHONE}</span>", html)
        digits = "+" + re.sub(r"\D", "", CONTACT_PHONE)
        self.assertIn(f'href="tel:{digits}"', html)


@override_settings(SITE_URL=SITE)
class NoindexTests(TestCase):
    def setUp(self):
        self.user = Users.objects.create_user(
            username="seo_private", email="seo_private@example.com", password="Volunteer2026!", is_client=True,
        )

    def _assert_noindex(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        html = response.content.decode()
        self.assertIn(NOINDEX, html)
        self.assertIsNone(_canonical(html))
        self.assertNotIn("application/ld+json", html)

    def test_anonymous_private_pages(self):
        self._assert_noindex("/forgot-password/")

    def test_admin_login_is_not_indexable(self):
        # Django admin ships its own robots meta ("NONE" = noindex, nofollow)
        # and never gets our public head (namespaced admin:login != login).
        html = self.client.get("/admin/login/").content.decode()
        self.assertIn('<meta name="robots" content="NONE,NOARCHIVE">', html)
        self.assertIsNone(_canonical(html))

    def test_logged_in_pages_and_myapp_crm(self):
        self.client.login(username="seo_private", password="Volunteer2026!")
        for url in (reverse("profile"), reverse("edit_profile"), reverse("map"), reverse("my_donations"), reverse("my_pet_reports")):
            with self.subTest(url=url):
                self._assert_noindex(url)

    def test_404_is_noindex(self):
        response = self.client.get("/definitely-not-a-page/")
        self.assertEqual(response.status_code, 404)
        self.assertIn(NOINDEX, response.content.decode())


class ServerSideI18nTests(TestCase):
    def test_translates_plain_text_and_placeholders(self):
        out = render_html(
            '<p data-i18n="hero.request">Request Help</p>'
            '<input type="text" placeholder="Username" data-i18n-ph="field.username">',
            "ru",
        )
        self.assertIn('<p data-i18n="hero.request">Запросить помощь</p>', out)
        self.assertIn('placeholder="Имя пользователя"', out)

    def test_interpolates_args_and_escapes(self):
        out = render_html(
            '<span data-i18n="err.password_too_short" data-i18n-args="{&quot;min_length&quot;: 8}">x</span>', "ru"
        )
        self.assertIn("не менее 8 символов", out)
        self.assertNotIn("{min_length}", out)

    def test_leaves_unknown_keys_and_nested_markup_alone(self):
        source = '<p data-i18n="no.such.key">Keep</p><p data-i18n="hero.request">A <b>b</b></p>'
        self.assertEqual(render_html(source, "ru"), source)

    def test_unknown_language_is_a_no_op(self):
        source = '<p data-i18n="hero.request">Request Help</p>'
        self.assertEqual(render_html(source, "xx"), source)
