"""/myapp/panel/ in the dashboard's look (2026-10-16): admin + curator get it,
the curator never sees volunteer applications, one request table with
free / busy tabs, working filters, 20-row pages whose links keep tab + filters,
the SOS alert only with open SOS, "Check overdue" still a POST, and a query
count that does not grow with the rows (candidates come from one batched
matching query — matching.best_volunteer_per_task, locked equal to
recommend_volunteers(task, limit=1) here)."""

import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import EmergencyReport, HelpRequest, VolunteerApplication
from myapp.services import dashboard, matching

PASSWORD = "Panel-Tests-2026!"


def _make(username, region="dushanbe", **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region=region, **flags
    )


def _task(client, **fields):
    data = {"help_type": "grocery", "description": "d", "address": "a", "phone": "+992901234567",
            "region": "dushanbe", "status": "pending"}
    data.update(fields)
    return HelpRequest.objects.create(client=client, **data)


def _locate(user, lat, lng):
    Profile.objects.filter(user=user).update(
        latitude=Decimal(str(lat)), longitude=Decimal(str(lng)),
        location_updated_at=timezone.now(), availability_status=Profile.AVAILABILITY_AVAILABLE,
    )


class PanelAccessAndLayoutTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = _make("pa_admin", is_superuser=True, is_staff=True)
        cls.curator = _make("pa_cur", is_curator=True)
        cls.applicant = _make("pa_applicant")
        VolunteerApplication.objects.create(user=cls.applicant, reason="m", region="dushanbe")

    def _get(self, user, query=""):
        self.client.force_login(user)
        response = self.client.get(reverse("admin_panel") + query)
        self.assertEqual(response.status_code, 200)
        return response

    def test_admin_and_curator_get_the_dashboard_look(self):
        for user in (self.admin, self.curator):
            with self.subTest(user=user.username):
                html = self._get(user).content.decode()
                self.assertIn("css/dashboard.css", html)
                self.assertIn("css/panel.css", html)
                self.assertIn("topbar", html)
                self.assertNotIn("sidebar", html)
                for marker in ('<section class="section dash">', 'class="card dash-hero', 'data-i18n="panel.title"',
                               'class="panel-band__grid', 'class="dash-row dash-row--cards"', 'id="requests"',
                               'data-i18n="crm.activity_title"'):
                    self.assertIn(marker, html)
                # The old grids are gone.
                self.assertNotIn("opportunity-grid", html)

    def test_curator_never_sees_volunteer_applications(self):
        admin = self._get(self.admin)
        curator = self._get(self.curator)
        self.assertIn("recent_applications", admin.context)
        self.assertNotIn("recent_applications", curator.context)
        self.assertContains(admin, 'id="panelAppsTitle"')
        self.assertNotContains(curator, 'id="panelAppsTitle"')
        # The curator gets the upcoming events in that place instead.
        self.assertContains(curator, 'id="panelEventsTitle"', count=1)
        self.assertContains(admin, 'id="panelEventsTitle"', count=1)

    def test_every_kpi_keeps_its_filter_link(self):
        html = self._get(self.admin).content.decode()
        for href in (
            reverse("emergency_list") + "?status=open", reverse("emergency_list") + "?status=acknowledged",
            reverse("people_list", args=["volunteer"]) + "?availability=available",
            reverse("people_list", args=["volunteer"]) + "?availability=busy",
            reverse("people_list", args=["volunteer"]) + "?availability=offline",
            reverse("people_list", args=["volunteer"]) + "?availability=away",
            reverse("people_list", args=["client"]),
            reverse("crm_tasks") + "?status=pending", reverse("crm_tasks") + "?status=active",
            reverse("crm_tasks") + "?overdue=1", reverse("crm_tasks") + "?is_urgent=on",
            reverse("completed_tasks"), reverse("volunteer_applications"),
        ):
            self.assertIn(f'href="{href}"', html, href)
        for href in (reverse("crm_tasks"), reverse("broadcast"), reverse("create_event"), reverse("crm_photo_reports"),
                     reverse("rating"), reverse("crm_reviews"), reverse("crm_analytics")):
            self.assertIn(f'href="{href}"', html, href)

    def test_hero_actions_are_icons_plus_a_more_menu(self):
        html = self._get(self.admin).content.decode()
        icons = html.split('class="panel-actions__icons"')[1].split("</div>")[0]
        menu = html.split('class="panel-more__menu"')[1].split("</div>")[0]
        for block in (icons, menu):
            self.assertEqual(block.count(f'href="{reverse("crm_analytics")}"'), 1)
            self.assertIn(f'action="{reverse("check_overdue")}"', block)
            self.assertIn("csrfmiddlewaretoken", block)
        self.assertIn('<details class="panel-more">', html)

    def test_check_overdue_still_posts_back_to_the_panel(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("check_overdue"))
        self.assertRedirects(response, reverse("admin_panel"))


class PanelSosAlertTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = _make("ps_admin", is_superuser=True, is_staff=True)
        cls.volunteer = _make("ps_vol", is_volunteer=True)
        cls.task = _task(_make("ps_cli", is_client=True), status="active", volunteer=cls.volunteer,
                         accepted_at=timezone.now())

    def _html(self):
        self.client.force_login(self.admin)
        return self.client.get(reverse("admin_panel")).content.decode()

    def test_no_alert_without_open_sos(self):
        html = self._html()
        self.assertNotIn('class="dash-alert"', html)
        self.assertNotIn("panel-kpi--alert", html)

    def test_alert_and_red_kpi_with_an_open_sos(self):
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer, reason="r")
        html = self._html()
        self.assertIn('class="dash-alert__row dash-alert__sos"', html)
        self.assertIn(f'href="{reverse("emergency_list")}?status=open" data-i18n="dash.triage"', html)
        self.assertRegex(html, r'class="dash-kpi panel-kpi panel-kpi--alert" href="[^"]*\?status=open"')
        self.assertIn('data-i18n="panel.total">', html)

    def test_overdue_line_links_to_the_overdue_filter(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(accepted_at=timezone.now() - timedelta(hours=5))
        html = self._html()
        self.assertIn('class="dash-alert__row dash-alert__overdue" href="/myapp/crm/tasks/?overdue=1"', html)


class PanelRequestTableTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = _make("pt_admin", is_superuser=True, is_staff=True)
        cls.client_user = _make("pt_cli", is_client=True)
        cls.volunteer = _make("pt_vol", is_volunteer=True)
        Profile.objects.filter(user=cls.volunteer).update(full_name="Malika Safarova")
        cls.free_med = _task(cls.client_user, help_type="medical", region="sogd", priority="high", is_urgent=True)
        cls.free_groc = _task(cls.client_user, help_type="grocery")
        cls.busy = _task(cls.client_user, help_type="transport", status="active", volunteer=cls.volunteer,
                         accepted_at=timezone.now())
        _task(cls.client_user, status="completed")

    def _get(self, query=""):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("admin_panel") + query)
        self.assertEqual(response.status_code, 200)
        return response

    def _ids(self, response):
        return [row["task"].pk for row in response.context["rows"]]

    def test_free_tab_is_the_default_and_shows_pending_only(self):
        response = self._get()
        self.assertEqual(response.context["tab"], "free")
        self.assertEqual(set(self._ids(response)), {self.free_med.pk, self.free_groc.pk})
        self.assertEqual(response.context["tab_counts"], {"free": 2, "busy": 1})
        # Urgent first.
        self.assertEqual(self._ids(response)[0], self.free_med.pk)
        self.assertContains(response, 'class="dash-filter__tab is-active" href="?tab=free#requests" aria-current="page"')
        self.assertContains(response, 'data-i18n="panel.col.candidate"')

    def test_busy_tab_shows_active_only_with_the_assigned_volunteer(self):
        response = self._get("?tab=busy")
        self.assertEqual(response.context["tab"], "busy")
        self.assertEqual(self._ids(response), [self.busy.pk])
        self.assertContains(response, 'class="dash-suggest dash-suggest--assigned">Malika Safarova<')
        self.assertContains(response, 'data-i18n="dash.col.volunteer"')
        self.assertContains(response, f'href="{reverse("crm_tasks")}?status=active"')

    def test_unknown_tab_falls_back_to_free(self):
        self.assertEqual(self._get("?tab=nope").context["tab"], "free")

    def test_the_filter_form_is_rendered_and_filters(self):
        response = self._get()
        for name in ("help_type", "region", "priority", "is_urgent"):
            self.assertContains(response, f'name="{name}"')
        self.assertContains(response, '<input type="hidden" name="tab" value="free">')
        cases = {
            "?help_type=medical": [self.free_med.pk],
            "?region=dushanbe": [self.free_groc.pk],
            "?priority=high": [self.free_med.pk],
            "?is_urgent=on": [self.free_med.pk],
            "?tab=busy&help_type=medical": [],
            "?tab=busy&help_type=transport": [self.busy.pk],
        }
        for query, expected in cases.items():
            with self.subTest(query=query):
                self.assertEqual(self._ids(self._get(query)), expected)
        # Tab counters follow the filters.
        self.assertEqual(self._get("?help_type=medical").context["tab_counts"], {"free": 1, "busy": 0})

    def test_filters_survive_tab_switches_and_the_crm_link(self):
        response = self._get("?tab=busy&help_type=transport")
        self.assertContains(response, 'href="?tab=free&amp;help_type=transport#requests"')
        self.assertContains(response, f'href="{reverse("crm_tasks")}?status=active&amp;help_type=transport"')
        self.assertContains(response, '<a class="button ghost dash-btn-sm" href="?tab=busy#requests" data-i18n="crm.clear_filters">')

    def test_empty_states(self):
        self.assertContains(self._get("?help_type=documents"), 'class="dash-empty" data-i18n="crm.no_tasks"')
        HelpRequest.objects.filter(status="active").delete()
        self.assertContains(self._get("?tab=busy"), 'class="dash-empty" data-i18n="admin.no_busy"')


class PanelPaginationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = _make("pp_admin", is_superuser=True, is_staff=True)
        client_user = _make("pp_cli", is_client=True)
        for _ in range(21):
            _task(client_user, help_type="medical")
        _task(client_user, help_type="grocery")

    def _get(self, query):
        self.client.force_login(self.admin)
        return self.client.get(reverse("admin_panel") + query)

    def test_21_requests_make_two_pages_and_links_keep_tab_and_filters(self):
        first = self._get("?tab=free&help_type=medical")
        self.assertEqual(len(first.context["rows"]), dashboard.PANEL_PAGE_SIZE)
        self.assertEqual(first.context["page_obj"].paginator.num_pages, 2)
        self.assertContains(first, 'href="?tab=free&amp;help_type=medical&page=2#requests"')
        self.assertContains(first, 'data-i18n="panel.shown_range" data-i18n-args=\'{"from": 1, "to": 20, "total": 21}\'')
        second = self._get("?tab=free&help_type=medical&page=2")
        self.assertEqual(len(second.context["rows"]), 1)
        self.assertContains(second, 'href="?tab=free&amp;help_type=medical&page=1#requests"')
        self.assertContains(second, '{"from": 21, "to": 21, "total": 21}')

    def test_a_filter_form_submit_keeps_the_tab(self):
        response = self._get("?tab=busy&help_type=medical")
        self.assertContains(response, '<input type="hidden" name="tab" value="busy">')

    def test_query_count_is_constant_and_below_the_old_panel(self):
        """The old panel made 29 queries on the demo data (25 now, 23 on the
        busy tab): tab counters are one aggregate, candidates one batched
        matching query — nothing runs per row."""
        for i in range(3):
            _locate(_make(f"pp_vol{i}", is_volunteer=True), 38.56 + i / 100, 68.78)
        HelpRequest.objects.update(latitude=Decimal("38.56"), longitude=Decimal("68.77"))
        self.client.force_login(self.admin)
        self.client.get(reverse("admin_panel"))  # warm session / caches
        HelpRequest.objects.filter(pk__in=HelpRequest.objects.filter(help_type="medical").values("pk")[:19]).delete()
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("admin_panel"))
        client_user = Users.objects.get(username="pp_cli")
        for _ in range(30):
            _task(client_user, latitude=Decimal("38.57"), longitude=Decimal("68.79"))
        with CaptureQueriesContext(connection) as big:
            response = self.client.get(reverse("admin_panel"))
        self.assertEqual(len(response.context["rows"]), dashboard.PANEL_PAGE_SIZE)
        self.assertTrue(all(row["task"].assignment_suggestion for row in response.context["rows"]))
        self.assertEqual(len(small.captured_queries), len(big.captured_queries))
        self.assertLess(len(big.captured_queries), 29)


class BestVolunteerPerTaskTests(TestCase):
    """The batched candidate lookup gives exactly recommend_volunteers(task, limit=1)."""

    @classmethod
    def setUpTestData(cls):
        client_user = _make("bv_cli", is_client=True)
        spots = [(38.55, 68.77), (38.60, 68.80), (40.28, 69.62), (38.53, 68.70), (37.90, 69.00)]
        for i, (lat, lng) in enumerate(spots):
            volunteer = _make(f"bv_vol{i}", is_volunteer=True, region="sogd" if lat > 40 else "dushanbe")
            _locate(volunteer, lat, lng)
        Profile.objects.filter(user__username="bv_vol1").update(skills=["medical"], availability_status="busy")
        cls.tasks = [
            _task(client_user, help_type="medical", latitude=Decimal("38.56"), longitude=Decimal("68.78")),
            _task(client_user, help_type="grocery", latitude=Decimal("40.27"), longitude=Decimal("69.60"), region="sogd"),
            _task(client_user, priority="emergency", is_urgent=True, latitude=Decimal("37.95"), longitude=Decimal("69.01")),
            _task(client_user),  # no location → None
        ]

    def _expected(self):
        out = {}
        for task in self.tasks:
            picks = matching.recommend_volunteers(task, limit=1)
            out[task.pk] = (picks[0]["volunteer"].pk, picks[0]["score"]) if picks else None
        return out

    def _batched(self):
        return {pk: (top["volunteer"].pk, top["score"]) if top else None
                for pk, top in matching.best_volunteer_per_task(self.tasks).items()}

    def test_same_answer_as_one_call_per_task(self):
        self.assertEqual(self._batched(), self._expected())
        self.assertIsNone(self._batched()[self.tasks[3].pk])

    def test_same_answer_when_the_nearest_cap_bites(self):
        with mock.patch.object(matching, "CANDIDATE_CAP", 2):
            self.assertEqual(self._batched(), self._expected())

    def test_one_query_for_the_whole_list_and_a_bounded_fallback(self):
        with CaptureQueriesContext(connection) as ctx:
            matching.best_volunteer_per_task(self.tasks)
        self.assertEqual(len(ctx.captured_queries), 1)
        with mock.patch.object(matching, "BULK_POOL_CAP", 1):
            self.assertEqual(self._batched(), self._expected())


class NoOpportunityGridLeftTests(TestCase):
    def test_opportunity_card_is_still_used_by_the_task_list_only(self):
        from pathlib import Path

        from django.conf import settings

        users = [p for p in Path(settings.BASE_DIR, "templates").rglob("*.html")
                 if "_opportunity_card.html" in p.read_text(encoding="utf-8")]
        self.assertEqual([p.name for p in users], ["task_list.html"])
        self.assertFalse(re.search(r"opportunity", Path(settings.BASE_DIR, "templates/myapp/admin_panel.html").read_text()))
