"""About page — volunteer network section (2026-10-14).

Locks the access rule of services/network.py: the map and region names are
public, the per-region volunteer numbers reach the HTML (list, map JSON, the
"network" plaque) only for an approved volunteer (VolunteerApplication
approved — strictly, owner 2026-10-14), a curator or an admin. Also: the old
"Why we are stronger" section is gone, the counts are one cached query that
can't leak through the cache, and MapLibre is loaded lazily.
"""

import json
import re

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import ABOUT_NETWORK_CACHE_KEY, Users
from myapp.models import VolunteerApplication
from myapp.services import network
from myapp.templatetags.maplibre import MAPLIBRE

PASSWORD = "Net-Tests-2026!"
# Distinctive numbers, so "no numbers in the HTML" is checkable.
COUNTS = {"dushanbe": 7, "sogd": 3, "khatlon": 0, "gbao": 2, "rrp": 0}


def _make(username, region="dushanbe", **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region=region, **flags
    )


def _volunteer(username, region="dushanbe", status=VolunteerApplication.STATUS_APPROVED, **flags):
    user = _make(username, region=region, is_volunteer=status == VolunteerApplication.STATUS_APPROVED, **flags)
    VolunteerApplication.objects.create(user=user, region=region, status=status)
    return user


def _map_json(html):
    match = re.search(r'<script id="gc-network-map" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(match.group(1))


@override_settings(TELEGRAM_BOT_TOKEN="")
class NetworkBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        for region, n in COUNTS.items():
            for i in range(n):
                _volunteer(f"net_{region}_{i}", region=region)
        # Never counted: pending / rejected applications, a volunteer flag
        # without an application, an inactive approved volunteer.
        _volunteer("net_pending_x", region="khatlon", status=VolunteerApplication.STATUS_PENDING)
        _volunteer("net_rejected_x", region="khatlon", status=VolunteerApplication.STATUS_REJECTED)
        _make("net_flag_only", region="khatlon", is_volunteer=True)
        _volunteer("net_inactive", region="rrp", is_active=False)

    def about(self, user=None):
        if user is not None:
            self.client.force_login(user)
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def assert_no_numbers(self, html):
        self.assertNotIn("network-region__count", html)
        self.assertNotIn('data-i18n="about.net_hud"', html)
        for point in _map_json(html)["points"]:
            self.assertNotIn("count", point)
        section = html[html.index('id="network"'):html.index('id="networkMap"')]
        self.assertNotRegex(section, r"<strong>\d+</strong>")

    def assert_numbers(self, html):
        for region, n in COUNTS.items():
            row = re.search(r'data-region="' + region + r'".*?</button>', html, re.S).group(0)
            self.assertIn(f"<strong>{n}</strong>", row, region)
        points = {p["key"]: p["count"] for p in _map_json(html)["points"]}
        self.assertEqual(points, COUNTS)
        total, covered = sum(COUNTS.values()), sum(1 for n in COUNTS.values() if n)
        self.assertIn(f'data-i18n-args=\'{{"n": {total}, "r": {covered}}}\'', html)


class WhoSeesTheNumbersTests(NetworkBase):
    def test_anonymous_sees_the_map_but_no_numbers(self):
        html = self.about()
        self.assert_no_numbers(html)
        self.assertIn('id="networkMap"', html)
        self.assertIn('data-i18n="about.net_login"', html)
        self.assertIn(f'href="{reverse("login")}?next=%2Fmyapp%2Fabout%2F%23network"', html)
        self.assertIn('data-i18n="about.net_join"', html)  # "become a volunteer" → register
        for key in COUNTS:
            self.assertIn(f'data-region="{key}"', html)

    def test_volunteer_with_pending_application_sees_no_numbers(self):
        html = self.about(Users.objects.get(username="net_pending_x"))
        self.assert_no_numbers(html)
        self.assertIn('data-i18n="about.net_pending"', html)
        self.assertNotIn('data-i18n="about.net_join"', html)

    def test_rejected_flag_only_and_client_see_no_numbers(self):
        for username in ("net_rejected_x", "net_flag_only"):
            self.client.logout()
            with self.subTest(user=username):
                html = self.about(Users.objects.get(username=username))
                self.assert_no_numbers(html)
                self.assertIn('data-i18n="about.net_denied"', html)
        self.client.logout()
        html = self.about(_make("net_client", is_client=True))
        self.assert_no_numbers(html)
        self.assertIn('data-i18n="about.net_denied"', html)

    def test_approved_volunteer_sees_the_right_numbers(self):
        self.assert_numbers(self.about(Users.objects.get(username="net_sogd_0")))

    def test_curator_and_admin_see_the_numbers(self):
        self.assert_numbers(self.about(_make("net_curator", is_curator=True)))
        self.client.logout()
        self.assert_numbers(self.about(_make("net_admin", is_superuser=True, is_staff=True)))

    def test_cached_numbers_never_reach_a_visitor(self):
        self.about(_make("net_admin2", is_superuser=True, is_staff=True))  # warms the shared cache
        self.assertIsNotNone(cache.get(ABOUT_NETWORK_CACHE_KEY))
        self.client.logout()
        self.assert_no_numbers(self.about())


class NetworkServiceTests(NetworkBase):
    def test_counts_are_one_query_and_cached(self):
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(network.cached_volunteer_counts(), COUNTS)
        self.assertEqual(len(ctx.captured_queries), 1)
        with CaptureQueriesContext(connection) as ctx:
            network.cached_volunteer_counts()
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_visitor_context_never_reads_the_counts(self):
        with CaptureQueriesContext(connection) as ctx:
            data = network.for_user(None)
        self.assertEqual(len(ctx.captured_queries), 0)
        self.assertEqual(data["access"], network.LOGIN)
        self.assertNotIn("total", data)
        self.assertIsNone(cache.get(ABOUT_NETWORK_CACHE_KEY))

    def test_access_levels(self):
        get = Users.objects.get
        self.assertEqual(network.access_level(get(username="net_dushanbe_0")), network.GRANTED)
        self.assertEqual(network.access_level(get(username="net_pending_x")), network.PENDING)
        self.assertEqual(network.access_level(get(username="net_rejected_x")), network.DENIED)
        self.assertEqual(network.access_level(get(username="net_flag_only")), network.DENIED)
        self.assertEqual(network.access_level(get(username="net_inactive")), network.LOGIN)
        self.assertEqual(network.access_level(_make("net_cur", is_curator=True)), network.GRANTED)

    def test_approve_and_reject_refresh_the_counts(self):
        admin = _make("net_reviewer", is_superuser=True, is_staff=True)
        network.cached_volunteer_counts()
        application = VolunteerApplication.objects.get(user__username="net_pending_x")
        with self.captureOnCommitCallbacks(execute=True):
            application.approve(admin)
        self.assertEqual(network.cached_volunteer_counts()["khatlon"], 1)
        with self.captureOnCommitCallbacks(execute=True):
            application.reject(admin)
        self.assertEqual(network.cached_volunteer_counts()["khatlon"], 0)

    def test_every_region_has_a_center_inside_tajikistan(self):
        (west, south), (east, north) = network.TAJIKISTAN_BOUNDS
        for key, _label in network.REGION_CHOICES:
            center = network.REGION_CENTERS[key]
            self.assertTrue(south <= center["lat"] <= north and west <= center["lng"] <= east, key)


@override_settings(TELEGRAM_BOT_TOKEN="")
class SectionMarkupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_empty_database_renders_for_visitor_and_admin(self):
        html = self.client.get(reverse("about")).content.decode()
        self.assertIn('id="network"', html)
        admin = _make("net_empty_admin", is_superuser=True, is_staff=True)
        self.client.force_login(admin)
        html = self.client.get(reverse("about")).content.decode()
        self.assertEqual({p["key"]: p["count"] for p in _map_json(html)["points"]}, {k: 0 for k in COUNTS})
        self.assertIn('data-i18n-args=\'{"n": 0, "r": 0}\'', html)

    def test_old_section_is_gone(self):
        html = self.client.get(reverse("about")).content.decode()
        for marker in ("section.why", "card.roles", "card.transp", "regions-panel", "region-grid",
                       "Почему мы сильнее", "mountains-"):
            self.assertNotIn(marker, html)
        from server.i18n_prerender import translation_table

        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in ("section.why", "section.why_eyebrow", "section.regions", "card.roles", "card.ai_desc",
                        "about.regions_photo_alt"):
                self.assertNotIn(key, table, f"{lang} {key}")

    def test_no_fabricated_mock_numbers(self):
        html = self.client.get(reverse("about")).content.decode()
        for fake in ("142", "джамоат", "24/7"):
            self.assertNotIn(fake, html)

    def test_maplibre_is_lazy_with_the_same_sri(self):
        html = self.client.get(reverse("about")).content.decode()
        self.assertNotIn('<script src="https://unpkg.com', html)
        self.assertNotIn('<link rel="stylesheet" href="https://unpkg.com', html)
        cfg = json.loads(re.search(r'<script id="gc-maplibre-lazy" type="application/json">(.*?)</script>', html, re.S).group(1))
        self.assertEqual(cfg["js"], MAPLIBRE["js"])
        self.assertEqual(cfg["js_sri"], MAPLIBRE["js_sri"])
        self.assertEqual(cfg["css_sri"], MAPLIBRE["css_sri"])
        self.assertIn("vendor/maplibre-gl-5.24.0/maplibre-gl.js", cfg["js_local"])
        self.assertIn("js/map.js", cfg["map_js"])
        self.assertIn("js/about-network.js", html)

    def test_new_keys_exist_in_every_language(self):
        from server.i18n_prerender import translation_table

        _volunteer("net_keys_vol")
        self.client.force_login(Users.objects.get(username="net_keys_vol"))
        html = self.client.get(reverse("about")).content.decode()
        section = html[html.index('id="network"'):html.index("</section>", html.index('id="network"'))]
        keys = set(re.findall(r'data-i18n(?:-aria)?="([^"]+)"', section))
        keys |= {"about.net_login", "about.net_pending", "about.net_denied", "about.net_join",
                 "map.coop_windows", "map.coop_mac", "map.coop_mobile"}
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in sorted(keys):
                self.assertTrue(table.get(key), f"{lang} {key}")
