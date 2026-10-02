"""Help-request cancellation (Feature Pack v2, owner decision §9.4):
myapp/services/cancellation.py + the task_cancel view."""

from unittest import mock

from django.core import mail
from django.test import TestCase
from django.urls import reverse

from accounts.models import Profile, Users
from myapp.models import HelpRequest, Notification
from myapp.services import cancellation
from myapp.services.cancellation import FORBIDDEN, NOT_OPEN, OK, REASON_REQUIRED, cancel_request

PASSWORD = "Cancel-Tests-2026!"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


class CancellationTestBase(TestCase):
    def setUp(self):
        self.admin = _make("c_admin", is_superuser=True, is_staff=True)
        self.curator = _make("c_curator", is_curator=True)
        self.volunteer = _make("c_volunteer", is_volunteer=True)
        self.client_user = _make("c_client", is_client=True)
        self.other_client = _make("c_client2", is_client=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="bread", address="a",
            phone="+992901234567", region="dushanbe",
        )

    def _activate(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(status="active", volunteer=self.volunteer)
        self.task.refresh_from_db()


class CancelServiceTests(CancellationTestBase):
    def test_client_cancels_own_pending_request_without_reason(self):
        outcome = cancel_request(self.task, self.client_user)
        self.assertEqual((outcome.ok, outcome.code), (True, OK))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "cancelled")
        self.assertEqual(self.task.cancelled_by, self.client_user)
        self.assertIsNotNone(self.task.cancelled_at)
        self.assertEqual(self.task.cancel_reason, "")

    def test_client_cannot_cancel_active_request(self):
        self._activate()
        outcome = cancel_request(self.task, self.client_user, "changed my mind")
        self.assertEqual(outcome.code, FORBIDDEN)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "active")

    def test_other_client_and_volunteer_cannot_cancel(self):
        for user in (self.other_client, self.volunteer):
            with self.subTest(user=user.username):
                self.assertEqual(cancel_request(self.task, user, "x").code, FORBIDDEN)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_staff_needs_a_reason(self):
        for user in (self.curator, self.admin):
            with self.subTest(user=user.username):
                self.assertEqual(cancel_request(self.task, user, "   ").code, REASON_REQUIRED)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_staff_cancels_active_task_with_reason(self):
        self._activate()
        outcome = cancel_request(self.task, self.curator, "Client hospitalised")
        self.assertTrue(outcome.ok)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "cancelled")
        self.assertEqual(self.task.cancel_reason, "Client hospitalised")

    def test_reason_is_trimmed_and_capped(self):
        cancel_request(self.task, self.admin, "  " + "x" * 900 + "  ")
        self.task.refresh_from_db()
        self.assertEqual(len(self.task.cancel_reason), cancellation.CANCEL_REASON_MAX_LENGTH)

    def test_closed_tasks_cannot_be_cancelled(self):
        for status in ("completed", "cancelled"):
            HelpRequest.objects.filter(pk=self.task.pk).update(status=status)
            self.task.refresh_from_db()
            with self.subTest(status=status):
                self.assertEqual(cancel_request(self.task, self.admin, "r").code, NOT_OPEN)
                self.assertEqual(cancel_request(self.task, self.client_user).code, NOT_OPEN)

    def test_race_with_accept_has_one_winner(self):
        # The task object is stale (still "pending" in memory) but a volunteer
        # accepted it in the meantime: the conditional UPDATE must not match.
        HelpRequest.objects.filter(pk=self.task.pk).update(status="active", volunteer=self.volunteer)
        outcome = cancel_request(self.task, self.client_user)
        self.assertEqual(outcome.code, NOT_OPEN)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "active")

    def test_second_cancel_is_a_no_op(self):
        self.assertTrue(cancel_request(self.task, self.client_user).ok)
        stale = HelpRequest.objects.get(pk=self.task.pk)
        stale.status = "pending"  # simulate a double submit holding an old copy
        with mock.patch("myapp.services.cancellation._after_cancel") as after:
            self.assertEqual(cancel_request(stale, self.client_user).code, NOT_OPEN)
        after.assert_not_called()

    def test_staff_cancel_notifies_client_and_volunteer_not_actor(self):
        self._activate()
        cancel_request(self.task, self.curator, "Duplicate request")
        recipients = set(
            Notification.objects.filter(type="task_cancelled").values_list("recipient_id", flat=True)
        )
        self.assertEqual(recipients, {self.client_user.pk, self.volunteer.pk})

    def test_client_cancel_notifies_staff(self):
        cancel_request(self.task, self.client_user)
        recipients = set(
            Notification.objects.filter(type="task_cancelled").values_list("recipient_id", flat=True)
        )
        self.assertEqual(recipients, {self.admin.pk, self.curator.pk})

    def test_cancel_publishes_task_updated(self):
        with mock.patch("myapp.realtime.publish.send_to_task") as send:
            cancel_request(self.task, self.client_user)
        send.assert_called_once()  # plus the staff CRM copy via send_to_staff
        _, event_type, payload = send.call_args.args
        self.assertEqual(event_type, "task.updated")
        self.assertEqual(payload, {"task_id": self.task.pk, "status": "cancelled", "work_stage": "assigned"})

    def test_cancel_frees_the_volunteer(self):
        self._activate()
        Profile.objects.filter(user=self.volunteer).update(availability_status="busy")
        with mock.patch("myapp.services.presence.on_task_change") as hook:
            cancel_request(self.task, self.admin, "Volunteer sick")
        hook.assert_called_once()
        self.assertEqual(hook.call_args.args[0].pk, self.task.pk)

    def test_log_line_has_ids_only(self):
        self._activate()
        with self.assertLogs("myapp.services.cancellation", level="INFO") as logs:
            cancel_request(self.task, self.curator, "Secret medical detail")
        line = "\n".join(logs.output)
        self.assertIn(f"task #{self.task.pk}", line)
        self.assertNotIn("Secret medical detail", line)
        self.assertNotIn(self.client_user.username, line)

    def test_can_cancel_matrix(self):
        self.assertTrue(cancellation.can_cancel(self.client_user, self.task))
        self.assertTrue(cancellation.can_cancel(self.curator, self.task))
        self.assertFalse(cancellation.can_cancel(self.volunteer, self.task))
        self.assertFalse(cancellation.can_cancel(self.other_client, self.task))
        self._activate()
        self.assertFalse(cancellation.can_cancel(self.client_user, self.task))
        self.assertTrue(cancellation.can_cancel(self.admin, self.task))


class CancelViewTests(CancellationTestBase):
    def _url(self):
        return reverse("task_cancel", args=[self.task.pk])

    def test_get_is_not_allowed(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(self._url()).status_code, 405)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_client_cancels_via_post(self):
        self.client.force_login(self.client_user)
        response = self.client.post(self._url(), {"reason": "No longer needed"})
        self.assertEqual(response.status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "cancelled")
        self.assertEqual(self.task.cancel_reason, "No longer needed")

    def test_staff_without_reason_is_refused(self):
        self.client.force_login(self.curator)
        self.client.post(self._url(), {"reason": ""})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_volunteer_cannot_cancel(self):
        self._activate()
        self.client.force_login(self.volunteer)
        self.client.post(self._url(), {"reason": "x"})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "active")

    def test_other_client_gets_no_access(self):
        self.client.force_login(self.other_client)
        self.client.post(self._url(), {"reason": "x"})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")

    def test_detail_shows_cancel_form_only_when_allowed(self):
        self.client.force_login(self.client_user)
        detail = reverse("task_detail", args=[self.task.pk])
        self.assertContains(self.client.get(detail), self._url())
        self._activate()
        self.assertNotContains(self.client.get(detail), self._url())
        self.client.force_login(self.curator)
        self.assertContains(self.client.get(detail), self._url())

    def test_staff_cancel_emails_the_parties(self):
        self._activate()
        mail.outbox.clear()
        self.client.force_login(self.admin)
        self.client.post(self._url(), {"reason": "Duplicate"})
        to = sorted(addr for m in mail.outbox for addr in m.to)
        self.assertIn(self.client_user.email, to)
        self.assertIn(self.volunteer.email, to)
        self.assertNotIn(self.admin.email, to)
        for message in mail.outbox:
            self.assertEqual(len(message.to), 1)  # never a shared To: line
