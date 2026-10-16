"""The /dashboard/ redesign (2026-10-16): under the normal navbar, one 1200px
container — hero + KPI strip +
readiness / donations / regions cards + request queue + activity feed; the
volunteer and client versions in the same visual language. Locks: every role
gets 200 and its own blocks, the SOS alert shows only with open SOS reports,
volunteers/clients never get staff aggregates, and the numbers the new blocks
read (dashboard_stats additions, queue, region share) are right."""

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import EmergencyReport, Event, HelpRequest
from myapp.services import analytics, dashboard

PASSWORD = "Design-Tests-2026!"
BASE = Path(settings.BASE_DIR)


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


def _task(client, **fields):
    data = {"help_type": "grocery", "description": "d", "address": "a", "phone": "+992901234567",
            "region": "dushanbe", "status": "pending"}
    data.update(fields)
    return HelpRequest.objects.create(client=client, **data)


class DashboardRoleBlocksTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = _make("dd_admin", is_superuser=True, is_staff=True)
        cls.curator = _make("dd_cur", is_curator=True)
        cls.volunteer = _make("dd_vol", is_volunteer=True)
        cls.client_user = _make("dd_cli", is_client=True)
        cls.task = _task(cls.client_user)

    def _get(self, user):
        self.client.force_login(user)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_staff_get_hero_kpis_queue_feed_and_crm_tiles(self):
        for user in (self.admin, self.curator):
            with self.subTest(user=user.username):
                response = self._get(user)
                for marker in ('class="card dash-hero', 'class="dash-kpis"',
                               'class="card dash-card dash-queue', 'class="dash-table"', 'data-i18n="crm.activity_title"',
                               'data-i18n="profile.quick"', f'href="{reverse("crm_tasks")}"',
                               f'href="{reverse("crm_analytics")}"', f'href="{reverse("donations_admin")}"',
                               f'href="{reverse("event_list")}"', f'href="{reverse("people_list", args=["client"])}"'):
                    self.assertContains(response, marker)
                self.assertContains(response, f'{reverse("task_detail", args=[self.task.pk])}')

    def test_admin_only_items(self):
        admin = self._get(self.admin)
        curator = self._get(self.curator)
        for marker in (reverse("people_list", args=["curator"]), reverse("volunteer_applications"),
                       'data-i18n="dash.curators"'):
            self.assertContains(admin, marker)
            self.assertNotContains(curator, marker)

    def test_volunteer_blocks_and_no_staff_aggregates(self):
        response = self._get(self.volunteer)
        self.assertContains(response, 'class="card dash-hero')
        self.assertContains(response, 'class="dash-kpis dash-kpis--4"')
        self.assertContains(response, 'data-i18n="dash.recommended"')
        self.assertContains(response, 'data-i18n="review.average"')
        self._assert_no_staff(response)

    def test_client_blocks_and_no_staff_aggregates(self):
        response = self._get(self.client_user)
        self.assertContains(response, 'class="card dash-hero')
        self.assertContains(response, 'class="dash-kpis dash-kpis--4"')
        self.assertContains(response, f'href="{reverse("create_request")}"')
        self.assertContains(response, 'data-i18n="dash.your_request"')
        self._assert_no_staff(response)

    def _assert_no_staff(self, response):
        for key in ("stats", "queue", "region_share", "rating_leader", "offer_summary", "users_by_role",
                    "recent_activity", "open_emergency_count"):
            self.assertNotIn(key, response.context)
        for marker in ("dash-queue", "dash-lcard", "data-region-chart", "data-trend-chart", "dash-alert__sos",
                       reverse("crm_tasks"), reverse("emergency_list"), reverse("crm_analytics"),
                       "chart.umd.min.js"):
            self.assertNotContains(response, marker)


class NavbarNotSidebarTests(TestCase):
    """The dashboard keeps the site navbar (no sidebar, no drawer) — owner 2026-10-16."""

    def test_dashboard_has_the_navbar_and_no_sidebar_for_every_role(self):
        users = [_make("nv_adm", is_superuser=True, is_staff=True), _make("nv_cur", is_curator=True),
                 _make("nv_vol", is_volunteer=True), _make("nv_cli", is_client=True)]
        for user in users:
            with self.subTest(user=user.username):
                self.client.force_login(user)
                html = self.client.get(reverse("dashboard")).content.decode()
                self.assertIn('<nav class="topbar', html)
                self.assertIn(f'href="{reverse("dashboard")}" data-i18n="nav.dashboard" aria-current="page"', html)
                self.assertEqual(html.count('id="themeToggle"'), 1)
                self.assertEqual(html.count('id="bellPanel"'), 1)
                for marker in ("dash-sidebar", "data-dash-burger", "dash-backdrop", "dash-shell"):
                    self.assertNotIn(marker, html)
                self.assertIn('class="section dash"', html)
                profile = self.client.get(reverse("profile")).content.decode()
                self.assertIn('<nav class="topbar', profile)


class SosAlertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.curator = _make("sos_cur", is_curator=True)
        cls.volunteer = _make("sos_vol", is_volunteer=True)
        client = _make("sos_cli", is_client=True)
        cls.task = _task(client, status="active", volunteer=cls.volunteer, accepted_at=timezone.now())

    def _html(self):
        self.client.force_login(self.curator)
        return self.client.get(reverse("dashboard")).content.decode()

    def test_no_alert_without_open_sos_or_overdue(self):
        html = self._html()
        self.assertNotIn("dash-alert__sos", html)
        self.assertNotIn('class="dash-alert"', html)

    def test_alert_with_an_open_sos(self):
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        html = self._html()
        self.assertIn("dash-alert__sos", html)
        self.assertIn(f'href="{reverse("emergency_list")}?status=open"', html)
        self.assertIn('data-i18n-plural="emg.n_open"', html)

    def test_resolved_sos_hides_the_alert(self):
        report = EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        report.resolve(self.curator)
        self.assertNotIn("dash-alert__sos", self._html())

    def test_overdue_line_without_sos(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(accepted_at=timezone.now() - timedelta(hours=5))
        html = self._html()
        self.assertNotIn("dash-alert__sos", html)
        self.assertIn("dash-alert__overdue", html)
        self.assertIn('data-i18n-plural="dash.n_overdue"', html)


class DashboardStatsAdditionsTests(TestCase):
    def test_totals_week_and_upcoming_events(self):
        client = _make("st_cli", is_client=True)
        curator = _make("st_cur", is_curator=True)
        _task(client)
        _task(client, status="completed", completed_at=timezone.now())
        _task(client, status="cancelled")
        old = _task(client)
        HelpRequest.objects.filter(pk=old.pk).update(created_at=timezone.now() - timedelta(days=10))
        Event.objects.create(curator=curator, title="Soon", description="d", date=timezone.now() + timedelta(days=2))
        Event.objects.create(curator=curator, title="Past", description="d", date=timezone.now() - timedelta(days=2))

        stats = analytics.dashboard_stats()
        self.assertEqual(stats["tasks_total"], 4)
        self.assertEqual(stats["tasks_cancelled"], 1)
        self.assertEqual(stats["tasks_created_week"], 3)
        self.assertEqual(stats["events_upcoming"], 1)
        self.assertEqual(stats["tasks_total"], analytics.platform_totals()["requests_total"])

    def test_query_count_does_not_grow_with_data(self):
        client = _make("st_cli2", is_client=True)
        with CaptureQueriesContext(connection) as small:
            analytics.dashboard_stats()
        for _ in range(5):
            _task(client)
        with CaptureQueriesContext(connection) as big:
            analytics.dashboard_stats()
        self.assertEqual(len(small.captured_queries), len(big.captured_queries))


class QueueAndRegionShareTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.curator = _make("q_cur", is_curator=True)
        cls.client_user = _make("q_cli", is_client=True)
        Profile.objects.filter(user=cls.client_user).update(full_name="Zamira Karimova")
        cls.fresh = _task(cls.client_user, help_type="medical", priority="emergency", is_urgent=True)
        cls.stale_task = _task(cls.client_user, help_type="grocery")
        HelpRequest.objects.filter(pk=cls.stale_task.pk).update(created_at=timezone.now() - timedelta(days=3))
        _task(cls.client_user, region="sogd", status="active", volunteer=_make("q_vol", is_volunteer=True),
              accepted_at=timezone.now())

    def test_queue_rows_flags_and_categories(self):
        data = dashboard.for_user(self.curator)
        rows = {row["task"].pk: row for row in data["queue"]}
        self.assertEqual(set(rows), {self.fresh.pk, self.stale_task.pk})  # each pending request once
        self.assertTrue(rows[self.stale_task.pk]["stale"])
        self.assertFalse(rows[self.fresh.pk]["stale"])
        self.assertEqual(rows[self.fresh.pk]["client"], {"name": "Zamira Karimova", "initials": "ZK"})
        self.assertEqual(data["queue_categories"], [("medical", 1), ("grocery", 1)])
        # Order: urgent first, then the longest-waiting.
        self.assertEqual([row["task"].pk for row in data["queue"]], [self.fresh.pk, self.stale_task.pk])

    def test_region_share(self):
        data = dashboard.for_user(self.curator)
        self.assertEqual(data["region_open_total"], 3)
        share = {row["region"]: (row["count"], row["pct"]) for row in data["region_share"]}
        self.assertEqual(share, {"dushanbe": (2, 67), "sogd": (1, 33)})

    def test_queue_markup(self):
        self.client.force_login(self.curator)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('data-i18n="dash.waiting_long"', html)
        self.assertIn('class="dash-pill dash-pill--emergency"', html)
        self.assertIn(f'href="{reverse("crm_tasks")}?status=pending&amp;help_type=medical"', html)
        self.assertIn('data-category="grocery"', html)
        self.assertIn(">ZK<", html)
        self.assertIn("data-region-chart", html)
        self.assertIn('data-region="sogd"', html)

    def test_rating_leader_only_with_a_completed_request(self):
        self.client.force_login(self.curator)
        self.assertNotIn('data-i18n="dash.leader"', self.client.get(reverse("dashboard")).content.decode())
        volunteer = Users.objects.get(username="q_vol")
        HelpRequest.objects.filter(volunteer=volunteer).update(status="completed", completed_at=timezone.now())
        from accounts.models import clear_rating_cache

        clear_rating_cache()
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('data-i18n="dash.leader"', html)
        self.assertIn(f'href="{reverse("rating")}"', html)


class QueueCardLimitsTests(TestCase):
    """The queue card shows 11 rows of N, its tabs count all N and open the full
    queue filtered; the compact cards show at most 4 items (cut in the service)."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = _make("lim_adm", is_superuser=True, is_staff=True)
        client = _make("lim_cli", is_client=True)
        volunteer = _make("lim_vol", is_volunteer=True)
        for i in range(13):
            _task(client, help_type="medical" if i < 10 else "transport")
        for i in range(6):
            _task(client, status="active", volunteer=volunteer,
                  accepted_at=timezone.now() - timedelta(hours=5))
        for task in HelpRequest.objects.filter(status="active"):
            EmergencyReport.objects.create(help_request=task, volunteer=volunteer)

    def test_queue_has_five_rows_and_tabs_count_the_whole_queue(self):
        data = dashboard.for_user(self.admin)
        self.assertEqual(dashboard.DASH_QUEUE_LIMIT, 11)
        self.assertEqual(len(data["queue"]), 11)
        self.assertEqual(data["stats"]["tasks_pending"], 13)
        self.assertEqual(data["queue_categories"], [("medical", 10), ("transport", 3)])
        self.client.force_login(self.admin)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(html.count('class="dash-table__row"'), 11)
        self.assertRegex(html, r'<strong>11</strong> <span data-i18n="dash.of">[^<]+</span> <strong>13</strong>')
        self.assertIn(f'href="{reverse("crm_tasks")}?status=pending"', html)
        self.assertRegex(html, re.escape(f'href="{reverse("crm_tasks")}?status=pending&amp;help_type=transport"><span data-i18n="help.transport">')
                         + r'[^<]+</span><span class="dash-filter__count">3</span>')

    def test_compact_cards_hold_at_most_four_items(self):
        data = dashboard.for_user(self.admin)
        self.assertEqual(dashboard.CARD_LIST_LIMIT, 4)
        self.assertEqual(len(data["open_emergencies"]), 4)
        self.assertEqual(len(data["overdue_tasks"]), 4)
        self.client.force_login(self.admin)
        html = self.client.get(reverse("dashboard")).content.decode()
        sos = re.search(r'aria-labelledby="dashSosTitle">(.*?)</section>', html, re.S).group(1)
        self.assertEqual(sos.count('class="dash-item"'), 4)

    def test_six_identical_cards_with_one_header_and_empty_states(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(html.count('class="card dash-card dash-lcard reveal'), 6)
        self.assertEqual(html.count('class="dash-card__head dash-card__head--line"'), 7)  # + the queue
        # No applications / offers / pets in this data → short empty states, cards still there.
        for key in ("va.no_pending", "dash.no_offers_review", "pet.dash_empty"):
            self.assertIn(f'class="dash-empty" data-i18n="{key}"', html)
        self.assertIn("data-trend-chart", html)
        self.assertIn('id="dashTrendData"', html)

    def test_short_queue_has_no_padding_and_no_shown_line(self):
        HelpRequest.objects.filter(status="pending").exclude(
            pk__in=list(HelpRequest.objects.filter(status="pending").values_list("pk", flat=True)[:3])
        ).delete()
        self.client.force_login(self.admin)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(html.count('class="dash-table__row"'), 3)
        self.assertNotIn('data-i18n="dash.shown"', html)
        self.assertIn(f'href="{reverse("crm_tasks")}?status=pending"><span data-i18n="dash.queue_all">', html)

    def test_curator_gets_five_cards(self):
        curator = _make("lim_cur", is_curator=True)
        self.client.force_login(curator)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertEqual(html.count('class="card dash-card dash-lcard reveal'), 5)  # no applications card

    def test_queue_row_is_one_line_markup(self):
        HelpRequest.objects.filter(status="pending").update(created_at=timezone.now() - timedelta(days=3))
        self.client.force_login(self.admin)
        html = self.client.get(reverse("dashboard")).content.decode()
        row = re.search(r'<tr class="dash-table__row".*?</tr>', html, re.S).group(0)
        who = re.search(r'<div class="dash-who">(.*?)</div>', row, re.S).group(1)
        self.assertIn('class="dash-pill dash-pill--stale"', who)  # the pill sits in the name line
        self.assertIn('data-i18n="dash.no_suggestion"', row)
        self.assertIn('class="button ghost dash-btn-xs"', row)

    def test_full_queue_pagination_keeps_the_filter(self):
        client = _make("lim_cli2", is_client=True)
        for i in range(25):
            _task(client, help_type="medical")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm_tasks"), {"status": "pending", "help_type": "medical"})
        self.assertEqual(len(response.context["page_obj"].object_list), 20)
        self.assertEqual(response.context["page_obj"].paginator.count, 35)
        self.assertContains(response, "?status=pending&amp;help_type=medical&page=2")
        page2 = self.client.get(reverse("crm_tasks"), {"status": "pending", "help_type": "medical", "page": 2})
        self.assertEqual(len(page2.context["page_obj"].object_list), 15)
        self.assertTrue(all(t.help_type == "medical" and t.status == "pending" for t in page2.context["page_obj"]))


class NoDataNoBlockTests(TestCase):
    def test_empty_platform_hides_data_cards(self):
        curator = _make("e_cur", is_curator=True)
        self.client.force_login(curator)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        for marker in ('aria-labelledby="dashReadinessTitle"', 'aria-labelledby="dashDonationsTitle"',
                       "data-region-chart", 'data-i18n="dash.leader"', 'class="dash-alert"'):
            self.assertNotContains(response, marker)
        self.assertContains(response, 'data-i18n="dash.no_unassigned"')


class DashboardDesignRulesTests(TestCase):
    TEMPLATES = sorted((BASE / "templates/myapp/dashboard").glob("*.html")) + [BASE / "templates/myapp/dashboard.html"]

    def test_no_inline_styles_in_the_markup(self):
        for path in self.TEMPLATES:
            with self.subTest(template=path.name):
                self.assertNotIn("style=", path.read_text(encoding="utf-8"))

    def test_css_uses_tokens_not_raw_colours(self):
        css = (BASE / "static/css/dashboard.css").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", css))
        self.assertIsNone(re.search(r"rgba?\(", css))
        self.assertIn("prefers-reduced-motion", css)

    def test_page_script_sets_no_colours(self):
        js = (BASE / "static/js/dashboard.js").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{6}\b", js))
        self.assertIn("GCChartTheme", js)

    def test_every_static_key_exists_in_three_languages(self):
        source = (BASE / "static/js/i18n.js").read_text(encoding="utf-8")
        keys = set()
        for path in self.TEMPLATES:
            text = path.read_text(encoding="utf-8")
            keys.update(re.findall(r'data-i18n="([^"{]+)"', text))
            for plural in re.findall(r'plural_i18n "([^"]+)"', text):
                keys.update(f"{plural}.{form}" for form in ("one", "few", "many", "other"))
        for key in sorted(keys):
            with self.subTest(key=key):
                self.assertEqual(len(re.findall(rf'"{re.escape(key)}"\s*:', source)), 3)
