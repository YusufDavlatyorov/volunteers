"""backfill_activity (myapp/services/activity_backfill.py): UserDailyActivity
restored from real timestamps only — days without events get no row, a second
run changes nothing, demo rows and demo accounts are untouched, a live
(middleware) row is never lowered, photo reports are only read. Plus the
reward rule on real data: no approved photo report → no points, no badges."""

import datetime
import io

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import (
    HelpRequest,
    Notification,
    PhotoReport,
    TaskReview,
    UserAchievement,
    UserDailyActivity,
)
from myapp.services import activity, activity_backfill, task_actions

TZ = activity.TZ


def _make(username, **flags):
    return Users.objects.create_user(username=username, email=f"{username}@example.com", password="x-Bf-12345",
                                     region="dushanbe", **flags)


def _at(day, hour=12):
    return datetime.datetime.combine(day, datetime.time(hour), tzinfo=TZ)


def _rows(user):
    return dict(UserDailyActivity.objects.filter(user=user).values_list("date", "actions_count"))


def _run(*args):
    out = io.StringIO()
    call_command("backfill_activity", *args, stdout=out)
    return out.getvalue()


class BackfillTests(TestCase):
    def setUp(self):
        cache.clear()
        self.today = activity.local_today()
        self.d1 = self.today - datetime.timedelta(days=3)
        self.d2 = self.today - datetime.timedelta(days=1)
        self.joined = _at(self.d1 - datetime.timedelta(days=10), 9)
        self.cli = _make("bf_cli", is_client=True)
        self.vol = _make("bf_vol", is_volunteer=True)
        Users.objects.filter(pk__in=[self.cli.pk, self.vol.pk]).update(date_joined=self.joined)
        # d1: the client creates a request, the volunteer accepts and completes it.
        task = HelpRequest.objects.create(client=self.cli, help_type="grocery", description="d", address="a",
                                          phone="+992901234567", region="dushanbe")
        HelpRequest.objects.filter(pk=task.pk).update(
            created_at=_at(self.d1, 9), status="completed", volunteer=self.vol,
            accepted_at=_at(self.d1, 10), completed_at=_at(self.d1, 13),
        )
        # d2: the client reviews it.
        review = TaskReview.objects.create(task=task, client=self.cli, volunteer=self.vol, stars=5)
        TaskReview.objects.filter(pk=review.pk).update(created_at=_at(self.d2, 11))

    def test_only_days_with_real_events_get_a_row(self):
        _run()
        joined_day = self.joined.astimezone(TZ).date()
        self.assertEqual(_rows(self.cli), {joined_day: 1, self.d1: 1, self.d2: 1})
        self.assertEqual(_rows(self.vol), {joined_day: 1, self.d1: 2})
        self.assertFalse(UserDailyActivity.objects.exclude(source="backfill").exists())
        self.assertFalse(UserDailyActivity.objects.filter(is_demo=True).exists())

    def test_second_run_changes_nothing(self):
        _run()
        before = list(UserDailyActivity.objects.order_by("pk").values())
        out = _run()
        self.assertIn("done: created 0, updated 0, deleted 0", out)
        self.assertEqual(list(UserDailyActivity.objects.order_by("pk").values()), before)

    def test_dry_run_writes_nothing(self):
        out = _run("--dry-run")
        self.assertIn("would create 5", out)
        self.assertIn("bf_cli: 3 active day(s)", out)
        self.assertFalse(UserDailyActivity.objects.exists())

    def test_demo_accounts_and_rows_are_untouched(self):
        demo = _make("bf_demo", is_client=True)
        Users.objects.filter(pk=demo.pk).update(is_demo=True, date_joined=self.joined)
        HelpRequest.objects.create(client=demo, help_type="grocery", description="d", address="a",
                                   phone="+992901234567", region="dushanbe", is_demo=True)
        demo_row = UserDailyActivity.objects.create(user=demo, date=self.d2, actions_count=7, is_demo=True,
                                                    source="demo")
        _run()
        self.assertEqual(list(UserDailyActivity.objects.filter(user=demo).values_list("pk", "actions_count")),
                         [(demo_row.pk, 7)])

    def test_a_deactivated_account_is_skipped_entirely(self):
        _run()
        kept = dict(_rows(self.vol))
        Users.objects.filter(pk=self.vol.pk).update(is_active=False)
        TaskReview.objects.all().delete()          # would make a backfill row stale for an active account
        out = _run()
        self.assertEqual(_rows(self.vol), kept)    # neither recounted nor deleted
        self.assertNotIn("bf_vol:", out)
        other = _make("bf_off", is_client=True)
        Users.objects.filter(pk=other.pk).update(is_active=False, date_joined=self.joined)
        _run()
        self.assertEqual(_rows(other), {})         # never created

    def test_a_live_row_is_raised_never_lowered(self):
        low = UserDailyActivity.objects.create(user=self.vol, date=self.d1, actions_count=1)       # events: 2
        high = UserDailyActivity.objects.create(user=self.cli, date=self.d1, actions_count=9)      # events: 1
        _run()
        low.refresh_from_db()
        high.refresh_from_db()
        self.assertEqual((low.actions_count, low.source), (2, "merged"))
        self.assertEqual((high.actions_count, high.source), (9, "live"))
        self.assertIn("done: created 0, updated 0, deleted 0", _run())

    def test_a_restored_day_whose_events_are_gone_is_removed(self):
        _run()
        TaskReview.objects.all().delete()
        _run()
        self.assertNotIn(self.d2, _rows(self.cli))

    def test_nothing_before_1_september_or_registration(self):
        early = activity.START_DATE - datetime.timedelta(days=2)
        task = HelpRequest.objects.create(client=self.cli, help_type="grocery", description="e", address="a",
                                          phone="+992901234567", region="dushanbe")
        HelpRequest.objects.filter(pk=task.pk).update(created_at=_at(early))
        _run()
        self.assertTrue(all(day >= activity.START_DATE for day in _rows(self.cli)))

    def test_a_location_saved_with_the_account_is_not_a_second_event(self):
        Profile.objects.filter(user=self.cli).update(location_updated_at=self.joined + datetime.timedelta(minutes=1))
        Profile.objects.filter(user=self.vol).update(location_updated_at=_at(self.d2, 18))
        _run()
        self.assertEqual(_rows(self.cli)[self.joined.astimezone(TZ).date()], 1)
        self.assertEqual(_rows(self.vol)[self.d2], 1)

    def test_an_imported_account_without_real_events_gets_no_row(self):
        from myapp.models import VolunteerApplication

        imported = Users.objects.create_user(username="bf_imp", email="volunteer-07@khayrkhoh.invalid",
                                             password="x-Bf-12345", region="dushanbe", is_volunteer=True)
        Users.objects.filter(pk=imported.pk).update(date_joined=_at(self.d1, 14))
        Profile.objects.filter(user=imported).update(location_updated_at=_at(self.d1, 14))
        app = VolunteerApplication.objects.create(user=imported, reason="импорт")
        VolunteerApplication.objects.filter(pk=app.pk).update(
            created_at=_at(self.d2, 11), status="approved", reviewed_at=_at(self.d2, 11), reviewed_by=self.cli)
        _run()
        self.assertEqual(_rows(imported), {})
        self.assertEqual(_rows(self.cli)[self.d2], 2)   # the staff side of it is real: the review + the approval
        # Once the person does something real, that day (only) counts.
        Users.objects.filter(pk=imported.pk).update(last_login=_at(self.d2, 20))
        _run()
        self.assertEqual(_rows(imported), {self.d2: 1})

    def test_an_application_the_person_sent_counts(self):
        from myapp.models import VolunteerApplication

        app = VolunteerApplication.objects.create(user=self.vol, reason="хочу помогать")
        VolunteerApplication.objects.filter(pk=app.pk).update(created_at=_at(self.d2, 9), reviewed_at=_at(self.d2, 15),
                                                              status="approved", reviewed_by=self.cli)
        _run()
        self.assertEqual(_rows(self.vol)[self.d2], 1)

    def test_mark_all_read_is_one_event(self):
        stamp = _at(self.d2, 15)
        for i in range(5):
            Notification.objects.create(recipient=self.vol, type="broadcast", title_key="x", body_key="y",
                                        read_at=stamp)
        _run()
        self.assertEqual(_rows(self.vol)[self.d2], 1)

    def test_photo_reports_are_only_read(self):
        report = PhotoReport.objects.create(author=self.vol, uploaded_by=self.vol, title="t", description="d",
                                            image="reports/x.jpg", region="dushanbe")
        PhotoReport.objects.filter(pk=report.pk).update(created_at=_at(self.d2, 16))
        before = PhotoReport.objects.values().get(pk=report.pk)
        _run()
        self.assertEqual(PhotoReport.objects.values().get(pk=report.pk), before)
        self.assertEqual(_rows(self.vol)[self.d2], 1)   # the upload counts once (uploaded_by, not author too)

    def test_heatmap_shows_restored_days(self):
        _run()
        self.cli.refresh_from_db()
        cal = activity.calendar(self.cli)
        self.assertEqual(cal["active_days"], 3)


@override_settings(TELEGRAM_BOT_TOKEN="", CELERY_BROKER_URL=None)
class RealRewardRuleTests(TestCase):
    """Real data: the reward needs an approved photo report — nothing else pays."""

    def test_real_request_completed_without_a_report_pays_nothing(self):
        cache.clear()
        vol = _make("rr_vol", is_volunteer=True)
        cli = _make("rr_cli", is_client=True)
        task = HelpRequest.objects.create(client=cli, help_type="grocery", description="d", address="a",
                                          phone="+992901234567", region="dushanbe")
        self.assertFalse(task.is_demo)
        self.assertTrue(task_actions.accept(vol, task).ok)
        outcome = task_actions.complete(vol, task)
        self.assertTrue(outcome.ok and outcome.reward_pending)
        call_command("recalculate_achievements", stdout=io.StringIO())
        task.refresh_from_db()
        self.assertTrue(task.reward_pending)
        self.assertEqual(Profile.objects.get(user=vol).rating, 0)
        self.assertFalse(UserAchievement.objects.filter(user=vol).exists())

    def test_recalculate_dry_run_saves_nothing(self):
        vol = _make("rr_vol2", is_volunteer=True)
        cli = _make("rr_cli2", is_client=True)
        task = HelpRequest.objects.create(client=cli, help_type="grocery", description="d", address="a",
                                          phone="+992901234567", region="dushanbe")
        HelpRequest.objects.filter(pk=task.pk).update(status="completed", volunteer=vol, completed_at=timezone.now())
        out = io.StringIO()
        call_command("recalculate_achievements", "--dry-run", stdout=out)
        self.assertIn("rr_vol2: first_help", out.getvalue())
        self.assertFalse(UserAchievement.objects.exists())
