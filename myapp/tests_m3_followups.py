"""Milestone-3 follow-ups: single-quoted data-i18n-args are pre-rendered on the
server too, and the CRM reviews list filters by volunteer."""

from django.test import TestCase
from django.urls import reverse

from accounts.models import Users
from myapp.models import HelpRequest
from myapp.services import feedback
from server.i18n_prerender import render_html

PASSWORD = "M3-Followups-2026!"


class PrerenderArgsQuotingTests(TestCase):
    def test_single_and_double_quoted_args_are_interpolated(self):
        for attr in ("data-i18n-args='{\"date\": \"12.10.2026\"}'",
                     'data-i18n-args="{&quot;date&quot;: &quot;12.10.2026&quot;}"'):
            with self.subTest(attr=attr):
                html = f'<html><body><span data-i18n="availability.away_until" {attr}>x</span></body></html>'
                out = render_html(html, "ru")
                self.assertIn("до 12.10.2026", out)


class CrmReviewsVolunteerFilterTests(TestCase):
    def setUp(self):
        self.curator = Users.objects.create_user(username="m3f_cur", email="c@example.com", password=PASSWORD, is_curator=True)
        self.client_user = Users.objects.create_user(username="m3f_cli", email="k@example.com", password=PASSWORD, is_client=True)
        self.vol_a = Users.objects.create_user(username="m3f_a", email="a@example.com", password=PASSWORD, is_volunteer=True)
        self.vol_b = Users.objects.create_user(username="m3f_b", email="b@example.com", password=PASSWORD, is_volunteer=True)
        for vol in (self.vol_a, self.vol_b):
            task = HelpRequest.objects.create(
                client=self.client_user, volunteer=vol, status="completed", help_type="grocery",
                description="d", address="a", phone="+992901234567", region="dushanbe",
            )
            feedback.submit_review(self.client_user, task, stars=5, comment=f"for {vol.username}")

    def test_filter_by_volunteer(self):
        self.client.force_login(self.curator)
        page = self.client.get(reverse("crm_reviews") + f"?volunteer={self.vol_a.pk}")
        self.assertContains(page, "for m3f_a")
        self.assertNotContains(page, "for m3f_b")
        self.assertContains(self.client.get(reverse("crm_reviews") + "?volunteer=abc"), "for m3f_b")
