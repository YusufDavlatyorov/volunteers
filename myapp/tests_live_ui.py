"""Feature Pack v2, milestone 1 — frontend wiring: navbar bell, realtime
scripts, do-not-disturb indicators/counts, i18n coverage of the new keys."""

import datetime
import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Users
from myapp.models import HelpRequest, Notification
from myapp.services import analytics
from myapp.templatetags.realtime_ui import away_last_day
from server.i18n_prerender import translation_table

PASSWORD = "Live-Ui-2026!"
TEMPLATES = Path(settings.BASE_DIR) / "templates"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


def _set_away(user, away, until=None):
    profile = user.profile
    profile.is_away = away
    profile.away_until = until
    profile.save()


class BellMarkupTests(TestCase):
    def test_bell_and_realtime_scripts_only_for_signed_in_users(self):
        html = self.client.get(reverse("login")).content.decode()
        self.assertNotIn("data-bell", html)
        self.assertNotIn("js/realtime.js", html)

        user = _make("bell_user", is_client=True)
        self.client.force_login(user)
        html = self.client.get(reverse("profile")).content.decode()
        self.assertIn("data-bell", html)
        self.assertIn('aria-expanded="false"', html)
        self.assertRegex(html, r'<script src="[^"]*js/realtime\.js[^"]*" defer')
        self.assertRegex(html, r'<script src="[^"]*js/live-ui\.js[^"]*" defer')
        self.assertIn(f'data-language-url="{reverse("profile_language")}"', html)
        self.assertIn(reverse("notifications_bell"), html)

    def test_badge_shows_server_side_unread_count(self):
        user = _make("bell_count", is_client=True)
        for i in range(3):
            Notification.objects.create(recipient=user, type="broadcast", title_key="ntf.broadcast.title",
                                        body_key="ntf.broadcast.body", params={"subject": "s", "message": "m"})
        self.client.force_login(user)
        html = self.client.get(reverse("profile")).content.decode()
        self.assertIn('data-unread="3"', html)
        self.assertRegex(html, r'data-bell-badge aria-hidden="true">3</span>')

    def test_badge_hidden_at_zero(self):
        self.client.force_login(_make("bell_zero", is_client=True))
        html = self.client.get(reverse("profile")).content.decode()
        self.assertRegex(html, r'data-bell-badge aria-hidden="true" hidden>')


class AwayIndicatorTests(TestCase):
    def setUp(self):
        self.admin = _make("away_admin", is_superuser=True, is_staff=True)
        self.away = _make("away_vol", is_volunteer=True)
        self.expired = _make("away_expired", is_volunteer=True)
        self.normal = _make("away_normal", is_volunteer=True)
        _set_away(self.away, True, timezone.now() + datetime.timedelta(days=3))
        _set_away(self.expired, True, timezone.now() - datetime.timedelta(hours=1))

    def test_analytics_counts_away_separately_and_keeps_totals(self):
        stats = analytics.dashboard_stats()
        self.assertEqual(stats["volunteers_away"], 1)
        self.assertEqual(analytics.volunteer_away_count(), 1)
        self.assertEqual(stats["volunteers_total"], 3)
        breakdown = analytics.volunteer_availability_breakdown()
        self.assertNotIn("away", breakdown)

    def test_open_ended_away_counts(self):
        _set_away(self.normal, True, None)
        self.assertEqual(analytics.volunteer_away_count(), 2)

    def test_people_list_away_filter_and_tag(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("people_list", args=["volunteer"]), {"availability": "away"})
        self.assertEqual(response.status_code, 200)
        names = {p.username for p in response.context["people"]}
        self.assertEqual(names, {"away_vol"})
        html = self.client.get(reverse("people_list", args=["volunteer"])).content.decode()
        self.assertIn(f'data-availability-user="{self.away.pk}"', html)
        self.assertEqual(len(re.findall(r'class="tag away" data-availability-away data-i18n="availability.away">', html)), 1)

    def test_admin_panel_shows_away_kpi(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_panel"))
        self.assertContains(response, 'data-i18n="crm.kpi_away"')
        self.assertContains(response, "?availability=away")

    def test_crm_volunteer_detail_shows_last_day(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm_volunteer_detail", args=[self.away.pk]))
        self.assertContains(response, 'data-i18n="availability.away_until"')

    def test_map_payload_carries_away_flag(self):
        for user, lat in ((self.away, 38.55), (self.normal, 38.56)):
            user.profile.latitude, user.profile.longitude = lat, 68.78
            user.profile.save()
        self.client.force_login(self.admin)
        points = self.client.get(reverse("map_data")).json()["points"]
        flags = {p["title"]: p["away"] for p in points if p.get("kind") == "volunteer"}
        self.assertEqual(flags, {"away_vol": True, "away_normal": False})

    def test_volunteer_dashboard_banner(self):
        self.client.force_login(self.away)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'class="away-banner')
        self.client.force_login(self.normal)
        self.assertNotContains(self.client.get(reverse("profile")), 'class="away-banner')

    def test_away_last_day_is_day_before_stored_boundary(self):
        boundary = timezone.make_aware(datetime.datetime(2026, 10, 13, 0, 0))
        self.assertEqual(away_last_day(boundary), datetime.date(2026, 10, 12))
        self.assertIsNone(away_last_day(None))


class CancelledRequestDisplayTests(TestCase):
    def test_cancelled_request_listed_in_client_history_with_i18n_status(self):
        client = _make("cx_client", is_client=True)
        HelpRequest.objects.create(client=client, help_type="grocery", description="d", address="a",
                                   phone="+992901234567", region="dushanbe", status="cancelled",
                                   cancelled_at=timezone.now())
        self.client.force_login(client)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'data-i18n="task.status_cancelled"')


class LiveUiI18nTests(TestCase):
    """Every literal data-i18n key in the templates touched by this milestone
    (and the dynamic keys the JS uses) exists in EN, RU and TJ."""

    JS_KEYS = [
        "bell.label", "bell.unread", "bell.empty", "bell.error", "bell.loading", "common.close",
        "availability.available", "availability.busy", "availability.offline", "availability.away",
        "availability.away_until", "rt.task_updated", "rt.reload", "rt.dismiss",
        "task.status_pending", "task.status_active", "task.status_completed", "task.status_cancelled",
    ]
    FILES = [
        "base.html", "myapp/notifications/list.html", "myapp/notifications/preferences.html",
        "myapp/task_detail.html", "myapp/dashboard/_volunteer.html", "myapp/dashboard/_curator.html",
        "myapp/admin_panel.html", "myapp/people_list.html", "myapp/crm_volunteer_detail.html",
        "accounts/profile.html", "myapp/_task_status_tag.html",
    ]

    def test_keys_exist_in_all_languages(self):
        keys = set(self.JS_KEYS)
        for name in self.FILES:
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            keys |= {k for k in re.findall(r'data-i18n="([^"{]+)"', text)}
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            missing = sorted(k for k in keys if k not in table)
            self.assertEqual(missing, [], f"{lang} is missing {missing}")
