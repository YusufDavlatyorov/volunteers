"""Feature Pack v2, feature 3 — system-computed availability + do-not-disturb
(myapp/services/presence.py, myapp/middleware.py, sweep_availability,
accounts.forms.ProfileForm, matching exclusion)."""

import datetime
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.forms import ProfileForm
from accounts.models import Profile, Users
from myapp.middleware import PresenceMiddleware
from myapp.models import HelpRequest
from myapp.services import matching, presence

PUBLISH = "myapp.realtime.publish.send_to_staff"
MIDDLEWARE_PATH = "myapp.middleware.PresenceMiddleware"
WITH_PRESENCE_MIDDLEWARE = (
    settings.MIDDLEWARE
    if MIDDLEWARE_PATH in settings.MIDDLEWARE
    else [*settings.MIDDLEWARE, MIDDLEWARE_PATH]
)
WITHOUT_PRESENCE_MIDDLEWARE = [m for m in settings.MIDDLEWARE if m != MIDDLEWARE_PATH]


def _user(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password="pass-Presence-1",
        region="dushanbe", **flags,
    )


def _profile(user):
    return Profile.objects.get(user=user)


def _set_profile(user, **values):
    Profile.objects.filter(user=user).update(**values)


class PresenceTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.now = timezone.now()
        self.client_user = _user("pr_client", is_client=True)
        self.volunteer = _user("pr_vol", is_volunteer=True)

    def tearDown(self):
        cache.clear()

    def _task(self, status="pending", volunteer=None, **extra):
        return HelpRequest.objects.create(
            client=self.client_user, volunteer=volunteer, help_type="grocery", description="d",
            address="a", phone="+992900000000", region="dushanbe", status=status, **extra,
        )


class ComputeStatusTests(TestCase):
    def test_table(self):
        now = timezone.now()
        idle = datetime.timedelta(minutes=settings.AVAILABILITY_IDLE_MINUTES)
        cases = [
            # is_volunteer, has_active_task, last_seen_at, expected
            (True, True, None, "busy"),
            (True, True, now - idle * 10, "busy"),
            (True, False, now, "available"),
            (True, False, now - idle, "available"),  # boundary is inclusive
            (True, False, now - idle - datetime.timedelta(seconds=1), "offline"),
            (True, False, None, "offline"),
            (False, False, now, "offline"),
        ]
        for is_volunteer, active, seen, expected in cases:
            with self.subTest(is_volunteer=is_volunteer, active=active, seen=seen):
                self.assertEqual(
                    presence.compute_status(
                        is_volunteer=is_volunteer, has_active_task=active, last_seen_at=seen, now=now
                    ),
                    expected,
                )

    @override_settings(AVAILABILITY_IDLE_MINUTES=5)
    def test_threshold_comes_from_settings(self):
        now = timezone.now()
        seen = now - datetime.timedelta(minutes=6)
        self.assertEqual(
            presence.compute_status(is_volunteer=True, has_active_task=False, last_seen_at=seen, now=now),
            "offline",
        )


class RecomputeTests(PresenceTestCase):
    def test_non_volunteer_is_noop(self):
        with mock.patch(PUBLISH) as publish:
            self.assertIsNone(presence.recompute(self.client_user))
            self.assertIsNone(presence.recompute(None))
        publish.assert_not_called()

    def test_saves_and_publishes_only_on_change(self):
        _set_profile(self.volunteer, last_seen_at=self.now, availability_status="offline")
        with mock.patch(PUBLISH) as publish:
            self.assertEqual(presence.recompute(self.volunteer), "available")
            self.assertEqual(_profile(self.volunteer).availability_status, "available")
            publish.assert_called_once_with(
                "availability.changed", {"user_id": self.volunteer.pk, "status": "available", "away": False}
            )
            publish.reset_mock()
            self.assertEqual(presence.recompute(self.volunteer), "available")
            publish.assert_not_called()

    def test_payload_has_no_personal_data(self):
        _set_profile(self.volunteer, last_seen_at=None, availability_status="available")
        with mock.patch(PUBLISH) as publish:
            presence.recompute(self.volunteer)
        _type, payload = publish.call_args.args
        self.assertEqual(set(payload), {"user_id", "status", "away"})

    def test_updates_loaded_profile_instance(self):
        profile = self.volunteer.profile  # cache the related object
        _set_profile(self.volunteer, last_seen_at=None, availability_status="available")
        with mock.patch(PUBLISH):
            presence.recompute(self.volunteer)
        self.assertEqual(profile.availability_status, "offline")


class MarkSeenTests(PresenceTestCase):
    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):
            presence.mark_seen(self.volunteer, source="carrier-pigeon")

    def test_one_write_per_interval(self):
        with mock.patch(PUBLISH):
            self.assertTrue(presence.mark_seen(self.volunteer, source="web"))
        first = _profile(self.volunteer).last_seen_at
        self.assertIsNotNone(first)
        _set_profile(self.volunteer, last_seen_at=first - datetime.timedelta(minutes=5))
        with self.assertNumQueries(0):
            self.assertFalse(presence.mark_seen(self.volunteer, source="ws"))
        # Still the older value: the second call was throttled.
        self.assertLess(_profile(self.volunteer).last_seen_at, first)
        cache.delete(f"presence:seen:{self.volunteer.pk}")  # interval elapsed
        with mock.patch(PUBLISH):
            self.assertTrue(presence.mark_seen(self.volunteer, source="telegram"))
        self.assertGreaterEqual(_profile(self.volunteer).last_seen_at, first)

    def test_offline_volunteer_becomes_available(self):
        _set_profile(self.volunteer, availability_status="offline", last_seen_at=None)
        with mock.patch(PUBLISH) as publish:
            presence.mark_seen(self.volunteer, source="web")
        self.assertEqual(_profile(self.volunteer).availability_status, "available")
        publish.assert_called_once()

    def test_available_volunteer_not_recomputed(self):
        with mock.patch.object(presence, "recompute") as recompute:
            presence.mark_seen(self.volunteer, source="web")
        recompute.assert_not_called()

    def test_busy_volunteer_stays_busy(self):
        self._task(status="active", volunteer=self.volunteer)
        _set_profile(self.volunteer, availability_status="busy")
        with mock.patch(PUBLISH):
            presence.mark_seen(self.volunteer, source="web")
        self.assertEqual(_profile(self.volunteer).availability_status, "busy")

    def test_non_volunteer_gets_last_seen_without_status_logic(self):
        with mock.patch(PUBLISH) as publish:
            self.assertTrue(presence.mark_seen(self.client_user, source="web"))
        self.assertIsNotNone(_profile(self.client_user).last_seen_at)
        publish.assert_not_called()


class OnTaskChangeTests(PresenceTestCase):
    def setUp(self):
        super().setUp()
        _set_profile(self.volunteer, last_seen_at=self.now, availability_status="available")

    def test_accept_then_complete(self):
        task = self._task(status="active", volunteer=self.volunteer)
        with mock.patch(PUBLISH):
            self.assertEqual(presence.on_task_change(task), "busy")
            HelpRequest.objects.filter(pk=task.pk).update(status="completed")
            self.assertEqual(presence.on_task_change(task), "available")

    def test_cancel_of_active_task_frees_volunteer(self):
        task = self._task(status="active", volunteer=self.volunteer)
        with mock.patch(PUBLISH):
            presence.on_task_change(task)
            HelpRequest.objects.filter(pk=task.pk).update(status="cancelled")
            self.assertEqual(presence.on_task_change(task), "available")

    def test_complete_one_of_two_active_tasks_stays_busy(self):
        task = self._task(status="active", volunteer=self.volunteer)
        self._task(status="active", volunteer=self.volunteer)
        with mock.patch(PUBLISH):
            HelpRequest.objects.filter(pk=task.pk).update(status="completed")
            self.assertEqual(presence.on_task_change(task), "busy")

    def test_idle_volunteer_goes_offline_after_task_ends(self):
        _set_profile(self.volunteer, last_seen_at=self.now - datetime.timedelta(hours=3), availability_status="busy")
        task = self._task(status="completed", volunteer=self.volunteer)
        with mock.patch(PUBLISH):
            self.assertEqual(presence.on_task_change(task), "offline")

    def test_task_without_volunteer_is_noop(self):
        self.assertIsNone(presence.on_task_change(self._task()))


class SetAwayTests(PresenceTestCase):
    def test_on_with_future_datetime(self):
        until = self.now + datetime.timedelta(days=3)
        with mock.patch(PUBLISH) as publish:
            presence.set_away(self.volunteer, True, until)
        profile = _profile(self.volunteer)
        self.assertTrue(profile.is_away)
        self.assertEqual(profile.away_until, until)
        self.assertTrue(profile.is_away_now)
        self.assertTrue(publish.call_args.args[1]["away"])

    def test_date_means_through_that_day(self):
        day = timezone.localdate() + datetime.timedelta(days=2)
        with mock.patch(PUBLISH):
            _away, until = presence.set_away(self.volunteer, True, day)
        self.assertEqual(timezone.localtime(until).date(), day + datetime.timedelta(days=1))
        self.assertEqual(timezone.localtime(until).time(), datetime.time.min)

    def test_past_until_rejected(self):
        with self.assertRaisesMessage(ValueError, "until_in_past"):
            presence.set_away(self.volunteer, True, self.now - datetime.timedelta(minutes=1))
        with self.assertRaisesMessage(ValueError, "until_in_past"):
            presence.set_away(self.volunteer, True, timezone.localdate() - datetime.timedelta(days=1))
        self.assertFalse(_profile(self.volunteer).is_away)

    def test_clearing_resets_until(self):
        with mock.patch(PUBLISH):
            presence.set_away(self.volunteer, True, self.now + datetime.timedelta(days=1))
            presence.set_away(self.volunteer, False, self.now + datetime.timedelta(days=5))
        profile = _profile(self.volunteer)
        self.assertFalse(profile.is_away)
        self.assertIsNone(profile.away_until)

    def test_open_ended(self):
        with mock.patch(PUBLISH):
            presence.set_away(self.volunteer, True)
        self.assertTrue(_profile(self.volunteer).is_away_now)

    def test_non_volunteer_rejected(self):
        with self.assertRaisesMessage(ValueError, "not_volunteer"):
            presence.set_away(self.client_user, True)

    def test_does_not_touch_availability(self):
        _set_profile(self.volunteer, availability_status="available", last_seen_at=self.now)
        with mock.patch(PUBLISH):
            presence.set_away(self.volunteer, True)
        self.assertEqual(_profile(self.volunteer).availability_status, "available")


class SweepTests(PresenceTestCase):
    def setUp(self):
        super().setUp()
        long_ago = self.now - datetime.timedelta(hours=2)
        self.idle = self.volunteer
        _set_profile(self.idle, last_seen_at=long_ago, availability_status="available")
        self.never = _user("pr_never", is_volunteer=True)  # last_seen_at NULL, default "available"
        self.fresh = _user("pr_fresh", is_volunteer=True)
        _set_profile(self.fresh, last_seen_at=self.now, availability_status="available")
        self.worker = _user("pr_worker", is_volunteer=True)
        _set_profile(self.worker, last_seen_at=long_ago, availability_status="busy")
        self._task(status="active", volunteer=self.worker)
        self.came_back = _user("pr_back", is_volunteer=True)
        _set_profile(self.came_back, last_seen_at=self.now, availability_status="offline")
        self.vacation_over = _user("pr_vac_over", is_volunteer=True)
        _set_profile(
            self.vacation_over, last_seen_at=self.now, is_away=True,
            away_until=self.now - datetime.timedelta(minutes=1),
        )
        self.vacation = _user("pr_vac", is_volunteer=True)
        _set_profile(
            self.vacation, last_seen_at=self.now, is_away=True,
            away_until=self.now + datetime.timedelta(days=1),
        )

    def test_sweep(self):
        with mock.patch(PUBLISH) as publish:
            result = presence.sweep()
        status = lambda u: _profile(u).availability_status  # noqa: E731
        self.assertEqual(status(self.idle), "offline")
        self.assertEqual(status(self.never), "offline")
        self.assertEqual(status(self.fresh), "available")
        self.assertEqual(status(self.worker), "busy")  # active task wins over idleness
        self.assertEqual(status(self.came_back), "available")
        self.assertFalse(_profile(self.vacation_over).is_away)
        self.assertIsNone(_profile(self.vacation_over).away_until)
        self.assertTrue(_profile(self.vacation).is_away)
        self.assertEqual(result.became_offline, 2)
        self.assertEqual(result.became_available, 1)
        self.assertEqual(result.became_busy, 0)
        self.assertEqual(result.away_cleared, 1)
        self.assertEqual(int(result), 4)
        self.assertEqual(publish.call_count, 4)
        # Idempotent.
        with mock.patch(PUBLISH) as publish:
            self.assertEqual(int(presence.sweep()), 0)
        publish.assert_not_called()

    def test_repairs_busy_drift(self):
        _set_profile(self.worker, availability_status="available")
        with mock.patch(PUBLISH):
            result = presence.sweep()
        self.assertEqual(result.became_busy, 1)
        self.assertEqual(_profile(self.worker).availability_status, "busy")

    def test_dry_run_writes_nothing(self):
        with mock.patch(PUBLISH) as publish:
            result = presence.sweep(dry_run=True)
        self.assertEqual(int(result), 4)
        publish.assert_not_called()
        self.assertEqual(_profile(self.idle).availability_status, "available")
        self.assertTrue(_profile(self.vacation_over).is_away)

    def test_command(self):
        out = StringIO()
        with mock.patch(PUBLISH):
            call_command("sweep_availability", "--dry-run", stdout=out)
            self.assertIn("[dry-run] 4 change(s)", out.getvalue())
            self.assertEqual(_profile(self.idle).availability_status, "available")
            out = StringIO()
            call_command("sweep_availability", stdout=out)
        self.assertIn("4 change(s)", out.getvalue())
        self.assertEqual(_profile(self.idle).availability_status, "offline")


class MiddlewareTests(PresenceTestCase):
    def _middleware(self):
        return PresenceMiddleware(lambda request: "response")

    def test_authenticated_request_marks_seen(self):
        request = RequestFactory().get("/profile/")
        request.user = self.volunteer
        with mock.patch.object(presence, "mark_seen") as mark_seen:
            self.assertEqual(self._middleware()(request), "response")
        mark_seen.assert_called_once_with(self.volunteer, source="web")

    def test_anonymous_and_skipped_paths(self):
        from django.contrib.auth.models import AnonymousUser

        middleware = self._middleware()
        with mock.patch.object(presence, "mark_seen") as mark_seen:
            request = RequestFactory().get("/profile/")
            request.user = AnonymousUser()
            middleware(request)
            for path in ("/health/", "/health/ready/", "/ws/", settings.STATIC_URL + "css/style.css"):
                request = RequestFactory().get(path)
                request.user = self.volunteer
                middleware(request)
            request = RequestFactory().get("/")  # no request.user at all
            middleware(request)
        mark_seen.assert_not_called()

    def test_never_raises(self):
        request = RequestFactory().get("/profile/")
        request.user = self.volunteer
        with mock.patch.object(presence, "mark_seen", side_effect=RuntimeError("db down")):
            self.assertEqual(self._middleware()(request), "response")

    @override_settings(MIDDLEWARE=WITH_PRESENCE_MIDDLEWARE)
    def test_end_to_end_page_view_brings_volunteer_online(self):
        _set_profile(self.volunteer, availability_status="offline", last_seen_at=None)
        self.client.force_login(self.volunteer)
        with mock.patch(PUBLISH):
            self.client.get(reverse("profile"))
        profile = _profile(self.volunteer)
        self.assertIsNotNone(profile.last_seen_at)
        self.assertEqual(profile.availability_status, "available")

    @override_settings(MIDDLEWARE=WITH_PRESENCE_MIDDLEWARE)
    def test_end_to_end_anonymous_untouched(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Profile.objects.exclude(last_seen_at=None).exists())


class MatchingDoNotDisturbTests(PresenceTestCase):
    def setUp(self):
        super().setUp()
        _set_profile(self.volunteer, latitude="38.560000", longitude="68.780000", availability_status="available")
        self.other = _user("pr_other", is_volunteer=True)
        _set_profile(self.other, latitude="38.570000", longitude="68.790000", availability_status="available")
        self.task = self._task(latitude="38.561000", longitude="68.781000")

    def _recommended(self):
        return {item["volunteer"].pk for item in matching.recommend_volunteers(self.task, limit=10)}

    def test_away_volunteer_excluded_then_included_after_until(self):
        self.assertEqual(self._recommended(), {self.volunteer.pk, self.other.pk})
        _set_profile(self.volunteer, is_away=True, away_until=self.now + datetime.timedelta(hours=1))
        self.assertEqual(self._recommended(), {self.other.pk})
        _set_profile(self.volunteer, away_until=self.now - datetime.timedelta(seconds=1))
        self.assertEqual(self._recommended(), {self.volunteer.pk, self.other.pk})

    def test_open_ended_away_excluded(self):
        _set_profile(self.volunteer, is_away=True, away_until=None)
        self.assertEqual(self._recommended(), {self.other.pk})

    def test_recommend_tasks_unaffected(self):
        _set_profile(self.volunteer, is_away=True, away_until=None)
        volunteer = Users.objects.select_related("profile").get(pk=self.volunteer.pk)
        tasks = [item["task"].pk for item in matching.recommend_tasks(volunteer)]
        self.assertIn(self.task.pk, tasks)

    def test_ai_available_volunteers_excludes_away(self):
        from myapp.services.ai import read_tools

        _set_profile(self.volunteer, is_away=True, away_until=None)
        result = read_tools.list_available_volunteers(None)
        ids = {item["id"] for item in result["items"]}
        self.assertNotIn(self.volunteer.pk, ids)
        self.assertIn(self.other.pk, ids)


class ProfileFormTests(PresenceTestCase):
    def _data(self, **extra):
        return {"full_name": "Имя", "age": "30", "bio": "", **extra}

    def test_availability_not_a_field(self):
        form = ProfileForm(instance=self.volunteer.profile)
        self.assertNotIn("availability_status", form.fields)

    def test_posting_availability_changes_nothing(self):
        _set_profile(self.volunteer, availability_status="offline")
        form = ProfileForm(self._data(availability_status="available"), instance=_profile(self.volunteer))
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(_profile(self.volunteer).availability_status, "offline")

    @override_settings(MIDDLEWARE=WITHOUT_PRESENCE_MIDDLEWARE)
    def test_edit_profile_view_ignores_availability(self):
        _set_profile(self.volunteer, availability_status="offline")
        self.client.force_login(self.volunteer)
        response = self.client.get(reverse("edit_profile"))
        self.assertNotContains(response, 'name="availability_status"')
        self.assertContains(response, 'name="is_away"')
        self.assertContains(response, 'data-i18n="profile.away_label"')
        with mock.patch(PUBLISH):
            self.client.post(
                reverse("edit_profile"),
                {"email": self.volunteer.email, "region": "dushanbe", **self._data(availability_status="available")},
            )
        # The posted field is ignored (the presence middleware is off here, so
        # nothing else can legitimately bring them online).
        self.assertEqual(_profile(self.volunteer).availability_status, "offline")

    def test_volunteer_sets_away_with_date(self):
        day = timezone.localdate() + datetime.timedelta(days=3)
        form = ProfileForm(self._data(is_away="on", away_until=day.isoformat()), instance=_profile(self.volunteer))
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch(PUBLISH) as publish:
            form.save()
        profile = _profile(self.volunteer)
        self.assertTrue(profile.is_away_now)
        self.assertEqual(timezone.localtime(profile.away_until).date(), day + datetime.timedelta(days=1))
        publish.assert_called_once()
        # The bound edit form shows the inclusive last day back.
        self.assertEqual(ProfileForm(instance=profile).initial["away_until"], day)

    def test_today_is_allowed_past_date_rejected(self):
        today = timezone.localdate()
        form = ProfileForm(self._data(is_away="on", away_until=today.isoformat()), instance=_profile(self.volunteer))
        self.assertTrue(form.is_valid(), form.errors)
        past = today - datetime.timedelta(days=1)
        form = ProfileForm(self._data(is_away="on", away_until=past.isoformat()), instance=_profile(self.volunteer))
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["away_until"][0].code, "away_until_past")

    def test_switching_off_clears_date(self):
        _set_profile(self.volunteer, is_away=True, away_until=self.now + datetime.timedelta(days=2))
        past = timezone.localdate() - datetime.timedelta(days=1)
        form = ProfileForm(self._data(away_until=past.isoformat()), instance=_profile(self.volunteer))
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch(PUBLISH):
            form.save()
        profile = _profile(self.volunteer)
        self.assertFalse(profile.is_away)
        self.assertIsNone(profile.away_until)

    def test_unchanged_away_does_not_publish(self):
        form = ProfileForm(self._data(), instance=_profile(self.volunteer))
        self.assertTrue(form.is_valid(), form.errors)
        with mock.patch(PUBLISH) as publish:
            form.save()
        publish.assert_not_called()

    def test_client_cannot_set_away(self):
        form = ProfileForm(self._data(is_away="on"), instance=_profile(self.client_user))
        self.assertNotIn("is_away", form.fields)
        self.assertNotIn("away_until", form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertFalse(_profile(self.client_user).is_away)
