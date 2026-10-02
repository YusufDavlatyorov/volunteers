"""Feature Pack v2, feature 7 — live tracking (myapp/services/tracking.py,
myapp/views_tracking.py, purge_tracking)."""

import datetime
import json
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import HelpRequest, TaskLocationPoint
from myapp.services import tracking

PUB_TASK = "myapp.services.tracking.publish.send_to_task"
PUB_MAP = "myapp.services.tracking.publish.send_to_map_staff"
NOTIFY = "myapp.services.tracking.notify"
PASSWORD = "pass-Tracking-1"

# Dushanbe; 0.001° lat ≈ 111 m.
LAT, LNG = 38.5598, 68.7870


def _user(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD,
        region="dushanbe", **flags,
    )


class TrackingBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client_user = _user("tr_client", is_client=True)
        self.other_client = _user("tr_client2", is_client=True)
        self.volunteer = _user("tr_vol", is_volunteer=True)
        self.other_volunteer = _user("tr_vol2", is_volunteer=True)
        self.curator = _user("tr_cur", is_curator=True)
        self.admin = Users.objects.create_superuser(username="tr_admin", email="tr_admin@example.com",
                                                    password=PASSWORD)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="d",
            address="a", phone="+992900000000", region="dushanbe", status="active",
            latitude=38.57, longitude=68.79,
        )
        tracking.give_consent(self.volunteer)
        self.now = timezone.now()

    def tearDown(self):
        cache.clear()

    def _accept(self, lat=LAT, lng=LNG, *, at=None, volunteer=None, task=None, **kw):
        with mock.patch(PUB_TASK) as pt, mock.patch(PUB_MAP) as pm:
            outcome = tracking.accept_point(volunteer or self.volunteer, (task or self.task).pk, lat, lng,
                                            recorded_at=at or timezone.now(), **kw)
        return outcome, pt, pm

    def _refresh(self):
        self.task.refresh_from_db()
        return self.task


class ConsentTests(TrackingBase):
    def test_give_and_revoke(self):
        vol = _user("tr_vol3", is_volunteer=True)
        self.assertFalse(tracking.has_consent(vol))
        self.assertTrue(tracking.give_consent(vol))
        self.assertTrue(tracking.has_consent(vol))
        first = Profile.objects.get(user=vol).tracking_consent_at
        tracking.give_consent(vol)  # idempotent: first answer wins
        self.assertEqual(Profile.objects.get(user=vol).tracking_consent_at, first)
        self.assertTrue(tracking.revoke_consent(vol))
        self.assertFalse(tracking.has_consent(vol))
        self.assertFalse(tracking.revoke_consent(vol))

    def test_non_volunteer_cannot_consent(self):
        self.assertFalse(tracking.give_consent(self.client_user))
        self.assertFalse(tracking.has_consent(self.client_user))

    def test_no_consent_no_share(self):
        tracking.revoke_consent(self.volunteer)
        self.assertFalse(tracking.can_share(self.volunteer, self.task))
        outcome, pt, _ = self._accept()
        self.assertEqual(outcome.code, tracking.FORBIDDEN)
        pt.assert_not_called()

    def test_revoke_stops_active_sharing(self):
        self._accept()
        with mock.patch(NOTIFY) as notify, mock.patch(PUB_TASK) as pt, mock.patch(PUB_MAP):
            tracking.revoke_consent(self.volunteer)
        task = self._refresh()
        self.assertIsNotNone(task.tracking_stopped_at)
        self.assertIsNone(task.volunteer_last_lat)
        self.assertFalse(TaskLocationPoint.objects.filter(task=task).exists())
        pt.assert_called_once_with(task.pk, "tracking.stopped", {"task_id": task.pk, "reason": "consent_revoked"})
        notify.assert_called_once()


class CanShareTests(TrackingBase):
    def test_only_assigned_volunteer_of_active_task(self):
        self.assertTrue(tracking.can_share(self.volunteer, self.task))
        tracking.give_consent(self.other_volunteer)
        self.assertFalse(tracking.can_share(self.other_volunteer, self.task))
        self.assertFalse(tracking.can_share(self.client_user, self.task))
        self.assertFalse(tracking.can_share(self.curator, self.task))
        for status in ("pending", "completed", "cancelled"):
            self.task.status = status
            self.assertFalse(tracking.can_share(self.volunteer, self.task), status)

    def test_inactive_user_cannot_share(self):
        self.volunteer.is_active = False
        self.volunteer.save()
        self.assertFalse(tracking.can_share(self.volunteer, self.task))


class AcceptPointTests(TrackingBase):
    def test_happy_path_stores_and_publishes(self):
        outcome, pt, pm = self._accept(accuracy=12.4, speed=3.2, heading=90, at=self.now)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, tracking.OK)
        task = self._refresh()
        self.assertAlmostEqual(float(task.volunteer_last_lat), LAT)
        self.assertAlmostEqual(float(task.volunteer_last_lng), LNG)
        self.assertEqual(task.volunteer_last_at, self.now)
        point = TaskLocationPoint.objects.get(task=task)
        self.assertEqual(point.accuracy_m, 12.4)
        self.assertEqual(point.source, "web")
        expected = {"task_id": task.pk, "lat": LAT, "lng": LNG, "ts": self.now.isoformat(), "accuracy": 12}
        self.assertEqual(outcome.payload, expected)
        pt.assert_called_once_with(task.pk, "location.updated", expected)
        pm.assert_called_once_with("map.marker", {
            "kind": "volunteer_live", "task_id": task.pk, "lat": LAT, "lng": LNG, "ts": self.now.isoformat(),
        })

    def test_payloads_never_contain_identity_or_contacts(self):
        outcome, pt, pm = self._accept(accuracy=5)
        blob = json.dumps([outcome.payload, pt.call_args.args, pm.call_args.args], default=str)
        for secret in ("+992900000000", "@example.com", "tr_vol", "tr_client", "phone", "email", "username"):
            self.assertNotIn(secret, blob)
        self.assertEqual(set(outcome.payload), {"task_id", "lat", "lng", "ts", "accuracy"})
        self.assertEqual(set(pm.call_args.args[1]), {"kind", "task_id", "lat", "lng", "ts"})

    def test_marks_presence_seen(self):
        with mock.patch("myapp.services.tracking.presence.mark_seen") as seen:
            self._accept(source="ws")
        seen.assert_called_once_with(self.volunteer, source="ws")

    def test_telegram_source_is_stored(self):
        self._accept(source="telegram")
        self.assertEqual(TaskLocationPoint.objects.get(task=self.task).source, "telegram")

    def test_clears_tracking_stopped_at(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(tracking_stopped_at=self.now)
        self.assertTrue(self._accept()[0].ok)
        self.assertIsNone(self._refresh().tracking_stopped_at)

    def test_forbidden_for_others_and_inactive(self):
        tracking.give_consent(self.other_volunteer)
        self.assertEqual(self._accept(volunteer=self.other_volunteer)[0].code, tracking.FORBIDDEN)
        self.assertEqual(self._accept(volunteer=self.client_user)[0].code, tracking.FORBIDDEN)
        self.assertEqual(self._accept(volunteer=self.curator)[0].code, tracking.FORBIDDEN)
        self.task.status = "completed"
        self.task.save(update_fields=["status"])
        self.assertEqual(self._accept()[0].code, tracking.FORBIDDEN)
        self.assertEqual(tracking.accept_point(self.volunteer, 999999, LAT, LNG).code, tracking.FORBIDDEN)
        self.assertFalse(TaskLocationPoint.objects.exists())

    def test_invalid_coordinates(self):
        for lat, lng in ((999, LNG), (LAT, -181), ("abc", LNG), (None, None), (float("nan"), LNG),
                         (float("inf"), LNG)):
            self.assertEqual(self._accept(lat, lng)[0].code, tracking.INVALID, (lat, lng))
        self.assertEqual(tracking.accept_point(self.volunteer, "x", LAT, LNG).code, tracking.INVALID)
        self.assertEqual(self._accept(accuracy=-5)[0].code, tracking.INVALID)
        self.assertFalse(TaskLocationPoint.objects.exists())

    def test_inaccurate(self):
        self.assertEqual(self._accept(accuracy=201)[0].code, tracking.INACCURATE)
        self.assertTrue(self._accept(accuracy=200)[0].ok)

    def test_too_soon(self):
        start = self.now - datetime.timedelta(seconds=10)
        self.assertTrue(self._accept(at=start)[0].ok)
        outcome, pt, _ = self._accept(LAT + 0.0001, LNG, at=start + datetime.timedelta(seconds=4))
        self.assertEqual(outcome.code, tracking.TOO_SOON)
        pt.assert_not_called()
        self.assertTrue(self._accept(LAT + 0.0001, LNG, at=start + datetime.timedelta(seconds=5))[0].ok)
        self.assertEqual(TaskLocationPoint.objects.filter(task=self.task).count(), 2)

    def test_jump_rejected(self):
        self.assertTrue(self._accept(at=self.now - datetime.timedelta(seconds=60))[0].ok)
        # ~11 km in 60 s ≈ 660 km/h.
        outcome, _, _ = self._accept(LAT + 0.1, LNG, at=self.now)
        self.assertEqual(outcome.code, tracking.JUMP)
        self.assertAlmostEqual(float(self._refresh().volunteer_last_lat), LAT)
        # A plausible move (≈ 111 m in 30 s ≈ 13 km/h) is fine.
        self.assertTrue(self._accept(LAT + 0.001, LNG, at=self.now + datetime.timedelta(seconds=30))[0].ok)

    def test_two_consistent_points_override_an_outlier_reference(self):
        # A bad first fix far away, then the real position twice.
        self.assertTrue(self._accept(LAT + 0.5, LNG, at=self.now - datetime.timedelta(seconds=30))[0].ok)
        self.assertEqual(self._accept(at=self.now - datetime.timedelta(seconds=20))[0].code, tracking.JUMP)
        outcome = self._accept(LAT + 0.0002, LNG, at=self.now - datetime.timedelta(seconds=10))[0]
        self.assertTrue(outcome.ok)
        self.assertAlmostEqual(float(self._refresh().volunteer_last_lat), LAT + 0.0002)

    def test_untrusted_recorded_at_falls_back_to_server_time(self):
        outcome = self._accept(at=self.now - datetime.timedelta(hours=3))[0]
        self.assertTrue(outcome.ok)
        self.assertGreater(self._refresh().volunteer_last_at, self.now - datetime.timedelta(minutes=1))


class StopEndTests(TrackingBase):
    def setUp(self):
        super().setUp()
        self._accept(at=self.now - datetime.timedelta(seconds=30))
        self._accept(LAT + 0.0005, LNG, at=self.now)

    def test_stop_by_volunteer(self):
        with mock.patch(NOTIFY) as notify, mock.patch(PUB_TASK) as pt, mock.patch(PUB_MAP) as pm:
            self.assertTrue(tracking.stop(self.volunteer, self.task))
        task = self._refresh()
        self.assertIsNotNone(task.tracking_stopped_at)
        self.assertIsNone(task.volunteer_last_lat)
        self.assertIsNone(tracking.latest(task))
        self.assertFalse(TaskLocationPoint.objects.filter(task=task).exists())
        pt.assert_called_once_with(task.pk, "tracking.stopped", {"task_id": task.pk, "reason": "volunteer"})
        pm.assert_called_once_with("map.marker", {"kind": "volunteer_live", "task_id": task.pk, "removed": True})
        notify.assert_called_once()
        recipients, ntype = notify.call_args.args
        self.assertEqual(ntype, "tracking_stopped")
        self.assertEqual(set(recipients), {self.curator, self.admin})
        self.assertEqual(notify.call_args.kwargs["params"], {"task_id": task.pk})
        self.assertEqual(notify.call_args.kwargs["link"], reverse("task_detail", args=[task.pk]))

    def test_stop_twice_notifies_staff_once(self):
        with mock.patch(NOTIFY) as notify, mock.patch(PUB_TASK), mock.patch(PUB_MAP):
            tracking.stop(self.volunteer, self.task)
            tracking.stop(self.volunteer, self._refresh())
        self.assertEqual(notify.call_count, 1)

    def test_only_assigned_volunteer_can_stop(self):
        with mock.patch(NOTIFY) as notify:
            self.assertFalse(tracking.stop(self.other_volunteer, self.task))
            self.assertFalse(tracking.stop(self.client_user, self.task))
        notify.assert_not_called()
        self.assertEqual(TaskLocationPoint.objects.filter(task=self.task).count(), 2)

    def test_end_deletes_trail_and_latest(self):
        self.task.status = "completed"
        self.task.save(update_fields=["status"])
        with mock.patch(PUB_TASK) as pt, mock.patch(PUB_MAP):
            self.assertEqual(tracking.end(self.task), 2)
        task = self._refresh()
        self.assertIsNone(task.volunteer_last_at)
        self.assertFalse(TaskLocationPoint.objects.filter(task=task).exists())
        pt.assert_called_once_with(task.pk, "tracking.stopped", {"task_id": task.pk, "reason": "task_ended"})
        self.assertEqual(tracking.end(self.task), 0)  # idempotent

    def test_latest_and_trail(self):
        self._refresh()
        self.assertEqual(tracking.latest(self.task)["lat"], round(LAT + 0.0005, 6))
        points = tracking.trail(self.task)
        self.assertEqual([p["lat"] for p in points], [LAT, round(LAT + 0.0005, 6)])  # oldest first
        self.assertEqual(len(tracking.trail(self.task, limit=1)), 1)
        self.assertEqual(tracking.trail(self.task, limit=1)[0]["lat"], round(LAT + 0.0005, 6))
        self.task.status = "completed"
        self.assertIsNone(tracking.latest(self.task))
        self.assertEqual(tracking.trail(self.task), [])

    def test_live_markers(self):
        markers = tracking.live_markers()
        self.assertEqual(len(markers), 1)
        self.assertEqual(set(markers[0]), {"kind", "task_id", "lat", "lng", "ts"})


class PurgeTests(TrackingBase):
    def test_purge_deletes_old_points_and_ended_tasks(self):
        self._accept(at=self.now)
        old = TaskLocationPoint.objects.create(
            task=self.task, volunteer=self.volunteer, latitude=LAT, longitude=LNG,
            recorded_at=self.now - datetime.timedelta(hours=25),
        )
        TaskLocationPoint.objects.filter(pk=old.pk).update(created_at=self.now - datetime.timedelta(hours=25))
        ended = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="d",
            address="a", phone="+992900000000", region="dushanbe", status="completed",
            volunteer_last_lat=LAT, volunteer_last_lng=LNG, volunteer_last_at=self.now,
        )
        TaskLocationPoint.objects.create(task=ended, volunteer=self.volunteer, latitude=LAT, longitude=LNG,
                                         recorded_at=self.now)

        self.assertEqual(tracking.purge(dry_run=True), 2)
        self.assertEqual(TaskLocationPoint.objects.count(), 3)
        self.assertEqual(tracking.purge(), 2)
        self.assertEqual(list(TaskLocationPoint.objects.values_list("task_id", flat=True)), [self.task.pk])
        ended.refresh_from_db()
        self.assertIsNone(ended.volunteer_last_at)
        self.assertIsNotNone(self._refresh().volunteer_last_at)  # live task untouched

    def test_purge_command(self):
        TaskLocationPoint.objects.create(task=self.task, volunteer=self.volunteer, latitude=LAT, longitude=LNG,
                                         recorded_at=self.now - datetime.timedelta(hours=30))
        out = StringIO()
        call_command("purge_tracking", "--dry-run", stdout=out)
        self.assertIn("1 trail point", out.getvalue())
        self.assertEqual(TaskLocationPoint.objects.count(), 1)
        call_command("purge_tracking", stdout=StringIO())
        self.assertEqual(TaskLocationPoint.objects.count(), 0)


class TrackingHttpTests(TrackingBase):
    def _login(self, user):
        self.client.force_login(user)

    def _post_point(self, data, as_json=True):
        url = reverse("task_location", args=[self.task.pk])
        with mock.patch(PUB_TASK), mock.patch(PUB_MAP):
            if as_json:
                return self.client.post(url, json.dumps(data), content_type="application/json")
            return self.client.post(url, data)

    def test_anonymous_redirected(self):
        for name, method in (("task_location", "get"), ("task_location", "post"), ("tracking_stop", "post")):
            response = getattr(self.client, method)(reverse(name, args=[self.task.pk]))
            self.assertEqual(response.status_code, 302, name)
        self.assertEqual(self.client.post(reverse("tracking_consent")).status_code, 302)

    def test_volunteer_posts_point_json_and_form(self):
        self._login(self.volunteer)
        response = self._post_point({"lat": LAT, "lng": LNG, "accuracy": 10})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], "ok")
        self.assertEqual(response.json()["point"]["lat"], LAT)
        HelpRequest.objects.filter(pk=self.task.pk).update(
            volunteer_last_at=timezone.now() - datetime.timedelta(seconds=30))
        response = self._post_point({"lat": str(LAT + 0.0003), "lng": str(LNG), "accuracy": "9"}, as_json=False)
        self.assertEqual(response.json()["code"], "ok")
        self.assertEqual(TaskLocationPoint.objects.filter(task=self.task).count(), 2)

    def test_post_soft_drop_and_errors(self):
        self._login(self.volunteer)
        self.assertEqual(self._post_point({"lat": LAT, "lng": LNG, "accuracy": 500}).json()["code"], "inaccurate")
        self.assertEqual(self._post_point({"lat": 999, "lng": LNG}).status_code, 400)
        response = self.client.post(reverse("task_location", args=[self.task.pk]), "{bad",
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_post_forbidden_for_non_assignee(self):
        for user in (self.other_volunteer, self.client_user, self.curator):
            self._login(user)
            self.assertEqual(self._post_point({"lat": LAT, "lng": LNG}).status_code, 403)
        self.assertFalse(TaskLocationPoint.objects.exists())

    def test_get_latest_permissions(self):
        self._accept(at=self.now)
        url = reverse("task_location", args=[self.task.pk])
        for user in (self.client_user, self.curator, self.admin, self.volunteer):
            self._login(user)
            response = self.client.get(url, {"trail": "1"})
            self.assertEqual(response.status_code, 200, user.username)
            data = response.json()
            self.assertEqual(data["latest"]["lat"], LAT)
            self.assertEqual(len(data["trail"]), 1)
            blob = json.dumps(data)
            self.assertNotIn("@example.com", blob)
            self.assertNotIn("+992", blob)
        for user in (self.other_client, self.other_volunteer):
            self._login(user)
            self.assertEqual(self.client.get(url).status_code, 403, user.username)

    def test_get_forbidden_when_task_not_active(self):
        self._accept(at=self.now)
        url = reverse("task_location", args=[self.task.pk])
        for status in ("pending", "completed", "cancelled"):
            HelpRequest.objects.filter(pk=self.task.pk).update(status=status)
            for user in (self.client_user, self.curator, self.volunteer):
                self._login(user)
                self.assertEqual(self.client.get(url).status_code, 403, (status, user.username))
        self._login(self.curator)
        self.assertEqual(self.client.get(reverse("task_location", args=[999999])).status_code, 404)

    def test_pending_task_viewer_cannot_see_location(self):
        # A region volunteer who may *view* a pending task never sees location.
        pending = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="d", address="a",
            phone="+992900000000", region="dushanbe", status="pending",
        )
        self._login(self.other_volunteer)
        self.assertEqual(self.client.get(reverse("task_location", args=[pending.pk])).status_code, 403)

    def test_stop_view(self):
        self._accept(at=self.now)
        url = reverse("tracking_stop", args=[self.task.pk])
        self._login(self.client_user)
        self.assertEqual(self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 403)
        self._login(self.volunteer)
        with mock.patch(NOTIFY) as notify, mock.patch(PUB_TASK), mock.patch(PUB_MAP):
            response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        notify.assert_called_once()
        self.assertFalse(TaskLocationPoint.objects.filter(task=self.task).exists())
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_consent_view(self):
        url = reverse("tracking_consent")
        self._login(self.client_user)
        self.assertEqual(self.client.post(url, {"action": "give"}).status_code, 403)
        vol = _user("tr_vol4", is_volunteer=True)
        self._login(vol)
        response = self.client.post(url, json.dumps({"action": "give"}), content_type="application/json")
        self.assertEqual(response.json(), {"ok": True, "consent": True})
        self.assertTrue(tracking.has_consent(vol))
        response = self.client.post(url, {"action": "revoke", "next": "/myapp/tasks/"})
        self.assertRedirects(response, "/myapp/tasks/", fetch_redirect_response=False)
        self.assertFalse(tracking.has_consent(vol))
        self.assertEqual(self.client.post(url, {"action": "maybe"}).status_code, 400)
        response = self.client.post(url, {"action": "give", "next": "https://evil.example/"})
        self.assertEqual(response.status_code, 200)  # off-site next is ignored
