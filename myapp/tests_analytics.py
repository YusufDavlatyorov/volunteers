"""CRM analytics (Feature Pack v2, milestone 4, feature 10): access, filters,
numbers, constant query count (no N+1), cache, CSV and the heatmap."""

import json
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import (
    OVERDUE_THRESHOLD,
    STALE_PENDING_THRESHOLD,
    Donation,
    EmergencyReport,
    HelpRequest,
    TaskReview,
)
from myapp.services import analytics_v2 as an

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "An-Tests-2026!"
TZ = ZoneInfo("Asia/Dushanbe")
TODAY = date(2026, 9, 30)


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD,
        region=flags.pop("region", "dushanbe"), **flags,
    )


def _at(day, hour=12):
    return datetime.combine(day, time(hour), tzinfo=TZ)


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class AnalyticsBase(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = _make("an_admin", is_superuser=True, is_staff=True)
        self.curator = _make("an_curator", is_curator=True)
        self.vol = _make("an_vol", is_volunteer=True)
        self.vol2 = _make("an_vol2", is_volunteer=True, region="sogd")
        self.client_user = _make("an_client", is_client=True)
        self.f = an.Filters(TODAY - timedelta(days=6), TODAY, "", 7)

    def task(self, day, *, status="pending", volunteer=None, region="dushanbe", help_type="grocery",
             priority="normal", accept_after=None, complete_after=None, lat=None, lng=None):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type=help_type, description="d", address="Rudaki 10",
            phone="+992901234567", region=region, priority=priority, latitude=lat, longitude=lng,
        )
        created = _at(day)
        fields = {"created_at": created, "status": status, "volunteer": volunteer}
        if accept_after is not None:
            fields["accepted_at"] = created + accept_after
        if complete_after is not None:
            fields["completed_at"] = fields["accepted_at"] + complete_after
        if status == "cancelled":
            fields["cancelled_at"] = created + timedelta(hours=1)
        HelpRequest.objects.filter(pk=task.pk).update(**fields)
        task.refresh_from_db()
        return task

    def seed(self, n=1):
        """n of each: completed / cancelled / pending, spread over the week."""
        for i in range(n):
            day = TODAY - timedelta(days=i % 7)
            self.task(day, status="completed", volunteer=self.vol if i % 2 else self.vol2,
                      accept_after=timedelta(hours=2), complete_after=timedelta(hours=1),
                      help_type=["grocery", "medical", "transport"][i % 3], lat=38.5612 + i * 1e-5, lng=68.7801)
            self.task(day, status="cancelled", region="sogd")
            self.task(day)


class FilterTests(AnalyticsBase):
    def test_presets_and_default(self):
        self.assertEqual(an.parse_filters({}, today=TODAY).days, 30)
        for days in (7, 30, 90):
            f = an.parse_filters({"days": str(days)}, today=TODAY)
            self.assertEqual((f.days, f.preset, f.end_date), (days, days, TODAY))
        self.assertEqual(an.parse_filters({"days": "13"}, today=TODAY).days, 30)
        self.assertEqual(an.parse_filters({"days": "x"}, today=TODAY).days, 30)

    def test_custom_range_is_validated_and_trimmed(self):
        f = an.parse_filters({"from": "2026-09-01", "to": "2026-09-10"}, today=TODAY)
        self.assertEqual((f.start_date, f.end_date, f.preset), (date(2026, 9, 1), date(2026, 9, 10), None))
        # future end clipped to today, inverted / garbage → default
        self.assertEqual(an.parse_filters({"from": "2026-09-20", "to": "2027-01-01"}, today=TODAY).end_date, TODAY)
        self.assertEqual(an.parse_filters({"from": "2026-09-10", "to": "2026-09-01"}, today=TODAY).days, 30)
        self.assertEqual(an.parse_filters({"from": "nope", "to": "2026-09-01"}, today=TODAY).days, 30)
        long = an.parse_filters({"from": "2024-01-01", "to": "2026-09-30"}, today=TODAY)
        self.assertEqual(long.days, an.MAX_RANGE_DAYS)
        self.assertTrue(long.weekly)

    def test_region_whitelist(self):
        self.assertEqual(an.parse_filters({"region": "sogd"}, today=TODAY).region, "sogd")
        self.assertEqual(an.parse_filters({"region": "'; DROP"}, today=TODAY).region, "")

    def test_local_day_boundaries(self):
        f = an.Filters(TODAY, TODAY, "", None)
        self.assertEqual(f.start, datetime(2026, 9, 30, 0, 0, tzinfo=TZ))
        self.assertEqual(f.end, datetime(2026, 10, 1, 0, 0, tzinfo=TZ))

    def test_weekly_buckets_start_on_monday(self):
        f = an.Filters(date(2026, 1, 1), TODAY, "", None)
        buckets = an.bucket_dates(f)
        self.assertTrue(all(d.weekday() == 0 for d in buckets))
        self.assertLessEqual(buckets[0], f.start_date)


class NumberTests(AnalyticsBase):
    def test_requests_timeline_counts_each_event_on_its_own_day(self):
        self.task(TODAY - timedelta(days=1), status="completed", volunteer=self.vol,
                  accept_after=timedelta(hours=1), complete_after=timedelta(hours=20))  # completes TODAY
        self.task(TODAY, status="cancelled")
        self.task(TODAY - timedelta(days=30))  # outside the window
        data = an.requests_timeline(self.f)
        series = {s["key"]: s["values"] for s in data["series"]}
        self.assertEqual(len(data["labels"]), 7)
        self.assertEqual(data["labels"][-1], TODAY.isoformat())
        self.assertEqual(series["created"][-2:], [1, 1])
        self.assertEqual(series["completed"][-1], 1)
        self.assertEqual(series["cancelled"][-1], 1)
        self.assertEqual(sum(series["created"]), 2)

    def test_a_late_evening_local_request_lands_on_its_local_day(self):
        task = self.task(TODAY)
        # 23:30 Dushanbe = 18:30 UTC the same day; 00:30 Dushanbe = previous day 19:30 UTC.
        HelpRequest.objects.filter(pk=task.pk).update(created_at=datetime(2026, 9, 30, 0, 30, tzinfo=TZ))
        series = an.requests_timeline(self.f)["series"][0]["values"]
        self.assertEqual(series[-1], 1)

    def test_categories_and_funnel(self):
        self.seed(3)
        help_types = an.help_types(self.f)
        counts = dict(zip(help_types["labels"], help_types["series"][0]["values"]))
        self.assertEqual(counts["grocery"], 7)  # 1 completed + 3 cancelled + 3 pending
        self.assertEqual(help_types["label_keys"][0], "an.help.medical")
        funnel = an.funnel(self.f)
        self.assertEqual(funnel["series"][0]["values"], [9, 3, 3])
        k = funnel["kpis"]
        self.assertEqual((k["created"], k["completed"], k["cancelled"], k["completion_rate"]), (9, 3, 3, 33))
        self.assertEqual((k["hours_to_accept"], k["hours_to_complete"]), (2.0, 1.0))

    def test_inconsistent_timestamps_never_skew_the_means(self):
        task = self.task(TODAY, status="completed", volunteer=self.vol, accept_after=timedelta(hours=2),
                         complete_after=timedelta(hours=1))
        HelpRequest.objects.filter(pk=task.pk).update(accepted_at=task.created_at - timedelta(hours=5))
        k = an.funnel(self.f)["kpis"]
        self.assertIsNone(k["hours_to_accept"])

    def test_region_filter(self):
        self.seed(2)
        sogd = an.Filters(self.f.start_date, TODAY, "sogd", 7)
        self.assertEqual(an.funnel(sogd)["kpis"]["created"], 2)  # only the cancelled ones
        regions = an.regions(sogd)
        self.assertEqual(regions["labels"], ["sogd"])

    def test_delays_use_the_one_thresholds(self):
        late = self.task(TODAY, status="completed", volunteer=self.vol, accept_after=STALE_PENDING_THRESHOLD + timedelta(hours=1),
                         complete_after=OVERDUE_THRESHOLD + timedelta(minutes=5))
        quick = self.task(TODAY, status="completed", volunteer=self.vol, accept_after=timedelta(hours=1),
                          complete_after=timedelta(hours=1))
        HelpRequest.objects.filter(pk=late.pk).update(created_at=late.created_at - STALE_PENDING_THRESHOLD - timedelta(hours=1),
                                                      accepted_at=late.created_at, completed_at=late.created_at + OVERDUE_THRESHOLD + timedelta(minutes=5))
        self.assertTrue(quick)
        data = an.delays(an.Filters(TODAY - timedelta(days=6), TODAY, "", 7))
        series = {s["key"]: sum(s["values"]) for s in data["series"]}
        self.assertEqual(series["overdue"], 1)
        self.assertEqual(series["stale"], 1)

    def test_volunteer_activity_and_workload(self):
        for _ in range(3):
            self.task(TODAY, status="completed", volunteer=self.vol, accept_after=timedelta(hours=1),
                      complete_after=timedelta(hours=1))
        self.task(TODAY, status="completed", volunteer=self.vol2, accept_after=timedelta(hours=1),
                  complete_after=timedelta(hours=1))
        self.assertEqual(an.volunteer_activity(self.f)["series"][0]["values"][-1], 2)
        workload = an.workload(self.f)
        self.assertEqual(dict(zip(workload["labels"], workload["series"][0]["values"]))["2–3"], 1)
        self.assertEqual(dict(zip(workload["labels"], workload["series"][0]["values"]))["1"], 1)

    def test_top_volunteers_carry_quality_and_no_contacts(self):
        for _ in range(2):
            self.task(TODAY, status="completed", volunteer=self.vol, accept_after=timedelta(hours=1),
                      complete_after=timedelta(hours=1))
        Profile.objects.filter(user=self.vol).update(full_name="Malika", review_avg=4.5, review_count=2)
        rows = an.top_volunteers(self.f)["rows"]
        self.assertEqual(rows[0]["name"], "Malika")
        self.assertEqual((rows[0]["completed"], rows[0]["review_avg"]), (2, 4.5))
        self.assertEqual(set(rows[0]), {"id", "name", "region", "completed", "review_avg", "review_count"})

    def test_ratings_exclude_hidden_reviews(self):
        t1 = self.task(TODAY, status="completed", volunteer=self.vol, accept_after=timedelta(hours=1),
                       complete_after=timedelta(hours=1))
        t2 = self.task(TODAY, status="completed", volunteer=self.vol, accept_after=timedelta(hours=1),
                       complete_after=timedelta(hours=1))
        TaskReview.objects.create(task=t1, client=self.client_user, volunteer=self.vol, stars=5)
        hidden = TaskReview.objects.create(task=t2, client=self.client_user, volunteer=self.vol, stars=1)
        TaskReview.objects.filter(pk=hidden.pk).update(hidden_at=timezone.now())
        TaskReview.objects.update(created_at=_at(TODAY))
        self.assertEqual(an.ratings(self.f)["series"][0]["values"], [0, 0, 0, 0, 1])
        self.assertEqual(an.ratings_timeline(self.f)["series"][0]["values"][-1], 5.0)

    def test_emergencies_response_time(self):
        task = self.task(TODAY, status="active", volunteer=self.vol, accept_after=timedelta(hours=1))
        report = EmergencyReport.objects.create(help_request=task, volunteer=self.vol)
        EmergencyReport.objects.filter(pk=report.pk).update(created_at=_at(TODAY), acknowledged_at=_at(TODAY) + timedelta(minutes=12),
                                                            status="acknowledged")
        data = an.emergencies(self.f)
        self.assertEqual(data["series"][0]["values"][-1], 1)
        self.assertEqual(data["kpis"], {"total": 1, "minutes_to_ack": 12})

    def test_donations_and_registrations(self):
        Donation.objects.create(donor=self.client_user, item_name="Мука", category="food", quantity=1)
        Donation.objects.update(created_at=_at(TODAY))
        status = an.donations_status(self.f)
        self.assertEqual(dict(zip(status["labels"], status["series"][0]["values"]))["pending"], 1)
        Users.objects.update(date_joined=_at(TODAY))
        regs = {s["key"]: s["values"][-1] for s in an.registrations(self.f)["series"]}
        self.assertEqual(regs, {"volunteers": 2, "clients": 1, "curators": 1, "other": 1})

    def test_availability_is_live(self):
        Profile.objects.filter(user=self.vol).update(availability_status="busy")
        data = an.availability(self.f)
        self.assertEqual(dict(zip(data["labels"], data["series"][0]["values"])), {"available": 1, "busy": 1, "offline": 0})


class QueryCountTests(AnalyticsBase):
    def _count(self):
        cache.clear()
        with CaptureQueriesContext(connection) as ctx:
            an.all_charts(self.f)
            an.heat_points(self.f)
        return len(ctx.captured_queries)

    def test_constant_whatever_the_volume(self):
        self.seed(1)
        small = self._count()
        self.seed(25)
        for i in range(10):
            _make(f"an_more_vol{i}", is_volunteer=True)
        big = self._count()
        self.assertEqual(small, big)
        self.assertLessEqual(big, 26)  # 16 + 2 flag charts (+4 queries) + heat; constant is what matters

    def test_second_load_is_served_from_cache(self):
        self.seed(2)
        self._count()
        with CaptureQueriesContext(connection) as ctx:
            an.all_charts(self.f)
            an.heat_points(self.f)
        self.assertEqual(len(ctx.captured_queries), 0)


class HeatTests(AnalyticsBase):
    def test_points_are_rounded_grouped_capped_and_anonymous(self):
        self.seed(5)
        self.task(TODAY, lat=999, lng=68.7)  # a corrupt pin never reaches the map
        points = an.heat_points(self.f)
        self.assertEqual(points, [[38.561, 68.78, 5]])
        for point in points:
            self.assertEqual(len(point), 3)

    def test_cap(self):
        original = an.HEAT_POINT_CAP
        an.HEAT_POINT_CAP = 2
        try:
            for i in range(4):
                self.task(TODAY, lat=38.5 + i * 0.01, lng=68.7)
            self.assertEqual(len(an.heat_points(self.f)), 2)
        finally:
            an.HEAT_POINT_CAP = original


class CsvTests(AnalyticsBase):
    def test_csv_layout_and_language(self):
        self.seed(2)
        text = an.to_csv("help_types", an.help_types(self.f), "tj")
        self.assertTrue(text.startswith("﻿"))
        lines = text.lstrip("﻿").splitlines()
        self.assertEqual(lines[0], "Категория,Сохта шуд")
        self.assertIn("Ёрии тиббӣ,1", lines)
        timeline = an.to_csv("requests_timeline", an.requests_timeline(self.f), "ru").lstrip("﻿").splitlines()
        self.assertEqual(timeline[0], "Дата,Создано,Выполнено,Отменено")
        self.assertEqual(len(timeline), 8)

    def test_formula_injection_is_neutralised(self):
        Profile.objects.filter(user=self.vol).update(full_name='=HYPERLINK("http://evil")')
        self.task(TODAY, status="completed", volunteer=self.vol, accept_after=timedelta(hours=1),
                  complete_after=timedelta(hours=1))
        text = an.to_csv("top_volunteers", an.top_volunteers(self.f), "ru")
        self.assertIn("'=HYPERLINK", text)
        self.assertNotIn('\n=HYPERLINK', text)


class ViewTests(AnalyticsBase):
    def test_access_matrix(self):
        url = reverse("crm_analytics")
        for user in (self.admin, self.curator):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                self.assertEqual(self.client.get(url).status_code, 200)
                self.assertEqual(self.client.get(reverse("crm_analytics_heat")).status_code, 200)
                self.assertEqual(self.client.get(reverse("crm_analytics_csv", args=["funnel"])).status_code, 200)
        for user in (self.vol, self.client_user):
            self.client.force_login(user)
            with self.subTest(user=user.username):
                for target in (url, reverse("crm_analytics_heat"), reverse("crm_analytics_csv", args=["funnel"])):
                    self.assertRedirects(self.client.get(target), reverse("profile"), fetch_redirect_response=False)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_page_embeds_data_and_self_hosted_libs(self):
        self.seed(2)
        self.client.force_login(self.curator)
        response = self.client.get(reverse("crm_analytics") + "?days=7&region=dushanbe")
        self.assertContains(response, 'id="analyticsData"')
        self.assertContains(response, "vendor/chartjs-4.4.7/chart.umd.min.js")
        self.assertContains(response, "vendor/leaflet-heat-0.2.0/leaflet-heat.js")
        self.assertNotContains(response, "cdn.jsdelivr.net")
        raw = re.search(r'<script id="analyticsData"[^>]*>(.*?)</script>', response.content.decode(), re.S).group(1)
        payload = json.loads(raw)
        self.assertEqual(set(payload["charts"]), set(an.CHARTS))
        self.assertEqual(payload["qs"], "days=7&region=dushanbe")
        for chart in ("help_types", "funnel", "requests_timeline"):
            self.assertContains(response, f'data-chart="{chart}"')
        body = response.content.decode()
        self.assertNotIn("+992901234567", body)  # the client phone (the footer has the NGO number)
        self.assertNotIn("Rudaki", body)

    def test_page_query_count_is_bounded(self):
        self.seed(1)
        self.client.force_login(self.curator)
        cache.clear()
        with CaptureQueriesContext(connection) as small:
            self.client.get(reverse("crm_analytics"))
        self.seed(20)
        cache.clear()
        with CaptureQueriesContext(connection) as big:
            self.client.get(reverse("crm_analytics"))
        self.assertEqual(len(small.captured_queries), len(big.captured_queries))

    def test_csv_download(self):
        self.seed(1)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("crm_analytics_csv", args=["requests_timeline"]) + "?days=7&lang=en")
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("Date,Created,Completed,Cancelled", response.content.decode("utf-8-sig"))
        self.assertEqual(self.client.get(reverse("crm_analytics_csv", args=["nope"])).status_code, 404)

    def test_heat_json(self):
        self.seed(1)
        self.client.force_login(self.admin)
        data = self.client.get(reverse("crm_analytics_heat") + "?days=7").json()
        self.assertEqual(set(data), {"points", "cap"})

    def test_curator_dashboard_has_trend_card_and_link(self):
        self.seed(1)
        self.client.force_login(self.curator)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'class="card reveal trend-card"')
        self.assertContains(response, reverse("crm_analytics"))
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("admin_panel")), reverse("crm_analytics"))

    def test_robots_blocks_the_crm(self):
        robots = self.client.get("/robots.txt").content.decode()
        self.assertIn("Disallow: /myapp/crm/", robots)

    def test_i18n_keys_exist_in_every_language(self):
        from server.i18n_prerender import translation_table

        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key, text in an.ui_i18n_keys(lang).items():
                self.assertEqual(table.get(key), text, f"{lang} {key}")
            for key in ("region.sogd", "availability.busy", "don.status_pending", "don.category_food"):
                self.assertTrue(table.get(key), f"{lang} {key}")
