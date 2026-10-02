"""Manual assignment of a do-not-disturb volunteer (owner decision 2026-10-02):
listed apart from the ranked shortlist, assignable only with an explicit
confirmation, logged with IDs only, and the volunteer is told it was urgent."""

import json
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import HelpRequest, Notification
from myapp.services import dispatch

PASSWORD = "Dispatch-Tests-2026!"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


class AwayDispatchTests(TestCase):
    def setUp(self):
        self.curator = _make("d_curator", is_curator=True)
        self.client_user = _make("d_client", is_client=True)
        self.available = _make("d_available", is_volunteer=True)
        self.away = _make("d_away", is_volunteer=True)
        self.away_until = timezone.now() + timedelta(days=3)
        Profile.objects.filter(user=self.away).update(
            is_away=True, away_until=self.away_until, latitude=38.56, longitude=68.78
        )
        Profile.objects.filter(user=self.available).update(latitude=38.57, longitude=68.79)
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="bread", address="a",
            phone="+992901234567", region="dushanbe", latitude=38.56, longitude=68.78,
        )
        self.client.force_login(self.curator)

    def _assign(self, volunteer, **data):
        return self.client.post(reverse("task_assign_volunteer", args=[self.task.pk, volunteer.pk]), data)

    def test_recommendations_list_away_volunteers_separately(self):
        data = self.client.get(reverse("task_recommendations", args=[self.task.pk])).json()
        ranked = {item["volunteer_id"] for item in data["recommendations"]}
        away = {item["volunteer_id"]: item for item in data["away_volunteers"]}
        self.assertNotIn(self.away.pk, ranked)
        self.assertIn(self.away.pk, away)
        self.assertNotIn(self.available.pk, away)
        self.assertEqual(away[self.away.pk]["distance_km"], 0.0)
        # Inclusive last day = stored "start of next day" minus one day.
        self.assertIsNotNone(away[self.away.pk]["away_until"])
        self.assertNotIn("email", json.dumps(data["away_volunteers"]))

    def test_expired_away_is_not_listed(self):
        Profile.objects.filter(user=self.away).update(away_until=timezone.now() - timedelta(minutes=1))
        data = self.client.get(reverse("task_recommendations", args=[self.task.pk])).json()
        self.assertEqual(data["away_volunteers"], [])

    def test_assign_without_confirmation_is_refused(self):
        response = self._assign(self.away)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["reason"], "volunteer_away")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
        self.assertIsNone(self.task.volunteer)

    def test_assign_with_confirmation_logs_override_and_notifies_urgently(self):
        with self.assertLogs("myapp.views", level="INFO") as logs:
            response = self._assign(self.away, confirm_away="1")
        self.assertTrue(response.json()["success"])
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.volunteer_id), ("active", self.away.pk))
        line = "\n".join(logs.output)
        self.assertIn("do-not-disturb override", line)
        self.assertIn(f"volunteer #{self.away.pk}", line)
        self.assertNotIn(self.away.username, line)
        note = Notification.objects.get(recipient=self.away, type__startswith="task_assigned")
        self.assertIn("urgent", note.title_key)

    def test_regular_volunteer_needs_no_confirmation(self):
        response = self._assign(self.available)
        self.assertTrue(response.json()["success"])
        note = Notification.objects.get(recipient=self.available, type__startswith="task_assigned")
        self.assertNotIn("urgent", note.title_key)

    def test_non_staff_cannot_assign(self):
        self.client.force_login(self.client_user)
        response = self._assign(self.away, confirm_away="1")
        self.assertNotEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_away_list_is_bounded_and_region_scoped(self):
        other = _make("d_away_sogd", is_volunteer=True)
        other.region = "sogd"
        other.save(update_fields=["region"])
        Profile.objects.filter(user=other).update(is_away=True)
        rows = dispatch.away_volunteers_for(self.task)
        self.assertEqual([u.pk for u, _ in rows], [self.away.pk])
        self.assertLessEqual(len(rows), dispatch.AWAY_LIST_LIMIT)

    def test_dispatch_card_has_away_block(self):
        page = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertContains(page, 'id="recommendAway"')
        self.assertContains(page, "confirm_away")
