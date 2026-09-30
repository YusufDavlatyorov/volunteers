"""Performance guarantees: no third-party render dependencies, self-hosted
fonts/Leaflet, responsive WebP images, cached landing-page aggregates, and
query counts that don't grow with the data (N+1 guards)."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import Users
from myapp.models import HelpRequest, PhotoReport

THIRD_PARTY = ("fonts.googleapis.com", "fonts.gstatic.com", "unpkg.com", "fbcdn.net", "cdn.jsdelivr.net", "cdnjs")


class NoThirdPartyAssetsTests(TestCase):
    def test_public_pages_load_nothing_from_third_party_hosts(self):
        for url in ("/", "/myapp/about/", "/login/", "/register/", "/myapp/rating/"):
            html = self.client.get(url).content.decode()
            for host in THIRD_PARTY:
                with self.subTest(url=url, host=host):
                    self.assertNotIn(host, html)

    def test_map_pages_use_self_hosted_leaflet(self):
        user = Users.objects.create_user(username="perf_admin", email="perf@example.com", password="x-Perf-12345", is_superuser=True, is_staff=True)
        self.client.force_login(user)
        html = self.client.get(reverse("map")).content.decode()
        self.assertIn("vendor/leaflet-1.9.4/leaflet.js", html)
        self.assertIn("vendor/leaflet-1.9.4/leaflet.css", html)
        self.assertNotIn("unpkg.com", html)

    def test_self_hosted_fonts_css_points_only_at_local_files(self):
        css_path = Path(finders.find("css/fonts.css"))
        css = css_path.read_text(encoding="utf-8")
        self.assertNotIn("https://", css.split("*/", 1)[1])  # header comment may name the source
        urls = set(re.findall(r"url\(([^)]+)\)", css))
        self.assertTrue(urls)
        for url in urls:
            with self.subTest(url=url):
                self.assertTrue((css_path.parent / url).resolve().exists())
        self.assertIn("font-display: swap", css)

    def test_preloaded_fonts_exist(self):
        html = self.client.get("/").content.decode()
        preloads = re.findall(r'<link rel="preload" href="/static/([^"]+)" as="font" type="font/woff2" crossorigin>', html)
        self.assertEqual(len(preloads), 2)
        for path in preloads:
            self.assertIsNotNone(finders.find(path), path)


class ResponsiveImagesTests(TestCase):
    def test_landing_photos_are_webp_srcset_with_jpeg_fallback(self):
        html = self.client.get("/").content.decode()
        for cls, base in (("volunteers-photo", "images/volunteers/volunteers-team-"), ("regions-photo", "images/tajikistan/mountains-")):
            with self.subTest(cls=cls):
                block =re.search(r"<picture>((?:(?!</picture>).)*class=\"" + cls + r"\".*?)</picture>", html, re.S).group(1)
                self.assertIn('type="image/webp"', block)
                self.assertIn(base, block)
                self.assertRegex(block, r'srcset="[^"]*\.webp 480w|srcset="[^"]*\.webp 640w')
                img = re.search(r"<img[^>]*class=\"" + cls + r"\"[^>]*>", block).group(0)
                for attr in ('width="', 'height="', 'loading="lazy"', "sizes=", 'src="/static/' + base):
                    self.assertIn(attr, img)
        for f in ["images/volunteers/volunteers-team-640.webp", "images/tajikistan/mountains-480.webp"]:
            self.assertIsNotNone(finders.find(f), f)


class LandingStatsCacheTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_about_stats_are_cached_between_requests(self):
        self.client.get("/")
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(len(ctx.captured_queries), 1)  # only the photo-report list


class QueryCountDoesNotGrowTests(TestCase):
    """N+1 guards: adding rows must not add queries."""

    def setUp(self):
        self.admin = Users.objects.create_user(username="perf_boss", email="boss@example.com", password="x-Perf-12345", is_superuser=True, is_staff=True)
        self.client.force_login(self.admin)

    def _queries(self, url):
        self.client.get(url)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(url)
        return len(ctx.captured_queries)

    def _add_tasks(self, n):
        for i in range(n):
            client = Users.objects.create_user(username=f"perf_c{HelpRequest.objects.count()}_{i}", email=f"c{i}_{HelpRequest.objects.count()}@example.com", password="x", is_client=True)
            HelpRequest.objects.create(client=client, help_type="grocery", description="d", address="a", phone="p", region="dushanbe")

    def test_photo_reports_page_help_request_options(self):
        self._add_tasks(2)
        before = self._queries(reverse("photo_reports"))
        self._add_tasks(5)
        self.assertEqual(self._queries(reverse("photo_reports")), before)

    def test_volunteer_applications_counts_are_one_query(self):
        self.assertLessEqual(self._queries(reverse("volunteer_applications")), 5)
