"""'Обновить моё местоположение' + stale-location reminders (owner decision
2026-10-02): matching keeps using the saved profile point."""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.services import profile_location

PASSWORD = "Ploc-Tests-2026!"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


class ProfileLocationViewTests(TestCase):
    def setUp(self):
        self.volunteer = _make("ploc_vol", is_volunteer=True)
        self.url = reverse("profile_location")

    def test_volunteer_saves_validated_point(self):
        self.client.force_login(self.volunteer)
        response = self.client.post(self.url, {"lat": "38.559800", "lng": "68.787000", "accuracy": "25"})
        self.assertTrue(response.json()["success"])
        profile = Profile.objects.get(user=self.volunteer)
        self.assertAlmostEqual(float(profile.latitude), 38.5598, places=4)
        self.assertAlmostEqual(float(profile.longitude), 68.787, places=4)
        self.assertIsNotNone(profile.location_updated_at)

    def test_saving_resets_the_reminder_claim(self):
        Profile.objects.filter(user=self.volunteer).update(location_reminded_at=timezone.now())
        self.client.force_login(self.volunteer)
        self.client.post(self.url, {"lat": "38.5", "lng": "68.7"})
        self.assertIsNone(Profile.objects.get(user=self.volunteer).location_reminded_at)

    def test_invalid_and_inaccurate_points_are_rejected(self):
        self.client.force_login(self.volunteer)
        for data in ({"lat": "800", "lng": "68"}, {"lat": "abc", "lng": "1"}, {"lat": "38.5"}):
            with self.subTest(data=data):
                self.assertEqual(self.client.post(self.url, data).status_code, 400)
        response = self.client.post(self.url, {"lat": "38.5", "lng": "68.7", "accuracy": "5000"})
        self.assertEqual(response.json()["error"], "inaccurate")
        self.assertIsNone(Profile.objects.get(user=self.volunteer).latitude)

    def test_only_volunteers_and_only_post(self):
        client_user = _make("ploc_cli", is_client=True)
        self.client.force_login(client_user)
        self.assertEqual(self.client.post(self.url, {"lat": "38.5", "lng": "68.7"}).status_code, 403)
        self.client.force_login(self.volunteer)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"lat": "38.5", "lng": "68.7"}).status_code, 302)

    def test_dashboard_card_shows_state(self):
        self.client.force_login(self.volunteer)
        page = self.client.get(reverse("profile"))
        self.assertContains(page, 'id="profileLocationCard"')
        self.assertContains(page, "ploc.never")
        Profile.objects.filter(user=self.volunteer).update(
            latitude=38.5, longitude=68.7, location_updated_at=timezone.now() - timedelta(days=40)
        )
        page = self.client.get(reverse("profile"))
        self.assertContains(page, "is-stale")
        self.assertContains(page, "ploc.stale")
        Profile.objects.filter(user=self.volunteer).update(location_updated_at=timezone.now())
        self.assertNotContains(self.client.get(reverse("profile")), "is-stale")

    def test_clients_have_no_card(self):
        client_user = _make("ploc_cli2", is_client=True)
        self.client.force_login(client_user)
        self.assertNotContains(self.client.get(reverse("profile")), "profileLocationCard")


class StaleLocationReminderTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.never = _make("ploc_never", is_volunteer=True)
        self.old = _make("ploc_old", is_volunteer=True)
        self.fresh = _make("ploc_fresh", is_volunteer=True)
        self.away_client = _make("ploc_client", is_client=True)
        Profile.objects.filter(user=self.old).update(
            latitude=38.5, longitude=68.7, location_updated_at=now - timedelta(days=45))
        Profile.objects.filter(user=self.fresh).update(
            latitude=38.5, longitude=68.7, location_updated_at=now - timedelta(days=2))

    def test_reminds_missing_and_old_points_once(self):
        with mock.patch("myapp.services.notify.notify") as notify:
            self.assertEqual(profile_location.remind_stale(), 2)
            self.assertEqual(profile_location.remind_stale(), 0)  # claimed — not again
        reminded = {call.args[0][0].pk for call in notify.call_args_list}
        self.assertEqual(reminded, {self.never.pk, self.old.pk})
        self.assertEqual({call.args[1] for call in notify.call_args_list}, {"location_stale"})

    def test_reminds_again_after_the_period(self):
        with mock.patch("myapp.services.notify.notify"):
            profile_location.remind_stale()
        Profile.objects.filter(user=self.old).update(location_reminded_at=timezone.now() - timedelta(days=31))
        with mock.patch("myapp.services.notify.notify") as notify:
            self.assertEqual(profile_location.remind_stale(), 1)
        self.assertEqual(notify.call_args.args[0][0].pk, self.old.pk)

    def test_dry_run_and_command(self):
        with mock.patch("myapp.services.notify.notify") as notify:
            out = StringIO()
            call_command("remind_stale_locations", "--dry-run", stdout=out)
            self.assertIn("2 volunteer(s) would be reminded", out.getvalue())
            notify.assert_not_called()
        self.assertFalse(Profile.objects.filter(location_reminded_at__isnull=False).exists())

    def test_real_notification_type_exists(self):
        profile_location.remind_stale()
        from myapp.models import Notification

        self.assertEqual(
            set(Notification.objects.filter(type="location_stale").values_list("recipient_id", flat=True)),
            {self.never.pk, self.old.pk},
        )
