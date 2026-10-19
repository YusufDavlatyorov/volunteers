"""Demo stand (2026-10-18): seed_demo, the per-day activity counter, the new
achievements, the rating's merit score and its tie-breaks.

* seed_demo: guards (DEMO_MODE, production host, production database),
  idempotent (a second run changes no counts), real accounts untouched, no
  impossible request states (completed without a volunteer, dates out of
  order or in the future, accept before registration, two active requests at
  once), statistics = completed requests, activity without gaps or duplicates,
  photo reports and their files untouched, nothing delivered, ``--reset``.
* UserDailyActivity + DailyActivityMiddleware + the heatmap summary.
* achievements exactly at their thresholds, none without a completed request.
* rating: stable order, the photo only as a tie-break, pages without gaps.
"""

import datetime
import io
import os
from pathlib import Path

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.management.commands import seed_demo
from myapp.models import (
    Achievement,
    ChatMessage,
    HelpRequest,
    Notification,
    PhotoReport,
    TaskReview,
    UserAchievement,
    UserDailyActivity,
)
from myapp.services import achievements, activity, rating
from myapp.testing import TempMediaRoot

_MEDIA = TempMediaRoot("gc-demo-seed-")
FAST_HASHER = ["django.contrib.auth.hashers.MD5PasswordHasher"]
TZ = activity.TZ
SEED_SETTINGS = dict(DEMO_MODE=True, PASSWORD_HASHERS=FAST_HASHER, CELERY_BROKER_URL=None, TELEGRAM_BOT_TOKEN="",
                     ALLOWED_HOSTS=["localhost", "127.0.0.1", "testserver"])


def _make(username, **flags):
    return Users.objects.create_user(username=username, email=f"{username}@example.com", password="Real-Pass-2026!",
                                     region="dushanbe", **flags)


def _seed(*args):
    out = io.StringIO()
    call_command("seed_demo", *args, stdout=out)
    return out.getvalue()


def _counts():
    return {
        "users": Users.objects.count(),
        "requests": HelpRequest.objects.count(),
        "statuses": sorted(HelpRequest.objects.values_list("status", flat=True)),
        "reviews": TaskReview.objects.count(),
        "activity": UserDailyActivity.objects.count(),
        "activity_sum": sum(UserDailyActivity.objects.values_list("actions_count", flat=True)),
        "badges": UserAchievement.objects.count(),
        "chat": ChatMessage.objects.count(),
        "photo_reports": PhotoReport.objects.count(),
    }


def _media_files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if p.is_file())


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #

@override_settings(**SEED_SETTINGS)
class SeedGuardTests(TestCase):
    def assertRefuses(self, message):
        with self.assertRaisesMessage(CommandError, message):
            _seed()
        self.assertFalse(Users.objects.exists())

    @override_settings(DEMO_MODE=False)
    def test_refuses_without_demo_mode(self):
        self.assertRefuses("DEMO_MODE")

    def test_refuses_on_a_production_host(self):
        for hosts in (["khayrkhoh.tj"], ["localhost", "www.khayrkhoh.tj"], [".khayrkhoh.tj"], ["KhayrKhoh.TJ"]):
            with self.subTest(hosts=hosts), override_settings(ALLOWED_HOSTS=hosts):
                self.assertRefuses("production domain")

    def test_refuses_on_the_production_database(self):
        name = str(connection.settings_dict["NAME"])
        from django.conf import settings

        databases = {**settings.DATABASES, "default": {**settings.DATABASES["default"], "NAME": name}}
        with override_settings(PRODUCTION_DB_NAMES=frozenset({name}), DATABASES=databases):
            self.assertRefuses("production database")

    def test_the_guard_runs_before_any_database_access(self):
        with override_settings(DEMO_MODE=False), CaptureQueriesContext(connection) as ctx:
            with self.assertRaises(CommandError):
                _seed()
        self.assertEqual(len(ctx.captured_queries), 0)


# --------------------------------------------------------------------------- #
# one seeded database, many checks
# --------------------------------------------------------------------------- #

@override_settings(MEDIA_ROOT=_MEDIA.path, **SEED_SETTINGS)
class SeededDatabaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cache.clear()
        # Real accounts and data that exist before the seed.
        cls.real_vol = _make("real_volunteer", is_volunteer=True)
        cls.real_cli = _make("real_client", is_client=True)
        cls.real_task = HelpRequest.objects.create(client=cls.real_cli, help_type="grocery", description="Купить хлеб",
                                                   address="ул. Рудаки 1", phone="+992901234567", region="dushanbe")
        # A real account that happens to own a demo username.
        cls.squatter = Users.objects.create_user(username="aziz_k", email="aziz@example.com", password="Real-Pass-2026!",
                                                 region="sogd", is_volunteer=True)
        cls.real_hashes = {u.pk: (u.password, u.email, u.username) for u in Users.objects.all()}
        # A photo report with its file, made before the seed.
        Path(_MEDIA.path, "reports").mkdir(parents=True, exist_ok=True)
        Path(_MEDIA.path, "reports", "real.jpg").write_bytes(b"\xff\xd8not-really-a-jpeg")
        cls.report = PhotoReport.objects.create(author=cls.real_vol, title="Настоящий отчёт", description="d",
                                                image="reports/real.jpg", region="dushanbe")
        cls.report_before = PhotoReport.objects.values().get(pk=cls.report.pk)
        cls.media_before = _media_files(_MEDIA.path)

        cls.first_out = _seed()
        cls.after_first = _counts()
        cls.second_out = _seed()
        cls.after_second = _counts()
        cls.now = timezone.now()

    def setUp(self):
        cache.clear()

    # ------------------------------------------------------------- basics

    def test_second_run_changes_no_counts(self):
        self.assertEqual(self.after_first, self.after_second)
        self.assertIn("Demo data ready.", self.second_out)

    def test_a_meaningful_amount_of_data(self):
        demo = Users.objects.filter(is_demo=True)
        self.assertGreaterEqual(demo.filter(is_volunteer=True).count(), 40)
        self.assertGreaterEqual(demo.filter(is_client=True).count(), 20)
        statuses = HelpRequest.objects.filter(is_demo=True).values_list("status", flat=True)
        self.assertGreater(list(statuses).count("completed"), 100)
        self.assertGreater(TaskReview.objects.filter(is_demo=True).count(), 50)

    def test_every_generated_row_is_flagged_demo(self):
        demo_users = set(Users.objects.filter(is_demo=True).values_list("pk", flat=True))
        for task in HelpRequest.objects.exclude(pk=self.real_task.pk):
            self.assertTrue(task.is_demo, task.pk)
            self.assertIn(task.client_id, demo_users)
        self.assertFalse(TaskReview.objects.filter(is_demo=False).exists())
        self.assertFalse(UserDailyActivity.objects.filter(is_demo=False).exists())
        self.assertEqual(set(UserDailyActivity.objects.values_list("user_id", flat=True)), demo_users)

    def test_real_accounts_are_untouched(self):
        real = [self.real_vol, self.real_cli, self.squatter]
        for user in real:
            user.refresh_from_db()
            self.assertFalse(user.is_demo)
            self.assertEqual((user.password, user.email, user.username), self.real_hashes[user.pk])
            self.assertFalse(HelpRequest.objects.filter(is_demo=True, volunteer=user).exists())
            self.assertFalse(HelpRequest.objects.filter(is_demo=True, client=user).exists())
            self.assertFalse(UserDailyActivity.objects.filter(user=user).exists())
            self.assertFalse(TaskReview.objects.filter(volunteer=user).exists())
            self.assertFalse(TaskReview.objects.filter(client=user).exists())
            self.assertFalse(UserAchievement.objects.filter(user=user).exists())
        self.real_task.refresh_from_db()
        self.assertEqual((self.real_task.status, self.real_task.volunteer_id), ("pending", None))
        self.assertEqual(Users.objects.filter(username="aziz_k").count(), 1)   # never duplicated
        self.assertIn("aziz_k", self.first_out)                                  # reported as skipped

    def test_no_avatar_is_generated(self):
        self.assertFalse(Profile.objects.filter(user__is_demo=True).exclude(image="").exists())

    def test_nothing_was_delivered(self):
        self.assertFalse(Notification.objects.exists())

    # ------------------------------------------------------- photo reports

    def test_photo_reports_and_their_files_are_untouched(self):
        self.assertEqual(self.after_first["photo_reports"], 1)
        self.assertEqual(PhotoReport.objects.values().get(pk=self.report.pk), self.report_before)
        self.assertEqual(_media_files(_MEDIA.path), self.media_before)
        self.assertFalse(PhotoReport.objects.filter(help_request__is_demo=True).exists())

    def test_seed_credits_demo_requests_only(self):
        source = Path(seed_demo.__file__).read_text(encoding="utf-8")
        self.assertEqual(source.count("photo_reports.credit_task(task)"), 1)
        self.assertIn("if task.is_demo:\n                        photo_reports.credit_task(task)", source)

    def test_seed_source_never_writes_photo_reports(self):
        source = Path(seed_demo.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PhotoReport.objects", source)
        self.assertNotIn("shutil", source)

    # ------------------------------------------------------------ requests

    def test_no_impossible_request_states(self):
        joined = dict(Users.objects.values_list("pk", "date_joined"))
        for task in HelpRequest.objects.filter(is_demo=True):
            with self.subTest(task=task.pk, status=task.status):
                self.assertLessEqual(task.created_at, self.now)
                self.assertGreaterEqual(task.created_at, joined[task.client_id])
                self.assertGreaterEqual(task.created_at, seed_demo.TIMELINE_START)
                if task.status == "pending":
                    self.assertIsNone(task.volunteer_id)
                    self.assertIsNone(task.accepted_at)
                    continue
                self.assertIn(task.status, ("active", "completed"))
                self.assertIsNotNone(task.volunteer_id)              # never completed without a volunteer
                self.assertLessEqual(task.created_at, task.accepted_at)
                self.assertGreaterEqual(task.accepted_at, joined[task.volunteer_id])
                self.assertLessEqual(task.accepted_at, self.now)
                if task.status == "completed":
                    self.assertLessEqual(task.accepted_at, task.completed_at)
                    self.assertLessEqual(task.completed_at, self.now)
                    self.assertFalse(task.reward_pending)
                else:
                    self.assertIsNone(task.completed_at)

    def test_one_active_request_per_volunteer_over_the_whole_timeline(self):
        spans = {}
        for vid, start, end in HelpRequest.objects.filter(is_demo=True, volunteer__isnull=False).values_list(
                "volunteer_id", "accepted_at", "completed_at"):
            spans.setdefault(vid, []).append((start, end or self.now))
        for vid, items in spans.items():
            items.sort()
            for (s1, e1), (s2, _e2) in zip(items, items[1:]):
                self.assertLessEqual(e1, s2, f"volunteer #{vid} held two active requests")
        for vid in spans:
            self.assertLessEqual(HelpRequest.objects.filter(volunteer_id=vid, status="active").count(), 1)

    def test_mostly_completed_some_active_some_pending(self):
        statuses = list(HelpRequest.objects.filter(is_demo=True).values_list("status", flat=True))
        self.assertGreater(statuses.count("completed"), len(statuses) * 0.8)
        self.assertGreaterEqual(statuses.count("active"), 1)
        self.assertGreaterEqual(statuses.count("pending"), 1)

    def test_only_existing_help_types_and_both_languages(self):
        from myapp.models import HELP_TYPE_CHOICES

        types = set(HelpRequest.objects.filter(is_demo=True).values_list("help_type", flat=True))
        self.assertLessEqual(types, {key for key, _ in HELP_TYPE_CHOICES})
        texts = set(HelpRequest.objects.filter(is_demo=True).values_list("description", flat=True))
        self.assertLessEqual(texts, seed_demo.DEMO_REQUEST_TEXTS)
        self.assertTrue(any("ҳ" in t or "ӯ" in t for t in texts))      # Tajik
        self.assertTrue(any("ы" in t for t in texts))                   # Russian

    def test_reviews_follow_completion_and_are_mostly_good(self):
        stars = []
        for review in TaskReview.objects.filter(is_demo=True).select_related("task"):
            self.assertEqual(review.task.status, "completed")
            self.assertEqual(review.volunteer_id, review.task.volunteer_id)
            self.assertGreaterEqual(review.created_at, review.task.completed_at)
            self.assertLessEqual(review.created_at, self.now)
            stars.append(review.stars)
        self.assertGreater(sum(1 for s in stars if s >= 4) / len(stars), 0.75)
        self.assertTrue(any(s <= 3 for s in stars))

    # ---------------------------------------------------- stats & badges

    def test_volunteer_statistics_match_completed_requests(self):
        rows = {row["user_id"]: row for row in rating.build()["rows"]}
        for volunteer in Users.objects.filter(is_demo=True, is_volunteer=True):
            done = HelpRequest.objects.filter(volunteer=volunteer, status="completed").count()
            with self.subTest(volunteer=volunteer.username):
                self.assertEqual(rows[volunteer.pk]["completed"], done)
                self.assertEqual(rows[volunteer.pk]["reviews"],
                                 TaskReview.objects.filter(volunteer=volunteer, hidden_at__isnull=True).count())

    def test_badges_held_are_the_badges_met(self):
        """Every badge met is held; a held one is met — except an average-based
        badge whose average fell later (badges are never revoked)."""
        active = list(Achievement.objects.filter(is_active=True).exclude(code__in=achievements.MANUAL_CODES))
        for volunteer in Users.objects.filter(is_demo=True, is_volunteer=True):
            numbers = achievements.counters(volunteer)
            held = set(UserAchievement.objects.filter(user=volunteer).values_list("achievement__code", flat=True))
            met = {a.code for a in active if achievements.is_met(a, numbers)}
            with self.subTest(volunteer=volunteer.username):
                self.assertLessEqual(met, held)
                self.assertLessEqual(held - met, achievements.AVERAGE_CODES)
                if numbers["tasks"] == 0:
                    self.assertEqual(held, set())

    def test_the_demo_shows_the_whole_badge_ladder(self):
        codes = set(UserAchievement.objects.values_list("achievement__code", flat=True))
        self.assertLessEqual({"first_help", "tasks_5", "tasks_10", "regular", "long_term", "high_rating"}, codes)

    # ------------------------------------------------------------ activity

    def test_activity_has_no_gaps_and_no_duplicates(self):
        today = activity.local_today(self.now)
        for user in Users.objects.filter(is_demo=True):
            first = max(activity.START_DATE, user.date_joined.astimezone(TZ).date())
            dates = list(UserDailyActivity.objects.filter(user=user).order_by("date").values_list("date", flat=True))
            with self.subTest(user=user.username):
                self.assertEqual(len(dates), len(set(dates)))
                self.assertEqual(dates, [first + datetime.timedelta(days=i) for i in range((today - first).days + 1)])
                self.assertFalse(UserDailyActivity.objects.filter(user=user, actions_count__lt=1).exists())

    def test_request_days_have_at_least_their_actions(self):
        for user in Users.objects.filter(is_demo=True, is_volunteer=True)[:15]:
            counts = activity.daily_counts(user, activity.local_today(self.now))
            stored = dict(UserDailyActivity.objects.filter(user=user).values_list("date", "actions_count"))
            for day, kinds in counts.items():
                derived = sum(n for kind, n in kinds.items() if kind != "site")
                self.assertGreaterEqual(stored.get(day, 0), derived, f"{user.username} {day}")

    def test_profile_heatmap_from_1_september(self):
        volunteer = Users.objects.filter(is_demo=True, is_volunteer=True).order_by("date_joined").first()
        self.client.force_login(volunteer)
        html = self.client.get(reverse("profile")).content.decode()
        self.assertIn('id="actSummary"', html)
        self.assertIn('data-act-plural="act.days"', html)
        self.assertIn('data-act-plural="act.streak"', html)
        self.assertIn('data-d="2026-09-01"', html)
        self.assertNotIn('data-d="2026-08-31"', html)
        cal = activity.calendar(volunteer)
        self.assertEqual(cal["active_days"], (cal["last"] - cal["first"]).days + 1)   # no gaps
        self.assertEqual(cal["streak"], cal["active_days"])

    # -------------------------------------------------------------- rating

    def test_rating_pages_have_every_volunteer_exactly_once(self):
        expected = [row["rank"] for row in rating.build()["rows"]]
        self.assertGreater(len(expected), 43)    # podium + 2 full pages + more → pages 1–3
        seen = []
        page = 1
        while True:
            response = self.client.get(reverse("rating"), {"page": page})
            seen += [row["rank"] for row in response.context["podium"]]
            seen += [row["rank"] for row in response.context["page_rows"]]
            if not response.context["page_obj"].has_next():
                break
            page += 1
        self.assertGreaterEqual(page, 3)
        self.assertEqual(seen, expected)

    def test_rating_is_stable(self):
        first = [(r["user_id"], r["score"]) for r in rating.build()["rows"]]
        second = [(r["user_id"], r["score"]) for r in rating.build()["rows"]]
        self.assertEqual(first, second)

    def test_seeded_rating_order_follows_the_keys(self):
        rows = rating.build()["rows"]
        keys = [(-r["score"], -int(r["has_photo"]), -r["completeness"], r["user_id"]) for r in rows]
        self.assertEqual(keys, sorted(keys))

    # --------------------------------------------------------------- reset

    def test_reset_deletes_only_demo_rows_and_never_a_photo_report(self):
        demo_vol = Users.objects.filter(is_demo=True, is_volunteer=True).first()
        demo_task = HelpRequest.objects.filter(is_demo=True, status="completed").first()
        PhotoReport.objects.filter(pk=self.report.pk).update(author=demo_vol, help_request=demo_task)
        protected = PhotoReport.objects.values().get(pk=self.report.pk)
        real_before = set(Users.objects.filter(is_demo=False).values_list("pk", flat=True))
        out = _seed("--reset")
        self.assertIn("--reset kept", out)
        self.assertEqual(PhotoReport.objects.values().get(pk=self.report.pk), protected)
        self.assertTrue(Users.objects.filter(pk=demo_vol.pk).exists())
        self.assertTrue(HelpRequest.objects.filter(pk=demo_task.pk).exists())
        self.assertEqual(set(Users.objects.filter(is_demo=False).values_list("pk", flat=True)), real_before)
        self.assertTrue(HelpRequest.objects.filter(pk=self.real_task.pk).exists())
        self.assertEqual(_media_files(_MEDIA.path), self.media_before)


# --------------------------------------------------------------------------- #
# the per-day counter
# --------------------------------------------------------------------------- #

@override_settings(USER_ACTIVITY_TRACKING=True)
class DailyActivityMiddlewareTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = _make("da_user", is_client=True)
        self.client.force_login(self.user)

    def row(self):
        return UserDailyActivity.objects.get(user=self.user, date=activity.local_today())

    def test_first_page_of_the_day_counts_once_then_page_views_are_free(self):
        self.client.get(reverse("profile"))
        self.assertEqual(self.row().actions_count, 1)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("profile"))
        self.assertFalse([q for q in ctx.captured_queries if "myapp_userdailyactivity" in q["sql"]])
        self.assertEqual(self.row().actions_count, 1)

    def test_a_successful_post_is_one_more_action(self):
        self.client.get(reverse("profile"))
        self.client.post(reverse("profile_language"), {"language": "tj"})   # ignored: a setting, not an action
        self.assertEqual(self.row().actions_count, 1)
        self.client.post(reverse("logout"))                                  # anonymous afterwards: not counted
        self.client.force_login(self.user)
        self.client.post(reverse("create_request"), {"help_type": "grocery", "priority": "normal",
                                                     "description": "Купить хлеб", "address": "ул. Айни 1",
                                                     "phone": "+992901234567"})
        self.assertEqual(self.row().actions_count, 2)
        self.assertFalse(self.row().is_demo)

    def test_one_row_per_day_and_a_demo_account_writes_demo_rows(self):
        Users.objects.filter(pk=self.user.pk).update(is_demo=True)
        self.user.refresh_from_db()
        for _ in range(3):
            activity.record_request(self.user)
        self.assertEqual(UserDailyActivity.objects.filter(user=self.user).count(), 1)
        self.assertEqual(self.row().actions_count, 3)
        self.assertTrue(self.row().is_demo)

    @override_settings(USER_ACTIVITY_TRACKING=False)
    def test_switched_off(self):
        self.client.get(reverse("profile"))
        self.assertFalse(UserDailyActivity.objects.exists())

    def test_anonymous_and_static_requests_are_not_counted(self):
        self.client.logout()
        self.client.get(reverse("rating"))
        self.assertFalse(UserDailyActivity.objects.exists())


class ActivitySummaryTests(TestCase):
    def test_active_days_and_current_streak(self):
        today = datetime.date(2026, 10, 9)
        first = activity.START_DATE
        days = {today - datetime.timedelta(days=i): {"site": 1} for i in (1, 2, 3, 7)}
        self.assertEqual(activity.summary(days, first, today), {"active_days": 4, "streak": 3})  # today not over yet
        days[today] = {"site": 2}
        self.assertEqual(activity.summary(days, first, today), {"active_days": 5, "streak": 4})
        self.assertEqual(activity.summary({}, first, today), {"active_days": 0, "streak": 0})

    def test_the_counter_is_merged_without_double_counting(self):
        user = _make("merge_cli", is_client=True)
        Users.objects.filter(pk=user.pk).update(date_joined=datetime.datetime(2026, 9, 1, tzinfo=TZ))
        user.refresh_from_db()
        today = datetime.date(2026, 10, 9)
        task = HelpRequest.objects.create(client=user, help_type="grocery", description="x", address="a",
                                          phone="+992900000000", region="dushanbe")
        HelpRequest.objects.filter(pk=task.pk).update(created_at=datetime.datetime(2026, 10, 5, 6, 0, tzinfo=TZ))
        UserDailyActivity.objects.create(user=user, date=datetime.date(2026, 10, 5), actions_count=4)
        UserDailyActivity.objects.create(user=user, date=datetime.date(2026, 10, 6), actions_count=2)
        counts = activity.daily_counts(user, today)
        self.assertEqual(counts[datetime.date(2026, 10, 5)], {"request_created": 1, "site": 3})
        self.assertEqual(counts[datetime.date(2026, 10, 6)], {"site": 2})

    def test_window_is_365_days_once_1_september_is_older(self):
        first, last = activity.window(datetime.date(2028, 1, 10))
        self.assertEqual((last - first).days, 364)

    def test_window_starts_at_registration(self):
        today = datetime.date(2026, 10, 18)
        joined = datetime.datetime(2026, 10, 9, 20, 30, tzinfo=datetime.timezone.utc)   # 10 Oct 01:30 in Dushanbe
        self.assertEqual(activity.window(today, joined), (datetime.date(2026, 10, 10), today))
        self.assertEqual(activity.window(today, datetime.date(2026, 8, 1)), (activity.START_DATE, today))

    def test_no_square_before_registration(self):
        user = _make("win_cli", is_client=True)
        Users.objects.filter(pk=user.pk).update(date_joined=timezone.now() - datetime.timedelta(days=2))
        user.refresh_from_db()
        cal = activity.calendar(user)
        days = [d for w in cal["weeks"] for d in w if not d["out"]]
        self.assertEqual(days[0]["date"], user.date_joined.astimezone(TZ).date())
        self.assertEqual(len(days), 3)


# --------------------------------------------------------------------------- #
# achievements at their thresholds
# --------------------------------------------------------------------------- #

class AchievementThresholdTests(TestCase):
    def setUp(self):
        cache.clear()
        self.vol = _make("th_vol", is_volunteer=True)
        self.cli = _make("th_cli", is_client=True)

    def codes(self):
        return set(UserAchievement.objects.filter(user=self.vol).values_list("achievement__code", flat=True))

    def done(self, when):
        task = HelpRequest.objects.create(client=self.cli, help_type="grocery", description="d", address="a",
                                          phone="+992901234567", region="dushanbe")
        HelpRequest.objects.filter(pk=task.pk).update(status="completed", volunteer=self.vol,
                                                      accepted_at=when - datetime.timedelta(hours=1), completed_at=when)
        task.refresh_from_db()
        return task

    def reviewed(self, n, stars):
        for _ in range(n):
            task = self.done(timezone.now() - datetime.timedelta(days=1))
            TaskReview.objects.create(task=task, client=self.cli, volunteer=self.vol, stars=stars)

    def test_thresholds_live_on_the_rows(self):
        expected = {"tasks_5": 5, "tasks_25": 25, "high_rating": 5, "regular": 4, "long_term": 60}
        self.assertEqual(dict(Achievement.objects.filter(code__in=expected).values_list("code", "threshold")), expected)

    def test_task_milestones_exactly_at_the_threshold(self):
        recent = timezone.now() - datetime.timedelta(days=1)
        for _ in range(4):
            self.done(recent)
        achievements.evaluate(self.vol)
        self.assertNotIn("tasks_5", self.codes())
        self.done(recent)
        achievements.evaluate(self.vol)
        self.assertIn("tasks_5", self.codes())
        self.assertNotIn("tasks_10", self.codes())
        for _ in range(19):
            self.done(recent)
        achievements.evaluate(self.vol)
        self.assertNotIn("tasks_25", self.codes())
        self.done(recent)
        achievements.evaluate(self.vol)
        self.assertIn("tasks_25", self.codes())

    def test_regular_needs_four_weeks_in_a_row(self):
        monday = timezone.now().astimezone(TZ) - datetime.timedelta(days=timezone.now().astimezone(TZ).weekday() + 70)
        for week in (0, 1, 2):
            self.done(monday + datetime.timedelta(weeks=week, hours=12))
        self.done(monday + datetime.timedelta(weeks=4, hours=12))   # a gap: 3 + 1
        self.assertEqual(achievements.counters(self.vol)["weeks_in_a_row"], 3)
        achievements.evaluate(self.vol)
        self.assertNotIn("regular", self.codes())
        self.done(monday + datetime.timedelta(weeks=3, hours=12))   # closes the gap: 5 in a row
        self.assertEqual(achievements.counters(self.vol)["weeks_in_a_row"], 5)
        achievements.evaluate(self.vol)
        self.assertIn("regular", self.codes())

    def test_long_term_needs_the_days_and_a_completed_request(self):
        Users.objects.filter(pk=self.vol.pk).update(date_joined=timezone.now() - datetime.timedelta(days=59, hours=1))
        self.vol.refresh_from_db()
        self.done(timezone.now() - datetime.timedelta(days=1))
        achievements.evaluate(self.vol)
        self.assertNotIn("long_term", self.codes())
        Users.objects.filter(pk=self.vol.pk).update(date_joined=timezone.now() - datetime.timedelta(days=60, hours=1))
        self.vol.refresh_from_db()
        achievements.evaluate(self.vol)
        self.assertIn("long_term", self.codes())

    def test_high_rating_needs_five_reviews_and_4_5(self):
        self.reviewed(4, 5)
        achievements.evaluate(self.vol)
        self.assertNotIn("high_rating", self.codes())
        self.reviewed(1, 4)   # 5 reviews, average 4.8
        achievements.evaluate(self.vol)
        self.assertIn("high_rating", self.codes())

    def test_high_rating_not_below_the_average(self):
        self.reviewed(3, 5)
        self.reviewed(2, 3)   # average 4.2
        achievements.evaluate(self.vol)
        self.assertNotIn("high_rating", self.codes())

    def test_nothing_without_a_completed_request(self):
        Users.objects.filter(pk=self.vol.pk).update(date_joined=timezone.now() - datetime.timedelta(days=400))
        self.vol.refresh_from_db()
        UserDailyActivity.objects.create(user=self.vol, date=activity.local_today(), actions_count=50)
        pending = HelpRequest.objects.create(client=self.cli, help_type="grocery", description="d", address="a",
                                             phone="+992901234567", region="dushanbe")
        HelpRequest.objects.filter(pk=pending.pk).update(status="active", volunteer=self.vol,
                                                         accepted_at=timezone.now())
        self.assertEqual(achievements.evaluate(self.vol), [])
        call_command("recalculate_achievements", stdout=io.StringIO())
        self.assertEqual(self.codes(), set())

    def test_recalculate_command(self):
        self.done(timezone.now() - datetime.timedelta(days=1))
        out = io.StringIO()
        call_command("recalculate_achievements", stdout=out)
        self.assertIn("awarded 1 new badge", out.getvalue())
        self.assertEqual(self.codes(), {"first_help"})
        out = io.StringIO()
        call_command("recalculate_achievements", stdout=out)
        self.assertIn("awarded 0", out.getvalue())

    def test_new_badges_have_art_and_texts(self):
        from django.conf import settings

        from myapp.services import notify_texts

        for code in ("tasks_5", "tasks_25", "high_rating", "regular", "long_term"):
            self.assertTrue((Path(settings.BASE_DIR) / "static" / "images" / "badges" / f"{code}.svg").exists())
            for lang in ("ru", "tj", "en"):
                self.assertTrue(notify_texts.LABELS["achievement"][lang][code])
                self.assertTrue(achievements.DESCRIPTIONS[code][lang])


# --------------------------------------------------------------------------- #
# rating tie-breaks
# --------------------------------------------------------------------------- #

class RatingTieBreakTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.cli = _make("rtb_cli", is_client=True)

    def volunteer(self, name, done=0, photo=False, filled=0):
        user = _make(name, is_volunteer=True)
        fields = {"full_name": name}
        if photo:
            fields["image"] = "avatars/x.jpg"
        if filled >= 1:
            fields["bio"] = "about"
        if filled >= 2:
            fields["age"] = 30
        Profile.objects.filter(user=user).update(**fields)
        for _ in range(done):
            task = HelpRequest.objects.create(client=self.cli, help_type="grocery", description="d", address="a",
                                              phone="+992901234567", region="dushanbe")
            HelpRequest.objects.filter(pk=task.pk).update(status="completed", volunteer=user,
                                                          completed_at=timezone.now())
        return user

    def test_photo_only_breaks_a_tie(self):
        self.volunteer("rtb_a", done=2)
        self.volunteer("rtb_b", done=2, photo=True)
        self.volunteer("rtb_c", done=1, photo=True)
        rows = rating.build()["rows"]
        self.assertEqual([r["name"] for r in rows], ["rtb_b", "rtb_a", "rtb_c"])
        self.assertEqual([r["score"] for r in rows], [20, 20, 10])   # the photo adds no points

    def test_then_completeness_then_id(self):
        self.volunteer("rtb_d")
        self.volunteer("rtb_e", filled=2)
        self.volunteer("rtb_f", filled=1)
        self.volunteer("rtb_g", filled=1)
        rows = rating.build()["rows"]
        self.assertEqual([r["name"] for r in rows], ["rtb_e", "rtb_f", "rtb_g", "rtb_d"])

    def test_a_hand_given_badge_earns_no_points(self):
        a = self.volunteer("rtb_m", done=0)
        self.volunteer("rtb_n", done=0)
        mentor = Achievement.objects.get(code="mentor")
        self.assertTrue(mentor.is_manual)
        UserAchievement.objects.create(user=a, achievement=mentor)
        rows = rating.build()["rows"]
        self.assertEqual([(r["name"], r["score"]) for r in rows], [("rtb_m", 0), ("rtb_n", 0)])
        self.assertEqual(Achievement.objects.filter(is_manual=True).count(), 1)

    def test_badges_add_points(self):
        a = self.volunteer("rtb_h", done=1)
        self.volunteer("rtb_i", done=1)
        UserAchievement.objects.create(user=a, achievement=Achievement.objects.get(code="first_help"))
        rows = rating.build()["rows"]
        self.assertEqual([(r["name"], r["score"]) for r in rows], [("rtb_h", 12), ("rtb_i", 10)])


# --------------------------------------------------------------------------- #
# banner + texts
# --------------------------------------------------------------------------- #

class DemoBannerTests(TestCase):
    # 2026-10-19 (owner): no plate above the navbar any more — one small line
    # in the footer of every page.
    @override_settings(DEMO_MODE=True)
    def test_footer_note_on_every_page_in_demo_mode(self):
        for name in ("home", "rating", "login", "faq"):
            html = self.client.get(reverse(name)).content.decode()
            self.assertIn('class="footer-demo-note"', html)
            self.assertIn("Часть данных на сайте демонстрационная", html)
            self.assertNotIn("demo-banner", html)
            self.assertLess(html.index("<footer"), html.index("footer-demo-note"))

    @override_settings(DEMO_MODE=False)
    def test_no_note_otherwise(self):
        html = self.client.get(reverse("home")).content.decode()
        self.assertNotIn("footer-demo-note", html)
        self.assertNotIn("demo-banner", html)

    def test_new_texts_in_every_language(self):
        from server.i18n_prerender import translation_table

        keys = ["demo.footer_note", "act.k.site", "act.grid_label", "ach.rule_weeks", "ach.rule_long_term"]
        keys += [f"act.{base}.{form}" for base in ("total", "days", "streak") for form in ("one", "few", "many", "other")]
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in keys:
                self.assertTrue(table.get(key), f"{lang} {key}")
