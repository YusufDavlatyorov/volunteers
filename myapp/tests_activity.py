"""Activity calendar (myapp/services/activity.py, templates/myapp/_activity_calendar.html):
counted actions per role, the Asia/Dushanbe day boundary, the 365-day / 53-week
window, quartile levels, Russian plurals, caching, roles that get no calendar,
and a query count that does not grow with the number of active days."""

import datetime
import re
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Users
from myapp.models import Donation, HelpRequest, PetReport, PhotoReport, TaskReview
from myapp.services import activity

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 10, 9, 7, 0, tzinfo=UTC)          # 12:00 in Dushanbe, a Friday
TODAY = datetime.date(2026, 10, 9)


def _user(name, **flags):
    return Users.objects.create_user(username=name, email=f"{name}@example.com", password="x-Act-12345",
                                     region="dushanbe", **flags)


def _at(y, m, d, hh=10, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=UTC)


class ActivityHelpersTests(TestCase):
    def test_quartile_levels_from_the_users_own_max(self):
        self.assertEqual([activity.level(n, 8) for n in (0, 1, 2, 3, 4, 5, 6, 7, 8)], [0, 1, 1, 2, 2, 3, 3, 4, 4])
        self.assertEqual(activity.level(1, 1), 4)          # a new user's only day is still visible
        self.assertEqual(activity.level(0, 0), 0)

    def test_russian_plural_categories(self):
        cases = {1: "one", 21: "one", 2: "few", 4: "few", 22: "few", 5: "many", 11: "many", 12: "many", 14: "many", 0: "many", 111: "many"}
        for n, cat in cases.items():
            self.assertEqual(activity.plural_category_ru(n), cat, n)

    def test_roles(self):
        self.assertTrue(activity.shows_calendar(_user("a_vol", is_volunteer=True)))
        self.assertTrue(activity.shows_calendar(_user("a_cli", is_client=True)))
        self.assertFalse(activity.shows_calendar(_user("a_cur", is_curator=True)))
        self.assertFalse(activity.shows_calendar(_user("a_adm", is_superuser=True, is_staff=True)))
        self.assertIsNone(activity.calendar(Users.objects.get(username="a_cur")))


class ActivityCountsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.vol = _user("ac_vol", is_volunteer=True)
        self.cli = _user("ac_cli", is_client=True)

    def tearDown(self):
        cache.clear()

    def _task(self, **kw):
        return HelpRequest.objects.create(client=self.cli, help_type="grocery", description="x", address="a",
                                          phone="+992900000000", region="dushanbe", **kw)

    def test_volunteer_kinds(self):
        done = self._task(volunteer=self.vol, status="completed")
        HelpRequest.objects.filter(pk=done.pk).update(accepted_at=_at(2026, 10, 5, 6), completed_at=_at(2026, 10, 5, 9))
        active = self._task(volunteer=self.vol, status="active")
        HelpRequest.objects.filter(pk=active.pk).update(accepted_at=_at(2026, 10, 6), completed_at=_at(2026, 10, 6))  # not completed
        report = PhotoReport.objects.create(author=self.vol, title="r", image="reports/x.jpg")
        # Counted on the day the help took place (event_date), not the upload day.
        PhotoReport.objects.filter(pk=report.pk).update(created_at=_at(2026, 10, 7, 11), event_date=datetime.date(2026, 10, 5))
        don = Donation.objects.create(donor=self.cli, item_name="rice", quantity=1, assigned_volunteer=self.vol)
        Donation.objects.filter(pk=don.pk).update(received_at=_at(2026, 10, 7), distributed_at=_at(2026, 10, 7, 12))
        counts = activity.daily_counts(self.vol, TODAY)
        self.assertEqual(counts[datetime.date(2026, 10, 5)], {"accepted": 1, "completed": 1, "photo_report": 1})
        self.assertEqual(counts[datetime.date(2026, 10, 6)], {"accepted": 1})
        # picked up and delivered: two separate actions
        self.assertEqual(counts[datetime.date(2026, 10, 7)], {"donation_received": 1, "donation_distributed": 1})

    def test_client_kinds_include_cancelled_requests_and_hidden_reviews(self):
        cancelled = self._task(status="cancelled")
        HelpRequest.objects.filter(pk=cancelled.pk).update(created_at=_at(2026, 10, 1))
        done = self._task(volunteer=self.vol, status="completed")
        HelpRequest.objects.filter(pk=done.pk).update(created_at=_at(2026, 10, 1, 12))
        review = TaskReview.objects.create(task=done, client=self.cli, volunteer=self.vol, stars=5, hidden_at=timezone.now())
        TaskReview.objects.filter(pk=review.pk).update(created_at=_at(2026, 10, 2))
        offer = Donation.objects.create(donor=self.cli, item_name="rice", quantity=1)
        Donation.objects.filter(pk=offer.pk).update(created_at=_at(2026, 10, 3))
        pet = PetReport.objects.create(reporter=self.cli, report_type="lost", description="cat")
        PetReport.objects.filter(pk=pet.pk).update(created_at=_at(2026, 10, 3, 13))
        counts = activity.daily_counts(self.cli, TODAY)
        self.assertEqual(counts[datetime.date(2026, 10, 1)], {"request_created": 2})
        self.assertEqual(counts[datetime.date(2026, 10, 2)], {"review": 1})
        self.assertEqual(counts[datetime.date(2026, 10, 3)], {"donation_offered": 1, "pet_report": 1})
        # The volunteer did none of those.
        self.assertNotIn(datetime.date(2026, 10, 1), activity.daily_counts(self.vol, TODAY))

    def test_day_boundary_is_dushanbe_midnight(self):
        # Dushanbe is UTC+5: 18:59 UTC is 23:59 on the 4th, 19:01 UTC is 00:01 on the 5th.
        before, after = self._task(), self._task()
        HelpRequest.objects.filter(pk=before.pk).update(created_at=_at(2026, 10, 4, 18, 59))
        HelpRequest.objects.filter(pk=after.pk).update(created_at=_at(2026, 10, 4, 19, 1))
        counts = activity.daily_counts(self.cli, TODAY)
        self.assertEqual(counts[datetime.date(2026, 10, 4)], {"request_created": 1})
        self.assertEqual(counts[datetime.date(2026, 10, 5)], {"request_created": 1})

    def test_window_is_365_days_including_today(self):
        first = TODAY - datetime.timedelta(days=364)
        inside, outside, today = self._task(), self._task(), self._task()
        HelpRequest.objects.filter(pk=inside.pk).update(created_at=datetime.datetime.combine(first, datetime.time(0, 5), tzinfo=activity.TZ))
        HelpRequest.objects.filter(pk=outside.pk).update(created_at=datetime.datetime.combine(first, datetime.time(0, 0), tzinfo=activity.TZ) - datetime.timedelta(minutes=1))
        HelpRequest.objects.filter(pk=today.pk).update(created_at=datetime.datetime.combine(TODAY, datetime.time(23, 59), tzinfo=activity.TZ))
        counts = activity.daily_counts(self.cli, TODAY)
        self.assertEqual(set(counts), {first, TODAY})

    def test_calendar_shape_levels_labels_and_cache(self):
        for day, n in ((5, 1), (6, 4)):
            for _ in range(n):
                t = self._task()
                HelpRequest.objects.filter(pk=t.pk).update(created_at=_at(2026, 10, day))
        cal = activity.calendar(self.cli, now=NOW)
        self.assertEqual(len(cal["weeks"]), 53)
        self.assertTrue(all(len(w) == 7 for w in cal["weeks"]))
        self.assertEqual(cal["weeks"][0][0]["date"].weekday(), 0)                   # grid starts on a Monday
        days = [d for w in cal["weeks"] for d in w if not d["out"]]
        self.assertEqual(len(days), 365)
        self.assertEqual(days[-1]["date"], TODAY)
        self.assertTrue(days[-1]["today"])
        by_date = {d["date"]: d for d in days}
        self.assertEqual((by_date[datetime.date(2026, 10, 5)]["level"], by_date[datetime.date(2026, 10, 6)]["level"]), (1, 4))
        self.assertEqual(cal["total"], 5)
        self.assertEqual(cal["total_text"], "5 действий за последний год")
        self.assertEqual(by_date[datetime.date(2026, 10, 6)]["label"], "4 действия · 6 октября 2026. Создан запрос: 4")
        self.assertEqual(by_date[datetime.date(2026, 10, 5)]["label"], "1 действие · 5 октября 2026")
        self.assertEqual(by_date[datetime.date(2026, 10, 7)]["label"], "Нет активности · 7 октября 2026")
        self.assertEqual(by_date[datetime.date(2026, 10, 6)]["kinds"], "request_created:4")
        self.assertTrue(cal["months"])
        self.assertTrue(all(b["col"] - a["col"] >= 3 for a, b in zip(cal["months"], cal["months"][1:])))
        # cached: no queries the second time
        with CaptureQueriesContext(connection) as ctx:
            activity.calendar(self.cli, now=NOW)
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_query_count_does_not_grow_with_active_days(self):
        def queries_for(user):
            cache.clear()
            with CaptureQueriesContext(connection) as ctx:
                activity.calendar(user, now=NOW)
            return len(ctx.captured_queries)

        few, many = _user("ac_few", is_client=True), _user("ac_many", is_client=True)
        for owner, n_days in ((few, 10), (many, 300)):
            for i in range(n_days):
                t = HelpRequest.objects.create(client=owner, help_type="grocery", description="x", address="a",
                                               phone="+992900000000", region="dushanbe")
                HelpRequest.objects.filter(pk=t.pk).update(created_at=NOW - datetime.timedelta(days=i))
        self.assertEqual(queries_for(few), queries_for(many))
        self.assertEqual(queries_for(many), 4)                          # one per kind for a client
        self.assertEqual(activity.calendar(many, now=NOW)["total"], 300)


class ActivityPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.vol = _user("ap_vol", is_volunteer=True)
        self.cli = _user("ap_cli", is_client=True)
        self.cur = _user("ap_cur", is_curator=True)

    def tearDown(self):
        cache.clear()

    def _profile(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("profile")).content.decode()

    def test_own_profile_for_volunteer_and_client(self):
        for user, hint in ((self.vol, "act.empty_volunteer"), (self.cli, "act.empty_client")):
            html = self._profile(user)
            with self.subTest(user=user.username):
                self.assertIn('id="activityCard"', html)
                self.assertIn(f'data-i18n="{hint}"', html)
                self.assertIn("0 действий за последний год", html)       # server-rendered Russian
                cells = re.findall(r'<span class="act-cell l\d[^"]*" role="img" aria-label="([^"]+)"', html)
                self.assertEqual(len(cells), 365)
                self.assertIn("is-today", html)
                self.assertIn("js/activity.js", html)
                self.assertIn("css/activity.css", html)

    def test_no_calendar_for_staff(self):
        html = self._profile(self.cur)
        self.assertNotIn('id="activityCard"', html)
        self.assertNotIn("js/activity.js", html)

    def test_crm_volunteer_card_shows_it_to_staff_only(self):
        url = reverse("crm_volunteer_detail", args=[self.vol.pk])
        self.client.force_login(self.cur)
        html = self.client.get(url).content.decode()
        self.assertIn('id="activityCard"', html)
        self.assertIn('data-i18n="act.empty_other"', html)
        self.client.force_login(self.cli)
        self.assertNotEqual(self.client.get(url).status_code, 200)

    def test_profile_query_count_does_not_grow_with_active_days(self):
        def profile_queries():
            cache.clear()
            self.client.force_login(self.cli)
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("profile"))
            return len(ctx.captured_queries)

        baseline = None
        for n_days in (0, 40):
            for i in range(n_days):
                t = HelpRequest.objects.create(client=self.cli, help_type="grocery", description="x", address="a",
                                               phone="+992900000000", region="dushanbe")
                HelpRequest.objects.filter(pk=t.pk).update(created_at=timezone.now() - datetime.timedelta(days=i * 7))
            count = profile_queries()
            baseline = baseline if baseline is not None else count
        # The client dashboard lists a bounded number of recent requests, so
        # only the calendar part matters: allow nothing beyond that bound.
        self.assertLessEqual(count - baseline, 2)


class ActivityI18nTests(TestCase):
    def test_every_key_in_every_language(self):
        from server.i18n_prerender import translation_table

        keys = {"act.title", "act.none", "act.date", "act.less", "act.more", "act.grid_label",
                "act.wd_mon", "act.wd_wed", "act.wd_fri",
                "act.empty_volunteer", "act.empty_client", "act.empty_other"}
        keys |= {f"act.{base}.{cat}" for base in ("total", "count") for cat in ("one", "few", "many", "other")}
        keys |= {f"act.m{i}" for i in range(1, 13)} | {f"act.mg{i}" for i in range(1, 13)}
        keys |= {f"act.k.{k}" for k in activity.KINDS}
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            with self.subTest(lang=lang):
                self.assertEqual(keys - set(table), set())
        js = Path(finders.find("js/activity.js")).read_text(encoding="utf-8")
        self.assertIn("Intl.PluralRules", js)
        self.assertTrue((Path(settings.BASE_DIR) / "static" / "css" / "activity.css").is_file())
