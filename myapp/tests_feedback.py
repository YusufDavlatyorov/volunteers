"""Client reviews (Feature Pack v2, feature 9): myapp/services/feedback.py,
myapp/views_feedback.py, the reviews template tags, the public shapes and the
Django admin."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db.models.query import QuerySet
from django.template import Context, Template
from django.test import RequestFactory, TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import HelpRequest, Notification, TaskReview
from myapp.services import dashboard, feedback
from myapp.services import notify as notify_service

PASSWORD = "Review-Tests-2026!"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


def _ensure_review_types():
    """review_low / review_request are registered by the realtime engineer; if
    this branch doesn't have them yet, register equivalent types for the test so
    the real notify() path still runs end to end."""
    extra = {}
    for code, roles in (("review_low", notify_service.STAFF), ("review_request", ("client",)),
                        ("review_received", ("volunteer",))):
        if code not in notify_service.NOTIFICATION_TYPES:
            extra[code] = notify_service.NotificationType(
                code=code, roles=tuple(roles), defaults=frozenset()
            )
    return mock.patch.dict(notify_service.NOTIFICATION_TYPES, extra)


class FeedbackTestBase(TestCase):
    def setUp(self):
        cache.clear()
        patcher = _ensure_review_types()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.admin = _make("rv_admin", is_superuser=True, is_staff=True)
        self.curator = _make("rv_curator", is_curator=True)
        self.volunteer = _make("rv_volunteer", is_volunteer=True)
        self.other_volunteer = _make("rv_volunteer2", is_volunteer=True)
        self.client_user = _make("rv_client", is_client=True)
        self.other_client = _make("rv_client2", is_client=True)
        Profile.objects.filter(user=self.volunteer).update(full_name="Malika Volunteer")
        self.task = self._task()

    def _task(self, status="completed", volunteer="default", client=None):
        volunteer = self.volunteer if volunteer == "default" else volunteer
        task = HelpRequest.objects.create(
            client=client or self.client_user, help_type="grocery", description="bread", address="a",
            phone="+992901234567", region="dushanbe",
        )
        HelpRequest.objects.filter(pk=task.pk).update(
            status=status, volunteer=volunteer,
            completed_at=timezone.now() if status == "completed" else None,
        )
        task.refresh_from_db()
        return task

    def _review(self, stars=5, comment="Thank you", tags=("polite",), task=None, client=None):
        outcome = feedback.submit_review(
            client or self.client_user, task or self.task, stars=stars, comment=comment, tags=tags
        )
        self.assertTrue(outcome.ok, outcome.code)
        return outcome.review

    def _age(self, review, hours):
        TaskReview.objects.filter(pk=review.pk).update(created_at=timezone.now() - timedelta(hours=hours))
        review.refresh_from_db()

    def _ntf(self, type_, user=None):
        qs = Notification.objects.filter(type=type_)
        return qs.filter(recipient=user) if user else qs


# --------------------------------------------------------------------------- #
# who can review
# --------------------------------------------------------------------------- #

class CanReviewTests(FeedbackTestBase):
    def test_client_of_own_completed_task_can_review(self):
        self.assertTrue(feedback.can_review(self.client_user, self.task))

    def test_not_on_pending_active_cancelled_or_unassigned(self):
        for status in ("pending", "active", "cancelled"):
            with self.subTest(status=status):
                task = self._task(status=status)
                self.assertFalse(feedback.can_review(self.client_user, task))
                self.assertEqual(
                    feedback.submit_review(self.client_user, task, stars=5).code, feedback.FORBIDDEN
                )
        no_volunteer = self._task(volunteer=None)
        self.assertFalse(feedback.can_review(self.client_user, no_volunteer))
        self.assertEqual(TaskReview.objects.count(), 0)

    def test_volunteer_staff_other_client_and_anonymous_cannot_review(self):
        for user in (self.volunteer, self.curator, self.admin, self.other_client, AnonymousUser()):
            with self.subTest(user=str(user)):
                self.assertFalse(feedback.can_review(user, self.task))
                self.assertEqual(feedback.submit_review(user, self.task, stars=5).code, feedback.FORBIDDEN)
        self.assertEqual(TaskReview.objects.count(), 0)

    def test_inactive_client_cannot_review(self):
        Users.objects.filter(pk=self.client_user.pk).update(is_active=False)
        self.client_user.refresh_from_db()
        self.assertFalse(feedback.can_review(self.client_user, self.task))

    def test_one_review_per_task_second_submit_is_an_edit(self):
        first = self._review(stars=5)
        self.assertFalse(feedback.can_review(self.client_user, self.task))
        outcome = feedback.submit_review(self.client_user, self.task, stars=3, comment="ok")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.created)
        self.assertEqual(outcome.review.pk, first.pk)
        self.assertEqual(TaskReview.objects.filter(task=self.task).count(), 1)
        self.assertEqual(TaskReview.objects.get().stars, 3)

    def test_lost_create_race_is_applied_as_an_edit_of_the_winner(self):
        winner = TaskReview.objects.create(
            task=self.task, client=self.client_user, volunteer=self.volunteer, stars=5
        )
        real_first = QuerySet.first
        calls = {"n": 0}

        def first_misses_once(qs):
            calls["n"] += 1
            return None if calls["n"] == 1 else real_first(qs)

        with mock.patch.object(QuerySet, "first", first_misses_once):
            outcome = feedback.submit_review(self.client_user, self.task, stars=4, comment="late")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.created)
        self.assertEqual(outcome.review.pk, winner.pk)
        self.assertEqual(TaskReview.objects.count(), 1)
        self.assertEqual(TaskReview.objects.get().stars, 4)
        self.assertEqual(self._ntf("review_received").count(), 0)  # an edit never re-announces


class EditWindowTests(FeedbackTestBase):
    def test_author_can_edit_within_window(self):
        review = self._review(stars=5)
        self._age(review, 23)
        self.assertTrue(feedback.can_edit(self.client_user, review))
        outcome = feedback.submit_review(self.client_user, self.task, stars=4, comment="edited")
        self.assertEqual(outcome.code, feedback.OK)
        review.refresh_from_db()
        self.assertEqual((review.stars, review.comment), (4, "edited"))

    def test_edit_closed_after_window(self):
        review = self._review(stars=5, comment="original")
        self._age(review, 25)
        self.assertFalse(feedback.can_edit(self.client_user, review))
        outcome = feedback.submit_review(self.client_user, self.task, stars=1, comment="changed")
        self.assertEqual(outcome.code, feedback.EDIT_CLOSED)
        review.refresh_from_db()
        self.assertEqual((review.stars, review.comment), (5, "original"))

    def test_window_uses_frozen_now(self):
        review = self._review()
        later = review.created_at + timedelta(hours=24, seconds=1)
        with mock.patch("myapp.services.feedback.timezone.now", return_value=later):
            self.assertFalse(feedback.can_edit(self.client_user, review))
        self.assertTrue(feedback.can_edit(self.client_user, review, now=review.created_at + timedelta(hours=24)))

    @mock.patch("django.conf.settings.REVIEW_EDIT_HOURS", 2, create=True)
    def test_window_follows_setting(self):
        review = self._review()
        self._age(review, 3)
        self.assertFalse(feedback.can_edit(self.client_user, review))

    def test_non_author_cannot_edit(self):
        review = self._review()
        for user in (self.volunteer, self.curator, self.admin, self.other_client):
            with self.subTest(user=user.username):
                self.assertFalse(feedback.can_edit(user, review))

    def test_unchanged_resubmit_is_a_no_op(self):
        self._review(stars=5, comment="same", tags=["polite"])
        outcome = feedback.submit_review(self.client_user, self.task, stars=5, comment="same", tags=["polite"])
        self.assertEqual((outcome.ok, outcome.code), (True, feedback.NOT_CHANGED))


class ValidationTests(FeedbackTestBase):
    def test_stars_must_be_1_to_5(self):
        for bad in (0, 6, -1, "abc", None, "", True, 3.5, "4.5"):
            with self.subTest(stars=bad):
                self.assertEqual(
                    feedback.submit_review(self.client_user, self.task, stars=bad).code, feedback.INVALID_STARS
                )
        self.assertEqual(TaskReview.objects.count(), 0)
        self.assertTrue(feedback.submit_review(self.client_user, self.task, stars="4").ok)
        self.assertEqual(TaskReview.objects.get().stars, 4)

    def test_comment_length_and_stripping(self):
        self.assertEqual(
            feedback.submit_review(self.client_user, self.task, stars=5, comment="x" * 1001).code,
            feedback.COMMENT_TOO_LONG,
        )
        review = feedback.submit_review(self.client_user, self.task, stars=5, comment="  " + "x" * 1000 + "\n ").review
        self.assertEqual(review.comment, "x" * 1000)

    def test_crlf_counts_as_one_character(self):
        text = "ab\r\n" * 250  # 1000 chars as the textarea counts them, 1000 after normalising
        outcome = feedback.submit_review(self.client_user, self.task, stars=5, comment=text)
        self.assertTrue(outcome.ok, outcome.code)
        self.assertNotIn("\r", outcome.review.comment)

    def test_tags_must_come_from_the_fixed_list_and_are_deduped(self):
        self.assertEqual(
            feedback.submit_review(self.client_user, self.task, stars=5, tags=["polite", "rude"]).code,
            feedback.INVALID_TAGS,
        )
        self.assertEqual(
            feedback.submit_review(self.client_user, self.task, stars=5, tags=[1]).code, feedback.INVALID_TAGS
        )
        review = feedback.submit_review(
            self.client_user, self.task, stars=5, tags=["punctual", "polite", "punctual"]
        ).review
        self.assertEqual(review.tags, ["punctual", "polite"])

    def test_single_string_tag_and_none(self):
        self.assertEqual(feedback.validate(5, None, "polite"), (feedback.OK, (5, "", ["polite"])))
        self.assertEqual(feedback.validate(5, "", None), (feedback.OK, (5, "", [])))


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #

class StatsTests(FeedbackTestBase):
    def _profile(self):
        return Profile.objects.get(user=self.volunteer)

    def test_average_and_count_recomputed(self):
        self._review(stars=5)
        second = self._task()
        self._review(stars=2, task=second)
        p = self._profile()
        self.assertEqual((str(p.review_avg), p.review_count), ("3.50", 2))

    def test_edit_recomputes(self):
        self._review(stars=5)
        feedback.submit_review(self.client_user, self.task, stars=3)
        p = self._profile()
        self.assertEqual((str(p.review_avg), p.review_count), ("3.00", 1))

    def test_rounding_to_two_decimals(self):
        for stars in (5, 4, 4):
            self._review(stars=stars, task=self._task())
        self.assertEqual(str(self._profile().review_avg), "4.33")

    def test_hide_and_unhide_recompute(self):
        good = self._review(stars=5)
        bad = self._review(stars=1, task=self._task())
        self.assertEqual(self._profile().review_count, 2)

        outcome = feedback.hide(self.curator, bad)
        self.assertEqual(outcome.code, feedback.OK)
        bad.refresh_from_db()
        self.assertIsNotNone(bad.hidden_at)
        self.assertEqual(bad.hidden_by, self.curator)
        p = self._profile()
        self.assertEqual((str(p.review_avg), p.review_count), ("5.00", 1))

        self.assertEqual(feedback.hide(self.curator, bad).code, feedback.NOT_CHANGED)

        self.assertEqual(feedback.unhide(self.admin, bad).code, feedback.OK)
        p = self._profile()
        self.assertEqual((str(p.review_avg), p.review_count), ("3.00", 2))
        self.assertEqual(feedback.unhide(self.admin, bad).code, feedback.NOT_CHANGED)

        feedback.hide(self.admin, good)
        feedback.hide(self.admin, bad)
        p = self._profile()
        self.assertEqual((p.review_avg, p.review_count), (None, 0))

    def test_non_staff_cannot_hide(self):
        review = self._review(stars=1)
        for user in (self.volunteer, self.client_user, self.other_client):
            with self.subTest(user=user.username):
                self.assertEqual(feedback.hide(user, review).code, feedback.FORBIDDEN)
        review.refresh_from_db()
        self.assertIsNone(review.hidden_at)
        self.assertEqual(self._profile().review_count, 1)

    def test_recompute_is_one_aggregate_and_one_update(self):
        self._review()
        with self.assertNumQueries(2):
            feedback.recompute_volunteer_stats(self.volunteer.pk)

    def test_recompute_refreshes_a_loaded_profile(self):
        volunteer = Users.objects.select_related("profile").get(pk=self.volunteer.pk)
        self._review(stars=4)
        feedback.recompute_volunteer_stats(volunteer)
        self.assertEqual(volunteer.profile.review_count, 1)

    def test_matching_does_not_read_reviews(self):
        import inspect

        from myapp.services import matching

        self.assertNotIn("review", inspect.getsource(matching))


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #

class NotificationTests(FeedbackTestBase):
    def test_review_received_once_on_create_not_on_edit(self):
        self._review(stars=5)
        feedback.submit_review(self.client_user, self.task, stars=4, comment="edit")
        rows = self._ntf("review_received")
        self.assertEqual(rows.count(), 1)
        row = rows.get()
        self.assertEqual(row.recipient, self.volunteer)
        self.assertEqual(row.params.get("task_id"), self.task.pk)
        self.assertEqual(row.params.get("stars"), 5)
        self.assertNotIn("comment", row.params)

    def test_low_rating_alerts_each_staff_member_once(self):
        self._review(stars=2, comment="secret words")
        for staff in (self.admin, self.curator):
            self.assertEqual(self._ntf("review_low", staff).count(), 1)
        self.assertFalse(self._ntf("review_low", self.volunteer).exists())
        feedback.submit_review(self.client_user, self.task, stars=1)
        for staff in (self.admin, self.curator):
            self.assertEqual(self._ntf("review_low", staff).count(), 1)
        row = self._ntf("review_low", self.curator).get()
        self.assertNotIn("secret words", str(row.params))
        self.assertEqual(row.params.get("task_id"), self.task.pk)
        self.assertEqual(row.params.get("volunteer_name"), "Malika Volunteer")

    def test_good_rating_does_not_alert(self):
        self._review(stars=3)
        self.assertFalse(self._ntf("review_low").exists())

    def test_edit_down_alerts_once_over_the_reviews_life(self):
        self._review(stars=4)
        self.assertFalse(self._ntf("review_low").exists())
        feedback.submit_review(self.client_user, self.task, stars=2)
        self.assertEqual(self._ntf("review_low", self.curator).count(), 1)
        feedback.submit_review(self.client_user, self.task, stars=5)
        feedback.submit_review(self.client_user, self.task, stars=1)
        self.assertEqual(self._ntf("review_low", self.curator).count(), 1)

    def test_review_request_is_deduped_per_task(self):
        self.assertEqual(feedback.review_request(self.task), 1)
        feedback.review_request(self.task)
        rows = self._ntf("review_request", self.client_user)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.get().link, reverse("task_review", args=[self.task.pk]))

    def test_review_request_only_for_reviewable_tasks(self):
        for status in ("pending", "active", "cancelled"):
            with self.subTest(status=status):
                self.assertEqual(feedback.review_request(self._task(status=status)), 0)
        self.assertEqual(feedback.review_request(self._task(volunteer=None)), 0)
        self._review()
        self.assertEqual(feedback.review_request(self.task), 0)
        self.assertFalse(self._ntf("review_request").exists())

    def test_a_notify_failure_does_not_lose_the_review(self):
        with mock.patch("myapp.services.notify.notify", side_effect=RuntimeError("down")), \
                self.assertLogs("myapp.services.feedback", level="ERROR"):
            outcome = feedback.submit_review(self.client_user, self.task, stars=1)
        self.assertTrue(outcome.ok)
        self.assertEqual(TaskReview.objects.count(), 1)

    def test_logs_carry_ids_not_the_comment(self):
        with self.assertLogs("myapp.services.feedback", level="INFO") as logs:
            review = self._review(stars=5, comment="very private comment")
            feedback.hide(self.curator, review)
        joined = "\n".join(logs.output)
        self.assertIn(f"review={review.pk}", joined)
        self.assertNotIn("very private comment", joined)


# --------------------------------------------------------------------------- #
# visibility
# --------------------------------------------------------------------------- #

class VisibilityTests(FeedbackTestBase):
    def setUp(self):
        super().setUp()
        self.visible = self._review(stars=5, comment="Visible comment")
        self.hidden = self._review(stars=1, comment="Hidden comment", task=self._task())
        feedback.hide(self.curator, self.hidden)

    def test_reviews_for_matrix(self):
        own = feedback.reviews_for(self.volunteer, self.volunteer)
        self.assertEqual([r.pk for r in own], [self.visible.pk])
        for staff in (self.curator, self.admin):
            page = feedback.reviews_for(staff, self.volunteer)
            self.assertEqual({r.pk for r in page}, {self.visible.pk, self.hidden.pk})
        for viewer in (self.other_volunteer, self.client_user, self.other_client, AnonymousUser(), None):
            with self.subTest(viewer=str(viewer)):
                self.assertIsNone(feedback.reviews_for(viewer, self.volunteer))

    def test_public_summary_has_only_avg_and_count(self):
        volunteer = Users.objects.get(pk=self.volunteer.pk)
        self.assertEqual(feedback.public_summary(volunteer), {"avg": 5.0, "count": 1})
        self.assertEqual(feedback.public_summary(volunteer.profile), {"avg": 5.0, "count": 1})
        self.assertEqual(feedback.public_summary(self.other_volunteer), {"avg": None, "count": 0})
        self.assertEqual(feedback.public_summary(None), {"avg": None, "count": 0})

    def test_client_dashboard_volunteer_shape(self):
        volunteer = Users.objects.select_related("profile").get(pk=self.volunteer.pk)
        shape = dashboard._public_volunteer(volunteer)
        self.assertEqual(set(shape), {"name", "region", "review_avg", "review_count"})
        self.assertEqual((shape["review_avg"], shape["review_count"]), (5.0, 1))
        joined = str(shape)
        for secret in (self.volunteer.email, "+992", "Visible comment"):
            self.assertNotIn(secret, joined)

    def test_rating_page_shows_average_not_comments_or_contacts(self):
        response = self.client.get(reverse("rating"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "review-summary")
        self.assertContains(response, "5.0")
        for secret in ("Visible comment", "Hidden comment", self.volunteer.email):
            self.assertNotContains(response, secret)

    def test_ai_volunteer_brief_has_counts_not_comments(self):
        from myapp.services.ai import read_tools

        volunteer = Users.objects.select_related("profile").get(pk=self.volunteer.pk)
        brief = read_tools._volunteer_brief(volunteer)
        self.assertEqual((brief["review_avg"], brief["review_count"]), (5.0, 1))
        self.assertNotIn("Visible comment", str(brief))

    # --- the per-task page -------------------------------------------------

    def _get(self, user, task=None):
        self.client.logout()
        if user is not None:
            self.client.force_login(user)
        return self.client.get(reverse("task_review", args=[(task or self.task).pk]))

    def test_task_review_page_matrix(self):
        self.assertContains(self._get(self.client_user), "Visible comment")
        self.assertContains(self._get(self.volunteer), "Visible comment")
        self.assertContains(self._get(self.curator), "Visible comment")
        self.assertContains(self._get(self.admin), "Visible comment")
        for user in (self.other_volunteer, self.other_client):
            with self.subTest(user=user.username):
                response = self._get(user)
                self.assertRedirects(response, reverse("profile"), fetch_redirect_response=False)
        response = self._get(None)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_reviewed_volunteer_cannot_see_a_hidden_review(self):
        hidden_task = self.hidden.task
        response = self._get(self.volunteer, hidden_task)
        self.assertRedirects(response, reverse("profile"), fetch_redirect_response=False)
        response = self._get(self.curator, hidden_task)
        self.assertContains(response, "Hidden comment")
        self.assertContains(response, 'value="unhide"')

    def test_my_reviews_page(self):
        self.client.force_login(self.volunteer)
        response = self.client.get(reverse("my_reviews"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible comment")
        self.assertNotContains(response, "Hidden comment")
        for user in (self.client_user, self.curator):
            self.client.force_login(user)
            self.assertRedirects(self.client.get(reverse("my_reviews")), reverse("profile"),
                                 fetch_redirect_response=False)

    def test_crm_reviews_staff_only_with_filters(self):
        for user in (self.volunteer, self.client_user):
            self.client.force_login(user)
            self.assertRedirects(self.client.get(reverse("crm_reviews")), reverse("profile"),
                                 fetch_redirect_response=False)
        self.client.force_login(self.curator)
        response = self.client.get(reverse("crm_reviews"))
        self.assertContains(response, "Visible comment")
        self.assertContains(response, "Hidden comment")
        response = self.client.get(reverse("crm_reviews") + "?low=1")
        self.assertNotContains(response, "Visible comment")
        self.assertContains(response, "Hidden comment")
        response = self.client.get(reverse("crm_reviews") + "?hidden=0")
        self.assertContains(response, "Visible comment")
        self.assertNotContains(response, "Hidden comment")
        response = self.client.get(reverse("crm_reviews") + "?hidden=1")
        self.assertNotContains(response, "Visible comment")
        self.assertContains(response, "Hidden comment")


# --------------------------------------------------------------------------- #
# views
# --------------------------------------------------------------------------- #

class ReviewFormViewTests(FeedbackTestBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.client_user)
        self.url = reverse("task_review", args=[self.task.pk])

    def test_form_renders_accessible_stars_and_tags(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'type="radio" name="stars"', count=5)
        self.assertContains(response, 'name="tags"', count=len(feedback.REVIEW_TAGS))
        self.assertContains(response, 'data-i18n="review.title_new"')
        self.assertContains(response, "<legend")

    def test_post_creates_then_edits(self):
        response = self.client.post(self.url, {"stars": "4", "comment": "Great", "tags": ["polite", "careful"]})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        review = TaskReview.objects.get()
        self.assertEqual((review.stars, review.comment, review.tags), (4, "Great", ["polite", "careful"]))
        response = self.client.get(self.url)
        self.assertContains(response, 'data-i18n="review.title_edit"')
        self.assertContains(response, 'value="4" required checked')
        self.client.post(self.url, {"stars": "5", "comment": "Even better"})
        review.refresh_from_db()
        self.assertEqual((review.stars, review.tags), (5, []))
        self.assertEqual(TaskReview.objects.count(), 1)

    def test_invalid_post_rerenders_with_error_key(self):
        response = self.client.post(self.url, {"stars": "9", "comment": "keep me"})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'data-i18n="review.err.invalid_stars"', status_code=400)
        self.assertContains(response, "keep me", status_code=400)
        response = self.client.post(self.url, {"stars": "3", "tags": ["bogus"]})
        self.assertContains(response, 'data-i18n="review.err.invalid_tags"', status_code=400)
        self.assertEqual(TaskReview.objects.count(), 0)

    def test_after_the_window_the_page_is_read_only(self):
        review = self._review(stars=5, comment="original")
        self._age(review, 25)
        response = self.client.get(self.url)
        self.assertNotContains(response, 'name="stars"')
        self.assertContains(response, "review.edit_closed")
        response = self.client.post(self.url, {"stars": "1"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        review.refresh_from_db()
        self.assertEqual(review.stars, 5)

    def test_unfinished_task_redirects_to_task(self):
        task = self._task(status="active")
        response = self.client.get(reverse("task_review", args=[task.pk]))
        self.assertRedirects(response, reverse("task_detail", args=[task.pk]), fetch_redirect_response=False)

    def test_volunteer_and_staff_cannot_post_a_review(self):
        for user in (self.volunteer, self.curator, self.admin, self.other_client):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.client.post(self.url, {"stars": "1"})
        self.assertEqual(TaskReview.objects.count(), 0)

    def test_staff_cannot_post_over_an_existing_review(self):
        self._review(stars=5, comment="mine")
        self.client.force_login(self.curator)
        self.client.post(self.url, {"stars": "1", "comment": "forged"})
        review = TaskReview.objects.get()
        self.assertEqual((review.stars, review.comment), (5, "mine"))


class HideViewTests(FeedbackTestBase):
    def setUp(self):
        super().setUp()
        self.review = self._review(stars=1, comment="abusive")
        self.url = reverse("review_hide", args=[self.review.pk])

    def test_get_not_allowed(self):
        self.client.force_login(self.curator)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_non_staff_cannot_hide(self):
        for user in (self.volunteer, self.client_user):
            self.client.force_login(user)
            self.client.post(self.url)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.hidden_at)

    def test_staff_hide_and_unhide_with_safe_next(self):
        self.client.force_login(self.curator)
        next_url = reverse("crm_reviews") + "?low=1"
        response = self.client.post(self.url, {"action": "hide", "next": next_url})
        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        self.review.refresh_from_db()
        self.assertIsNotNone(self.review.hidden_at)
        self.assertEqual(Profile.objects.get(user=self.volunteer).review_count, 0)

        response = self.client.post(self.url, {"action": "unhide", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("crm_reviews"), fetch_redirect_response=False)
        self.review.refresh_from_db()
        self.assertIsNone(self.review.hidden_at)
        self.assertEqual(Profile.objects.get(user=self.volunteer).review_count, 1)


class QueryCountTests(FeedbackTestBase):
    def _count(self, user, url):
        self.client.force_login(user)
        self.client.get(url)  # warm session/caches
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return len(ctx)

    def _seed(self, n):
        for _ in range(n):
            client = _make(f"rv_c{TaskReview.objects.count()}_{_}", is_client=True)
            task = self._task(client=client)
            self._review(stars=2, comment="c", tags=["polite"], task=task, client=client)

    def test_staff_list_query_count_is_constant(self):
        self._seed(2)
        small = self._count(self.curator, reverse("crm_reviews"))
        self._seed(10)
        large = self._count(self.curator, reverse("crm_reviews"))
        self.assertEqual(small, large)

    def test_volunteer_page_query_count_is_constant(self):
        self._seed(2)
        small = self._count(self.volunteer, reverse("my_reviews"))
        self._seed(10)
        large = self._count(self.volunteer, reverse("my_reviews"))
        self.assertEqual(small, large)


class TemplateTagTests(FeedbackTestBase):
    def _render(self, source, **ctx):
        return Template("{% load reviews %}" + source).render(Context(ctx))

    def test_review_summary(self):
        self.assertIn("review.none_yet", self._render("{% review_summary v %}", v=self.volunteer))
        self.assertEqual(self._render("{% review_summary v compact=True %}", v=self.volunteer).strip(), "")
        self._review(stars=4, comment="private")
        volunteer = Users.objects.select_related("profile").get(pk=self.volunteer.pk)
        html = self._render("{% review_summary v %}", v=volunteer)
        self.assertIn("4.0", html)
        self.assertNotIn("private", html)

    def test_review_entry(self):
        request = RequestFactory().get("/")
        request.user = self.client_user
        source = "{% review_entry t as e %}{{ e.can_review }}|{{ e.can_edit }}|{{ e.review.pk }}|{{ e.url }}"
        self.assertEqual(
            self._render(source, t=self.task, request=request),
            f"True|False||{reverse('task_review', args=[self.task.pk])}",
        )
        review = self._review()
        self.assertTrue(self._render(source, t=self.task, request=request).startswith(f"False|True|{review.pk}|"))
        request.user = self.other_volunteer
        self.assertTrue(self._render(source, t=self.task, request=request).startswith("False|False||"))


class AdminTests(FeedbackTestBase):
    def setUp(self):
        super().setUp()
        self.review = self._review(stars=5, comment="original")
        self.client.force_login(self.admin)

    def test_admin_cannot_edit_add_or_delete(self):
        change_url = reverse("admin:myapp_taskreview_change", args=[self.review.pk])
        self.assertEqual(self.client.get(change_url).status_code, 200)
        self.client.post(change_url, {"stars": "1", "comment": "forged", "tags": "[]"})
        self.review.refresh_from_db()
        self.assertEqual((self.review.stars, self.review.comment), (5, "original"))
        self.assertEqual(self.client.get(reverse("admin:myapp_taskreview_add")).status_code, 403)
        delete_url = reverse("admin:myapp_taskreview_delete", args=[self.review.pk])
        self.assertEqual(self.client.post(delete_url, {"post": "yes"}).status_code, 403)
        self.assertTrue(TaskReview.objects.filter(pk=self.review.pk).exists())

    def test_admin_hide_action_goes_through_the_service(self):
        changelist = reverse("admin:myapp_taskreview_changelist")
        self.client.post(changelist, {"action": "hide_reviews", "_selected_action": [self.review.pk]})
        self.review.refresh_from_db()
        self.assertIsNotNone(self.review.hidden_at)
        self.assertEqual(Profile.objects.get(user=self.volunteer).review_count, 0)
        self.client.post(changelist, {"action": "unhide_reviews", "_selected_action": [self.review.pk]})
        self.review.refresh_from_db()
        self.assertIsNone(self.review.hidden_at)
        self.assertEqual(Profile.objects.get(user=self.volunteer).review_count, 1)
