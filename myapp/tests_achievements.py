"""Volunteer achievements (Feature Pack v2, milestone 4, feature 6).

Locks the owner's "no race" rules: milestones without deadlines, quality from
visible reviews only, low-rated tasks don't count, nothing revoked
automatically, volunteers only, idempotent awards, and — like reviews —
achievements never influence volunteer matching.
"""

import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import (
    Achievement,
    Donation,
    HelpRequest,
    Notification,
    PetReport,
    TaskReview,
    UserAchievement,
)
from myapp.services import achievements, feedback, matching

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "Ach-Tests-2026!"
HELP_TYPES = ["grocery", "medical", "transport", "household", "emotional", "documents", "other"]


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class AchievementBase(TestCase):
    def setUp(self):
        cache.clear()
        self.vol = _make("ach_vol", is_volunteer=True)
        self.client_user = _make("ach_client", is_client=True)
        self.curator = _make("ach_curator", is_curator=True)
        self.admin = _make("ach_admin", is_superuser=True, is_staff=True)

    def done(self, n=1, help_type="grocery", days_ago=1, volunteer=None):
        """n completed tasks, completed `days_ago` days ago."""
        tasks = []
        for i in range(n):
            task = HelpRequest.objects.create(
                client=self.client_user, help_type=help_type, description=f"t{i}", address="a",
                phone="+992901234567", region="dushanbe",
            )
            when = timezone.now() - timedelta(days=days_ago)
            HelpRequest.objects.filter(pk=task.pk).update(
                status="completed", volunteer=volunteer or self.vol, accepted_at=when - timedelta(hours=2),
                completed_at=when,
            )
            task.refresh_from_db()
            tasks.append(task)
        return tasks

    def review(self, task, stars=5, tags=("polite",)):
        outcome = feedback.submit_review(self.client_user, task, stars=stars, comment="", tags=list(tags))
        self.assertTrue(outcome.ok, outcome.code)
        return outcome.review

    def codes(self, user=None):
        return set(UserAchievement.objects.filter(user=user or self.vol).values_list("achievement__code", flat=True))


class StarterSetTests(AchievementBase):
    def test_owner_approved_set_is_seeded(self):
        codes = set(Achievement.objects.values_list("code", flat=True))
        self.assertEqual(codes, {
            "first_help", "tasks_10", "tasks_50", "tasks_100", "kind_words", "trusted", "steady_helper",
            "year_together", "versatile", "donation_courier", "pet_friend", "mentor",
        })
        self.assertFalse(Achievement.objects.get(code="mentor").is_active)  # community bot, milestone 5

    def test_no_race_badges_exist(self):
        """No month-top, daily streak, night-shift, SOS or points badge."""
        codes = " ".join(Achievement.objects.values_list("code", flat=True))
        for banned in ("hero", "streak", "night", "early", "sos", "emergency", "legend", "points", "top"):
            self.assertNotIn(banned, codes)

    def test_every_code_has_a_rule_a_badge_and_texts(self):
        from pathlib import Path

        from django.conf import settings

        from myapp.services import notify_texts

        for a in Achievement.objects.all():
            with self.subTest(code=a.code):
                self.assertIn(a.code, achievements.COUNTER_FOR)
                self.assertTrue((Path(settings.BASE_DIR) / "static" / a.icon_path).exists())
                for lang in ("ru", "tj", "en"):
                    self.assertIn(a.code, notify_texts.LABELS["achievement"][lang])
                    self.assertTrue(achievements.DESCRIPTIONS[a.code][lang])

    def test_badge_svgs_are_original_and_safe(self):
        from pathlib import Path
        from xml.dom import minidom

        from django.conf import settings

        for svg in (Path(settings.BASE_DIR) / "static" / "images" / "badges").glob("*.svg"):
            with self.subTest(svg=svg.name):
                text = svg.read_text(encoding="utf-8")
                minidom.parseString(text)  # well-formed
                for forbidden in ("<script", "href=\"http", "xlink:href", "<image", "onload"):
                    self.assertNotIn(forbidden, text)


class RuleTests(AchievementBase):
    def test_first_help_after_one_completed_task(self):
        self.assertEqual(achievements.evaluate(self.vol), [])
        self.done(1)
        awarded = achievements.evaluate(self.vol)
        self.assertEqual([row.achievement.code for row in awarded], ["first_help"])
        self.assertEqual(awarded[0].progress_snapshot, 1)

    def test_milestones_have_no_deadline(self):
        self.done(4, days_ago=400)
        self.done(6, days_ago=2)
        achievements.evaluate(self.vol)
        self.assertIn("tasks_10", self.codes())

    def test_a_visible_low_review_takes_the_task_out_of_the_count(self):
        tasks = self.done(10)
        self.review(tasks[0], stars=2, tags=())
        self.assertEqual(achievements.counters(self.vol)["tasks"], 9)
        achievements.evaluate(self.vol)
        self.assertNotIn("tasks_10", self.codes())
        # hidden low review: counts again (hidden reviews never count)
        feedback.hide(self.curator, TaskReview.objects.get(task=tasks[0]))
        self.assertEqual(achievements.counters(self.vol)["tasks"], 10)

    def test_kind_words_counts_visible_reviews_with_quality_tags(self):
        tasks = self.done(6)
        for task in tasks[:4]:
            self.review(task, stars=5, tags=["polite"])
        self.review(tasks[4], stars=5, tags=["punctual"])  # not a quality tag for this badge
        self.assertNotIn("kind_words", self.codes())
        self.review(tasks[5], stars=4, tags=["above_and_beyond"])
        self.assertIn("kind_words", self.codes())  # awarded from the review hook

    def test_hidden_reviews_never_count_for_quality(self):
        tasks = self.done(5)
        reviews = [self.review(t, stars=5, tags=["careful"]) for t in tasks[:4]]
        feedback.hide(self.curator, reviews[0])
        self.review(tasks[4], stars=5, tags=["careful"])
        self.assertEqual(achievements.counters(self.vol)["kind_reviews"], 4)
        self.assertNotIn("kind_words", self.codes())

    def test_trusted_needs_ten_reviews_and_a_high_average(self):
        tasks = self.done(11)
        for task in tasks[:9]:
            self.review(task, stars=5, tags=())
        self.review(tasks[9], stars=3, tags=())  # avg (45+3)/10 = 4.8
        self.assertIn("trusted", self.codes())

    def test_trusted_is_not_met_below_the_average(self):
        tasks = self.done(10)
        for i, task in enumerate(tasks):
            self.review(task, stars=4 if i % 2 else 5, tags=())  # avg 4.5 → met
        self.assertIn("trusted", self.codes())
        other = _make("ach_vol2", is_volunteer=True)
        for i, task in enumerate(self.done(10, volunteer=other)):
            self.review(task, stars=4, tags=())  # avg 4.0
        self.assertNotIn("trusted", self.codes(other))

    def test_steady_counts_months_not_days(self):
        self.done(5, days_ago=1)  # five tasks, one month
        achievements.evaluate(self.vol)
        self.assertNotIn("steady_helper", self.codes())
        self.done(1, days_ago=40)
        self.done(1, days_ago=75)
        achievements.evaluate(self.vol)
        self.assertIn("steady_helper", self.codes())

    def test_year_together_uses_the_last_twelve_months(self):
        for months_ago in (14, 15, 16, 17, 18, 19):
            self.done(1, days_ago=months_ago * 30)
        numbers = achievements.counters(self.vol)
        self.assertEqual(numbers["months_year"], 0)
        self.assertGreaterEqual(numbers["months"], 6)

    def test_versatile_after_four_kinds_of_help(self):
        for help_type in HELP_TYPES[:3]:
            self.done(1, help_type=help_type)
        achievements.evaluate(self.vol)
        self.assertNotIn("versatile", self.codes())
        self.done(1, help_type=HELP_TYPES[3])
        achievements.evaluate(self.vol)
        self.assertIn("versatile", self.codes())

    def test_donation_courier_from_the_delivery_service(self):
        from myapp.services import donations

        offer = Donation.objects.create(donor=self.client_user, item_name="Мука", category="food", quantity=2,
                                        status="ready", assigned_volunteer=self.vol)
        donations.mark_offer_received(offer, actor=self.vol)
        self.assertIn("donation_courier", self.codes())

    def test_pet_friend_when_the_report_is_resolved(self):
        from myapp.services import pets

        report = PetReport.objects.create(reporter=self.vol, report_type="found", species="dog", description="d",
                                          region="dushanbe")
        pets.apply_action(report, "resolve", actor=self.vol)
        self.assertIn("pet_friend", self.codes())

    def test_completing_a_task_through_the_view_awards(self):
        task = HelpRequest.objects.create(client=self.client_user, help_type="grocery", description="d",
                                          address="a", phone="+992901234567", region="dushanbe")
        HelpRequest.objects.filter(pk=task.pk).update(status="active", volunteer=self.vol,
                                                      accepted_at=timezone.now())
        self.client.force_login(self.vol)
        self.client.post(reverse("complete_task", args=[task.pk]))
        self.assertIn("first_help", self.codes())

    def test_inactive_badges_and_rule_less_rows_never_award(self):
        Achievement.objects.filter(code="first_help").update(is_active=False)
        Achievement.objects.create(code="admin_made", threshold=0)
        self.done(1)
        achievements.evaluate(self.vol)
        self.assertEqual(self.codes(), set())

    def test_threshold_is_editable_by_the_owner(self):
        Achievement.objects.filter(code="tasks_10").update(threshold=3)
        self.done(3)
        achievements.evaluate(self.vol)
        self.assertIn("tasks_10", self.codes())


class AwardingTests(AchievementBase):
    def test_volunteers_only(self):
        self.done(1, volunteer=self.curator)
        for user in (self.client_user, self.curator, self.admin, AnonymousUser(), None):
            with self.subTest(user=str(user)):
                self.assertEqual(achievements.evaluate(user), [])
        inactive = _make("ach_inactive", is_volunteer=True)
        self.done(1, volunteer=inactive)
        Users.objects.filter(pk=inactive.pk).update(is_active=False)
        inactive.refresh_from_db()
        self.assertEqual(achievements.evaluate(inactive), [])

    def test_idempotent_and_notifies_once(self):
        self.done(1)
        achievements.evaluate(self.vol)
        achievements.evaluate(self.vol)
        self.assertEqual(UserAchievement.objects.filter(user=self.vol).count(), 1)
        rows = Notification.objects.filter(recipient=self.vol, type="achievement_unlocked")
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().params, {"achievement_code": "first_help"})
        self.assertEqual(rows.first().link, reverse("profile") + "#achievements")

    def test_a_lost_race_does_not_award_or_notify_twice(self):
        self.done(1)
        first = Achievement.objects.get(code="first_help")
        UserAchievement.objects.create(user=self.vol, achievement=first)  # the "other worker" won
        with mock.patch.object(UserAchievement.objects, "filter") as held:
            held.return_value.values_list.return_value = []  # this worker didn't see it
            self.assertEqual(achievements.evaluate(self.vol), [])
        self.assertFalse(Notification.objects.filter(type="achievement_unlocked").exists())

    def test_hiding_a_review_never_revokes_a_badge(self):
        tasks = self.done(5)
        reviews = [self.review(t, stars=5, tags=["polite"]) for t in tasks]
        self.assertIn("kind_words", self.codes())
        feedback.hide(self.curator, reviews[0])
        self.assertIn("kind_words", self.codes())

    def test_award_safely_never_raises(self):
        with mock.patch("myapp.services.achievements.evaluate", side_effect=RuntimeError("boom")):
            self.assertEqual(achievements.award_safely(self.vol, "x"), [])

    def test_a_notify_failure_keeps_the_badge(self):
        self.done(1)
        with mock.patch("myapp.services.notify.notify", side_effect=RuntimeError("boom")):
            awarded = achievements.evaluate(self.vol)
        self.assertEqual(len(awarded), 1)
        self.assertIn("first_help", self.codes())

    def test_channel_text_names_the_badge_in_the_recipient_language(self):
        from myapp.services import notify_texts

        subject, body = notify_texts.render_channel("achievement_unlocked", "tj", {"achievement_code": "versatile"})
        self.assertIn("Устои ҳамакора", body)

    def test_backfill_is_silent_and_idempotent(self):
        self.done(10)
        out = io.StringIO()
        call_command("backfill_achievements", "--dry-run", stdout=out)
        self.assertIn("would award 2", out.getvalue())
        self.assertFalse(UserAchievement.objects.exists())
        call_command("backfill_achievements", stdout=io.StringIO())
        self.assertEqual(self.codes(), {"first_help", "tasks_10"})
        self.assertFalse(Notification.objects.filter(type="achievement_unlocked").exists())
        out = io.StringIO()
        call_command("backfill_achievements", stdout=out)
        self.assertIn("awarded 0", out.getvalue())

    def test_backfill_notify_flag(self):
        self.done(1)
        call_command("backfill_achievements", "--notify", stdout=io.StringIO())
        self.assertTrue(Notification.objects.filter(type="achievement_unlocked", recipient=self.vol).exists())


class MatchingIndependenceTests(AchievementBase):
    """Owner rule: achievements and reviews never change who gets recommended."""

    def _volunteer(self, name, lat):
        user = _make(name, is_volunteer=True)
        Profile.objects.filter(user=user).update(latitude=lat, longitude=68.78, location_updated_at=timezone.now(),
                                                 availability_status="available")
        return Users.objects.get(pk=user.pk)

    def test_badges_and_reviews_do_not_change_the_ranking(self):
        a = self._volunteer("ach_match_a", 38.560)
        b = self._volunteer("ach_match_b", 38.560)
        task = HelpRequest.objects.create(client=self.client_user, help_type="grocery", description="d",
                                          address="a", phone="+992901234567", region="dushanbe",
                                          latitude=38.57, longitude=68.79)
        before = [(r["volunteer"].pk, r["score"]) for r in matching.recommend_volunteers(task)]
        # b gets every badge and glowing reviews; a gets poor reviews.
        for achievement in Achievement.objects.all():
            UserAchievement.objects.create(user=b, achievement=achievement)
        Profile.objects.filter(user=b).update(review_avg=5, review_count=40)
        Profile.objects.filter(user=a).update(review_avg=1.5, review_count=40)
        after = [(r["volunteer"].pk, r["score"]) for r in matching.recommend_volunteers(task)]
        self.assertEqual(before, after)

        vol_tasks_before = [(r["task"].pk, r["score"]) for r in matching.recommend_tasks(a)]
        UserAchievement.objects.filter(user=b).delete()
        for achievement in Achievement.objects.all():
            UserAchievement.objects.create(user=a, achievement=achievement)
        self.assertEqual(vol_tasks_before, [(r["task"].pk, r["score"]) for r in matching.recommend_tasks(a)])

    def test_matching_source_never_reads_badges_or_reviews(self):
        import inspect

        source = inspect.getsource(matching).lower()
        for word in ("achievement", "review", "badge", "rating"):
            self.assertNotIn(word, source)


class ReadTests(AchievementBase):
    def test_counters_use_a_constant_number_of_queries(self):
        with CaptureQueriesContext(connection) as small:
            achievements.counters(self.vol)
        tasks = self.done(15)
        for task in tasks:
            self.review(task, stars=5, tags=["polite"])
        with CaptureQueriesContext(connection) as big:
            achievements.counters(self.vol)
        self.assertEqual(len(small.captured_queries), len(big.captured_queries))
        self.assertLessEqual(len(big.captured_queries), 4)

    def test_progress_for_profile_grid(self):
        self.done(3)
        achievements.evaluate(self.vol)
        items = {i["code"]: i for i in achievements.progress_for(self.vol)}
        self.assertNotIn("mentor", items)  # inactive and not earned → hidden
        self.assertTrue(items["first_help"]["earned"])
        self.assertEqual((items["tasks_10"]["value"], items["tasks_10"]["threshold"], items["tasks_10"]["percent"]),
                         (3, 10, 30))
        self.assertEqual(items["tasks_10"]["rule_key"], "ach.rule_milestone")

    def test_earned_badges_stay_visible_when_deactivated(self):
        self.done(1)
        achievements.evaluate(self.vol)
        Achievement.objects.filter(code="first_help").update(is_active=False)
        codes = [i["code"] for i in achievements.progress_for(self.vol)]
        self.assertIn("first_help", codes)

    def test_top_badges_rarest_first_in_one_query(self):
        other = _make("ach_vol3", is_volunteer=True)
        for code in ("first_help", "trusted", "kind_words", "tasks_100"):
            UserAchievement.objects.create(user=self.vol, achievement=Achievement.objects.get(code=code))
        with CaptureQueriesContext(connection) as ctx:
            top = achievements.top_badges([self.vol.pk, other.pk])
        self.assertEqual(len(ctx.captured_queries), 1)
        self.assertEqual([a.code for a in top[self.vol.pk]], ["tasks_100", "trusted", "kind_words"])
        self.assertEqual(top[other.pk], [])


class UiTests(AchievementBase):
    def test_volunteer_profile_shows_grid_and_dialog(self):
        self.done(1)
        achievements.evaluate(self.vol)
        self.client.force_login(self.vol)
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'id="achievements"')
        self.assertContains(response, 'class="gc-dialog badge-dialog"')
        self.assertContains(response, 'data-code="first_help"')
        self.assertContains(response, 'data-i18n="ach.first_help.title"')
        self.assertContains(response, "js/achievements.js")
        self.assertContains(response, 'data-i18n="ach.section_lede"')

    def test_other_roles_have_no_grid(self):
        for user in (self.client_user, self.curator, self.admin):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertNotContains(self.client.get(reverse("profile")), 'class="badge-grid"')

    def test_staff_volunteer_page_shows_the_grid(self):
        self.done(1)
        achievements.evaluate(self.vol)
        self.client.force_login(self.curator)
        response = self.client.get(reverse("crm_volunteer_detail", args=[self.vol.pk]))
        self.assertContains(response, 'class="badge-grid"')
        self.assertNotContains(response, 'data-i18n="ach.section_lede"')  # "your…" wording is for the owner

    def test_public_rating_shows_only_the_badge(self):
        self.done(1)
        achievements.evaluate(self.vol)
        response = self.client.get(reverse("rating"))
        self.assertContains(response, 'class="badge-row"')
        self.assertContains(response, "images/badges/first_help")
        content = response.content.decode()
        self.assertNotIn("data-threshold", content)
        self.assertNotIn("badge-progress", content)
        self.assertNotIn(self.vol.email, content)

    def test_i18n_keys_exist_in_every_language(self):
        from server.i18n_prerender import translation_table

        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key, text in achievements.ui_i18n_keys(lang).items():
                self.assertEqual(table.get(key), text, f"{lang} {key}")
