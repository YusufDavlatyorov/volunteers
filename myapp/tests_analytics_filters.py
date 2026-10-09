"""CRM analytics filter panel (stage 3): ?period=7d|30d|3m|1y|custom&from=&to=&region=.

One parser (``analytics_v2.parse_filters``) feeds every number, chart, CSV and
the heatmap; invalid input falls back to the defaults without breaking the
page; days are Asia/Dushanbe days and "to" includes its whole last day."""

import json
import re
from datetime import date, datetime, timedelta
from unittest import mock

from django.http import QueryDict
from django.urls import reverse

from myapp.models import EmergencyReport, HelpRequest
from myapp.services import analytics_v2 as an
from myapp.tests_analytics import TODAY, TZ, AnalyticsBase


def _payload(response):
    raw = re.search(r'<script id="analyticsData"[^>]*>(.*?)</script>', response.content.decode(), re.S).group(1)
    return json.loads(raw)


class ParseFiltersTests(AnalyticsBase):
    def parse(self, **params):
        return an.parse_filters(params, today=TODAY)

    def test_every_period_and_the_default(self):
        for token, days in (("7d", 7), ("30d", 30), ("3m", 90), ("1y", 365)):
            with self.subTest(token=token):
                f = self.parse(period=token)
                self.assertEqual((f.days, f.end_date, f.period, f.error), (days, TODAY, token, ""))
        for empty in ({}, {"period": ""}, {"period": "", "from": "", "to": "", "region": ""}):
            with self.subTest(params=empty):
                f = an.parse_filters(empty, today=TODAY)
                self.assertEqual((f.period, f.days, f.region, f.error), ("30d", 30, "", ""))

    def test_old_days_links_still_work(self):
        self.assertEqual(self.parse(days="90").period, "3m")
        self.assertEqual(self.parse(days="7").period, "7d")

    def test_a_preset_ignores_the_dates(self):
        f = self.parse(period="7d", **{"from": "2026-01-01", "to": "2026-01-05"})
        self.assertEqual((f.start_date, f.end_date, f.error), (TODAY - timedelta(days=6), TODAY, ""))

    def test_custom_range(self):
        f = self.parse(period="custom", **{"from": "2026-09-01", "to": "2026-09-10"})
        self.assertEqual((f.start_date, f.end_date, f.period, f.preset, f.error),
                         (date(2026, 9, 1), date(2026, 9, 10), "custom", None, ""))
        one_day = self.parse(period="custom", **{"from": "2026-09-30", "to": "2026-09-30"})
        self.assertEqual((one_day.days, one_day.error), (1, ""))

    def test_garbage_falls_back_to_the_default(self):
        cases = [
            {"period": "abc"}, {"period": "custom"}, {"period": "custom", "from": "2026-09-01"},
            {"period": "custom", "from": "nope", "to": "2026-09-10"},
            {"period": "custom", "from": "2026-02-30", "to": "2026-09-10"},
            {"period": "custom", "from": "<script>", "to": "1e9"}, {"days": "13"}, {"days": "x"},
            {"period": "30d\x00"},
        ]
        for params in cases:
            with self.subTest(params=params):
                f = self.parse(**params)
                self.assertEqual((f.period, f.start_date, f.end_date, f.error),
                                 ("30d", TODAY - timedelta(days=29), TODAY, "invalid"))
        self.assertEqual(self.parse(region="'; DROP TABLE").region, "")
        self.assertEqual(self.parse(region="'; DROP TABLE").error, "")

    def test_from_after_to_is_rejected(self):
        f = self.parse(period="custom", **{"from": "2026-09-10", "to": "2026-09-01"})
        self.assertEqual((f.period, f.days, f.error), ("30d", 30, "invalid"))

    def test_future_dates_are_rejected(self):
        tomorrow = (TODAY + timedelta(days=1)).isoformat()
        for params in ({"from": "2026-09-20", "to": tomorrow}, {"from": tomorrow, "to": tomorrow}):
            with self.subTest(params=params):
                f = self.parse(period="custom", **params)
                self.assertEqual((f.period, f.end_date, f.error), ("30d", TODAY, "invalid"))
        self.assertEqual(self.parse(period="custom", **{"from": "2026-09-20", "to": TODAY.isoformat()}).error, "")

    def test_today_is_the_dushanbe_day(self):
        # 20:30 UTC on 30 Sep is already 1 Oct in Dushanbe (UTC+5).
        now = datetime.fromisoformat("2026-09-30T20:30:00+00:00")
        self.assertEqual(an.local_today(now), date(2026, 10, 1))

    def test_region(self):
        self.assertEqual(self.parse(period="7d", region="sogd").region, "sogd")
        self.assertEqual(self.parse(region="Sogd").region, "")

    def test_querystring_round_trip(self):
        for params in ({"period": "3m", "region": "khatlon"}, {"period": "1y"},
                       {"period": "custom", "from": "2026-08-01", "to": "2026-08-31", "region": "gbao"}):
            with self.subTest(params=params):
                f = self.parse(**params)
                self.assertEqual(an.parse_filters(QueryDict(f.querystring()), today=TODAY), f)
        self.assertEqual(self.parse(period="30d").querystring(), "period=30d")
        self.assertEqual(self.parse(period="custom", **{"from": "2026-08-01", "to": "2026-08-31"}).querystring(),
                         "period=custom&from=2026-08-01&to=2026-08-31")

    def test_previous_period_has_the_same_length_right_before(self):
        f = self.parse(period="30d", region="sogd")
        prev = f.previous()
        self.assertEqual((prev.days, prev.end_date, prev.region), (30, f.start_date - timedelta(days=1), "sogd"))
        custom = self.parse(period="custom", **{"from": "2026-09-01", "to": "2026-09-10"})
        self.assertEqual((custom.previous().start_date, custom.previous().end_date),
                         (date(2026, 8, 22), date(2026, 8, 31)))


class FilterAppliesEverywhereTests(AnalyticsBase):
    def make(self, when, region="dushanbe"):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="d", address="a",
            phone="+992901234567", region=region,
        )
        HelpRequest.objects.filter(pk=task.pk).update(created_at=when)
        return task

    def test_the_last_day_is_included_up_to_23_59_59_dushanbe(self):
        day = date(2026, 9, 29)
        f = an.parse_filters({"period": "custom", "from": day.isoformat(), "to": day.isoformat()}, today=TODAY)
        self.make(datetime(2026, 9, 29, 0, 0, 0, tzinfo=TZ))                 # first second — in
        self.make(datetime(2026, 9, 29, 23, 59, 59, tzinfo=TZ))              # = 18:59:59 UTC — in
        self.make(datetime(2026, 9, 30, 0, 0, 0, tzinfo=TZ))                 # = 29 Sep 19:00 UTC — out
        self.make(datetime(2026, 9, 28, 23, 59, 59, tzinfo=TZ))             # the day before — out
        self.assertEqual(an.funnel(f)["kpis"]["created"], 2)
        self.assertEqual(an.requests_timeline(f)["series"][0]["values"], [2])

    def test_region_filter_reaches_every_number(self):
        recent = datetime.combine(TODAY, datetime.min.time(), tzinfo=TZ) + timedelta(hours=10)
        mine = self.make(recent, region="sogd")
        self.make(recent, region="dushanbe")
        sos = EmergencyReport.objects.create(help_request=mine, volunteer=self.vol2)
        EmergencyReport.objects.filter(pk=sos.pk).update(created_at=recent)
        both = an.parse_filters({"period": "7d"}, today=TODAY)
        sogd = an.parse_filters({"period": "7d", "region": "sogd"}, today=TODAY)
        self.assertEqual(an.funnel(both)["kpis"]["created"], 2)
        self.assertEqual(an.funnel(sogd)["kpis"]["created"], 1)
        self.assertEqual(sum(an.help_types(sogd)["series"][0]["values"]), 1)
        self.assertEqual(an.regions(sogd)["labels"], ["sogd"])
        self.assertEqual(an.emergencies(sogd)["kpis"]["total"], 1)
        self.assertEqual(an.emergencies(an.parse_filters({"period": "7d", "region": "gbao"}, today=TODAY))["kpis"]["total"], 0)

    def test_page_csv_and_heatmap_read_the_same_filters(self):
        freeze_today(self)
        today = an.local_today()
        self.client.force_login(self.curator)
        start = today - timedelta(days=4)
        query = f"?period=custom&from={start}&to={today}&region=sogd"
        response = self.client.get(reverse("crm_analytics") + query)
        payload = _payload(response)
        expected = an.parse_filters({"period": "custom", "from": str(start), "to": str(today), "region": "sogd"})
        self.assertEqual(payload["qs"], expected.querystring())
        self.assertEqual(payload["charts"]["requests_timeline"]["labels"][0], start.isoformat())
        self.assertEqual(payload["charts"]["regions"]["labels"], ["sogd"])
        # Every CSV link and the heatmap URL carry exactly these filters.
        body = response.content.decode()
        self.assertEqual(body.count("?" + expected.querystring().replace("&", "&amp;")), len(an.CHARTS) + 1)
        csv = self.client.get(reverse("crm_analytics_csv", args=["funnel"]) + query)
        self.assertIn(f"{start}_{today}_sogd", csv["Content-Disposition"])


def freeze_today(test):
    """The test and the view must agree on "today": without this a run that
    crosses midnight in Dushanbe (19:00 UTC) compares two different days."""
    patcher = mock.patch("myapp.services.analytics_v2.local_today", return_value=an.local_today())
    patcher.start()
    test.addCleanup(patcher.stop)


class FilterPanelPageTests(AnalyticsBase):
    def setUp(self):
        super().setUp()
        freeze_today(self)

    def get(self, query=""):
        self.client.force_login(self.curator)
        return self.client.get(reverse("crm_analytics") + query)

    def test_panel_markup_defaults(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        today = an.local_today().isoformat()
        self.assertIn('id="anFilters"', body)
        for token in ("7d", "30d", "3m", "1y", "custom"):
            self.assertIn(f'value="{token}"', body)
        self.assertRegex(body, r'<option value="30d" selected')
        # A preset shows its dates in disabled fields; nothing in the future can be picked.
        self.assertRegex(body, r'id="anFrom"[^>]*value="%s"[^>]*max="%s"[^>]*disabled' % (
            (an.local_today() - timedelta(days=29)).isoformat(), today))
        self.assertRegex(body, r'id="anTo"[^>]*value="%s"[^>]*disabled' % today)
        self.assertIn('data-i18n="an.apply"', body)
        self.assertIn(f'href="{reverse("crm_analytics")}" class="button ghost an-ctl an-filter__btn" id="anReset"', body)
        self.assertRegex(body, r'id="anFilterError"[^>]*hidden')
        self.assertIn("js/analytics-filters.js", body)
        # Server render is Russian, period line in the calendar's date format.
        self.assertIn("Все регионы", body)
        self.assertIn("Сбросить", body)

    def test_custom_range_keeps_the_dates_editable(self):
        start = an.local_today() - timedelta(days=3)
        body = self.get(f"?period=custom&from={start}&to={an.local_today()}&region=khatlon").content.decode()
        self.assertRegex(body, r'<option value="custom" selected')
        self.assertRegex(body, r'<option value="khatlon" selected')
        self.assertNotRegex(body, r'id="anFrom"[^>]*disabled')

    def test_invalid_url_shows_defaults_and_a_note(self):
        for query in ("?period=zzz", "?period=custom&from=2026-09-10&to=2026-09-01",
                      "?period=custom&from=2020-01-01&to=2999-01-01", "?period=custom&from=%00&to=",
                      "?region=<script>&period=custom"):
            with self.subTest(query=query):
                response = self.get(query)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(_payload(response)["qs"], "period=30d")
                self.assertContains(response, 'data-i18n="an.err.url"')
                self.assertNotContains(response, "<script>&")

    def test_i18n_keys_in_every_language(self):
        from server.i18n_prerender import translation_table

        keys = ["an.p.7d", "an.p.30d", "an.p.3m", "an.p.1y", "an.p.custom", "an.from", "an.to",
                "an.from_aria", "an.to_aria", "an.region", "an.all_regions", "an.apply", "an.reset",
                "an.err.required", "an.err.future", "an.err.order", "an.err.too_long", "an.err.url",
                "act.date"] + [f"act.mg{m}" for m in range(1, 13)]
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in keys:
                self.assertTrue(table.get(key), f"{lang} {key}")
