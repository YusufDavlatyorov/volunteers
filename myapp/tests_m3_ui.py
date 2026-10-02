"""Milestone 3 UI integration (Feature Pack v2 features 8 + 9): chat entry
points with unread badges, review calls to action, constant dashboard query
counts, private chat image URLs and EN/RU/TJ completeness of every new key."""

import re
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile
from myapp.models import REVIEW_TAGS, ChatMessage, HelpRequest, TaskReview
from myapp.services import chat, feedback
from myapp.tests_realtime import _make
from server.i18n_prerender import translation_table

BASE = Path(settings.BASE_DIR)


class _M3Fixture(TestCase):
    def setUp(self):
        cache.clear()
        self.client_user = _make("m3_client", is_client=True)
        self.other_client = _make("m3_client2", is_client=True)
        self.vol = _make("m3_vol", is_volunteer=True)
        self.other_vol = _make("m3_vol2", is_volunteer=True)
        self.curator = _make("m3_curator", is_curator=True)
        self.admin = _make("m3_admin", is_superuser=True, is_staff=True)
        Profile.objects.filter(user=self.curator).update(full_name="Curator Secretname")
        self.task = self._task("active")

    def _task(self, status, *, client=None, volunteer="default", ended_days_ago=0):
        task = HelpRequest.objects.create(
            client=client or self.client_user, help_type="grocery", description="bread", address="Rudaki 1",
            phone="+992901234567", region="dushanbe",
        )
        when = timezone.now() - timedelta(days=ended_days_ago)
        HelpRequest.objects.filter(pk=task.pk).update(
            status=status, volunteer=self.vol if volunteer == "default" else volunteer,
            accepted_at=None if status == "pending" else when,
            completed_at=when if status == "completed" else None,
        )
        task.refresh_from_db()
        return task

    def _msg(self, task, sender, body="hello", **extra):
        return ChatMessage.objects.create(task=task, sender=sender, kind="text", body=body, **extra)

    def _get(self, user, name, *args):
        self.client.force_login(user)
        return self.client.get(reverse(name, args=args))


class ChatEntryPointTests(_M3Fixture):
    def test_task_detail_chat_button_for_parties_and_staff_only(self):
        for user in (self.client_user, self.vol, self.curator, self.admin):
            response = self._get(user, "task_detail", self.task.pk)
            self.assertContains(response, "data-chat-entry", msg_prefix=user.username)
            self.assertContains(response, reverse("task_chat", args=[self.task.pk]))
        # a volunteer who can't open the task never gets the page at all
        response = self._get(self.other_vol, "task_detail", self.task.pk)
        self.assertEqual(response.status_code, 302)

    def test_no_chat_button_while_pending_for_the_client(self):
        pending = self._task("pending", volunteer=None)
        self.assertNotContains(self._get(self.client_user, "task_detail", pending.pk), "data-chat-entry")
        # a volunteer who may *view* a pending task can't read its chat
        self.assertNotContains(self._get(self.other_vol, "task_detail", pending.pk), "data-chat-entry")

    def test_archived_chat_hidden_from_parties_but_not_staff(self):
        old = self._task("completed", ended_days_ago=settings.CHAT_ARCHIVE_DAYS + 5)
        self.assertNotContains(self._get(self.client_user, "task_detail", old.pk), "data-chat-entry")
        self.assertNotContains(self._get(self.vol, "task_detail", old.pk), "data-chat-entry")
        self.assertContains(self._get(self.curator, "task_detail", old.pk), "data-chat-entry")

    def test_unread_badge_numbers(self):
        self._msg(self.task, self.client_user, "one")
        self._msg(self.task, self.client_user, "two")
        mine = self._msg(self.task, self.vol, "reply")
        response = self._get(self.vol, "task_detail", self.task.pk)
        self.assertContains(response, 'data-count="2"')
        self.assertContains(response, "Новых: 2")   # server pre-render (ru) of chat.unread
        # the client has one unread (the volunteer's reply); their own don't count
        response = self._get(self.client_user, "task_detail", self.task.pk)
        self.assertContains(response, 'data-count="1"')
        chat.mark_read(self.client_user, self.task, mine.pk)
        response = self._get(self.client_user, "task_detail", self.task.pk)
        self.assertContains(response, 'data-count="0"')
        self.assertRegex(response.content.decode(), r'data-chat-unread[^>]*data-count="0"[^>]* hidden')

    def test_hidden_messages_do_not_count_for_parties(self):
        self._msg(self.task, self.client_user, "bad", hidden_at=timezone.now())
        self.assertContains(self._get(self.vol, "task_detail", self.task.pk), 'data-count="0"')

    def test_dashboards_and_task_list_show_badges(self):
        self._msg(self.task, self.client_user, "a")
        self._msg(self.task, self.client_user, "b")
        self._msg(self.task, self.client_user, "c")
        self._msg(self.task, self.vol, "x")
        vol_dash = self._get(self.vol, "profile")
        self.assertContains(vol_dash, "data-chat-entry")
        self.assertContains(vol_dash, 'data-count="3"')
        self.assertContains(self._get(self.vol, "task_list"), 'data-count="3"')
        client_dash = self._get(self.client_user, "profile")
        self.assertContains(client_dash, "data-chat-entry")
        self.assertContains(client_dash, 'data-count="1"')

    def test_client_pending_request_has_no_chat_on_dashboard(self):
        HelpRequest.objects.filter(pk=self.task.pk).delete()
        self._task("pending", volunteer=None)
        response = self._get(self.client_user, "profile")
        self.assertNotContains(response, "data-chat-entry")
        self.assertNotContains(response, "data-chat-unread")


class DashboardQueryCountTests(_M3Fixture):
    """Unread counts come from ONE chat.unread_counts call per page."""

    def _count(self, user, name):
        self.client.force_login(user)
        self.client.get(reverse(name))  # warm caches (session, content types, …)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse(name))
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_client_dashboard_constant_in_number_of_requests(self):
        self._msg(self.task, self.vol)
        small = self._count(self.client_user, "profile")
        for _ in range(4):
            task = self._task("active")
            self._msg(task, self.vol)
            done = self._task("completed")
            self._msg(done, self.vol)
        self.assertEqual(self._count(self.client_user, "profile"), small)

    def test_volunteer_task_list_constant_in_number_of_active_tasks(self):
        self._msg(self.task, self.client_user)
        small = self._count(self.vol, "task_list")
        for _ in range(4):
            self._msg(self._task("active"), self.client_user)
        self.assertEqual(self._count(self.vol, "task_list"), small)

    def test_volunteer_dashboard_has_one_unread_query(self):
        self._msg(self.task, self.client_user)
        self.client.force_login(self.vol)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("profile"))
        unread = [q for q in ctx.captured_queries if "myapp_chatreadstate" in q["sql"].lower()]
        self.assertEqual(len(unread), 1)


class ChatPageUiTests(_M3Fixture):
    def test_writer_gets_composer_quick_replies_and_large_toggle(self):
        response = self._get(self.vol, "task_chat", self.task.pk)
        for needle in ("data-chat-form", "data-chat-quick", 'data-quick="chat.quick.on_way"',
                       'data-quick="chat.quick.arrived"', 'data-quick="chat.quick.call_me"',
                       "data-chat-large", 'aria-pressed="false"', "data-chat-upload", 'role="log"'):
            self.assertContains(response, needle)
        self.assertContains(response, "css/chat.css")
        self.assertNotContains(response, "<style>")  # styles live in chat.css

    def test_quick_replies_per_role(self):
        response = self._get(self.client_user, "task_chat", self.task.pk)
        self.assertContains(response, 'data-quick="chat.quick.call_me"')
        self.assertNotContains(response, 'data-quick="chat.quick.on_way"')
        self.assertNotContains(response, 'data-quick="chat.quick.arrived"')
        response = self._get(self.curator, "task_chat", self.task.pk)
        self.assertContains(response, "data-chat-form")       # staff may post (as Куратор)
        self.assertNotContains(response, "data-chat-quick")

    def test_report_is_for_parties_only(self):
        self._msg(self.task, self.client_user, "hi")
        self.assertContains(self._get(self.vol, "task_chat", self.task.pk), "data-chat-report")
        response = self._get(self.curator, "task_chat", self.task.pk)
        self.assertNotContains(response, "data-chat-report")
        self.assertContains(response, "data-chat-hide")

    def test_completed_chat_read_only_with_review_button_for_client(self):
        done = self._task("completed")
        self._msg(done, self.vol, "bye")
        response = self._get(self.client_user, "task_chat", done.pk)
        self.assertNotContains(response, "data-chat-form")
        self.assertContains(response, 'data-i18n="chat.read_only"')
        self.assertContains(response, reverse("task_review", args=[done.pk]))
        response = self._get(self.vol, "task_chat", done.pk)
        self.assertContains(response, 'data-i18n="chat.read_only"')
        self.assertNotContains(response, reverse("task_review", args=[done.pk]))

    def test_staff_post_shows_label_not_name_to_parties(self):
        self._msg(self.task, self.curator, "from staff", is_staff_post=True)
        for user in (self.client_user, self.vol):
            response = self._get(user, "task_chat", self.task.pk)
            self.assertContains(response, 'data-i18n="chat.staff_label"')
            self.assertNotContains(response, "Curator Secretname")
        self.assertContains(self._get(self.admin, "task_chat", self.task.pk), "Curator Secretname")

    def test_hidden_message_is_a_stub_for_parties(self):
        self._msg(self.task, self.vol, "rude words", hidden_at=timezone.now())
        response = self._get(self.client_user, "task_chat", self.task.pk)
        self.assertNotContains(response, "rude words")
        self.assertContains(response, 'data-i18n="chat.hidden_stub"')
        response = self._get(self.curator, "task_chat", self.task.pk)
        self.assertContains(response, "rude words")
        self.assertContains(response, 'value="unhide"')

    def test_links_are_never_anchors(self):
        self._msg(self.task, self.vol, "see https://evil.example/x")
        response = self._get(self.client_user, "task_chat", self.task.pk)
        self.assertContains(response, "https://evil.example/x")
        self.assertNotContains(response, 'href="https://evil.example')

    def test_no_media_chat_urls_anywhere(self):
        ChatMessage.objects.create(
            task=self.task, sender=self.vol, kind="image", body="photo", image="chat/2026/10/x.png",
            image_variants={"thumb": "chat/2026/10/x_thumb.webp", "medium": "chat/2026/10/x_medium.webp"},
        )
        protected = reverse("chat_message_image", args=[ChatMessage.objects.get(kind="image").pk, "thumb"])
        pages = [("task_chat", self.task.pk), ("task_detail", self.task.pk), ("profile",)]
        for user in (self.client_user, self.vol, self.curator):
            for name, *args in pages:
                body = self._get(user, name, *args).content.decode()
                self.assertNotIn("/media/chat/", body, f"{user.username} {name}")
            body = self._get(user, "task_chat", self.task.pk).content.decode()
            self.assertIn(protected, body)
            self.assertRegex(body, r'<img class="chat-msg__img"[^>]*width="320" height="240" loading="lazy"')


class ReviewEntryTests(_M3Fixture):
    def setUp(self):
        super().setUp()
        self.done = self._task("completed")

    def test_rate_cta_only_for_the_client_of_a_completed_task(self):
        response = self._get(self.client_user, "task_detail", self.done.pk)
        self.assertContains(response, 'id="reviewCta"')
        self.assertContains(response, reverse("task_review", args=[self.done.pk]))
        self.assertContains(response, 'data-i18n="review.cta_hint"')
        for user in (self.vol, self.curator, self.admin):
            self.assertNotContains(self._get(user, "task_detail", self.done.pk), 'id="reviewCta"')
        # not before completion
        self.assertNotContains(self._get(self.client_user, "task_detail", self.task.pk), 'id="reviewCta"')

    def test_edit_within_window_then_read_only(self):
        feedback.submit_review(self.client_user, self.done, stars=4, comment="Kind and quick", tags=["polite"])
        response = self._get(self.client_user, "task_detail", self.done.pk)
        self.assertContains(response, 'data-i18n="review.title_edit"')
        self.assertContains(response, 'data-i18n="review.yours"')
        TaskReview.objects.filter(task=self.done).update(
            created_at=timezone.now() - timedelta(hours=settings.REVIEW_EDIT_HOURS + 1))
        response = self._get(self.client_user, "task_detail", self.done.pk)
        self.assertNotContains(response, 'data-i18n="review.title_edit"')
        self.assertContains(response, 'data-i18n="review.view"')

    def test_volunteer_sees_the_review_with_comment_on_the_task(self):
        feedback.submit_review(self.client_user, self.done, stars=5, comment="Golden hands", tags=[])
        response = self._get(self.vol, "task_detail", self.done.pk)
        self.assertContains(response, 'data-i18n="review.client_review"')
        self.assertContains(response, "Golden hands")

    def test_volunteer_dashboard_average_and_my_reviews_action(self):
        feedback.submit_review(self.client_user, self.done, stars=4, comment="secret comment", tags=[])
        response = self._get(self.vol, "profile")
        self.assertContains(response, reverse("my_reviews"))
        self.assertContains(response, "4.0")
        self.assertContains(response, 'data-i18n="review.average"')
        self.assertNotContains(response, "secret comment")

    def test_staff_navigation_and_volunteer_detail_average(self):
        feedback.submit_review(self.client_user, self.done, stars=2, comment="late", tags=[])
        self.assertContains(self._get(self.curator, "admin_panel"), reverse("crm_reviews"))
        self.assertContains(self._get(self.curator, "profile"), reverse("crm_reviews"))
        response = self._get(self.curator, "crm_volunteer_detail", self.vol.pk)
        self.assertContains(response, "2.0")
        self.assertContains(response, 'data-i18n="review.average"')

    def test_public_rating_shows_average_but_never_comments(self):
        feedback.submit_review(self.client_user, self.done, stars=5, comment="private words", tags=["polite"])
        response = self.client.get(reverse("rating"))
        self.assertContains(response, "review-summary")
        self.assertNotContains(response, "private words")

    def test_review_form_is_accessible(self):
        response = self._get(self.client_user, "task_review", self.done.pk)
        self.assertContains(response, 'type="radio" name="stars"', count=5)
        self.assertContains(response, "<legend", count=2)
        self.assertContains(response, 'class="review-chip"', count=len(REVIEW_TAGS))
        self.assertContains(response, "3 из 5")       # data-i18n-args pre-rendered
        self.assertNotContains(response, "<style>")


class M3I18nTests(TestCase):
    """Every key the milestone-3 templates and scripts use exists in EN/RU/TJ."""

    TEMPLATES = [
        "templates/myapp/chat.html", "templates/myapp/_chat_button.html", "templates/myapp/task_detail.html",
        "templates/myapp/dashboard/_volunteer.html", "templates/myapp/dashboard/_client.html",
        "templates/myapp/task_list.html", "templates/accounts/profile.html", "templates/myapp/admin_panel.html",
        "templates/myapp/crm_volunteer_detail.html",
        *[str(p.relative_to(BASE)) for p in (BASE / "templates/myapp/reviews").glob("*.html")],
    ]
    SCRIPTS = ["static/js/chat.js", "static/js/live-ui.js", "static/js/profile-location.js"]
    PREFIXES = ("chat.", "chatpage.", "review.", "ploc.", "ntf.")

    def _used(self):
        keys = set()
        for rel in self.TEMPLATES:
            text = (BASE / rel).read_text(encoding="utf-8")
            for key in re.findall(r'data-i18n(?:-ph)?="([^"{}]+)"', text):
                keys.add(key)
            for a, b in re.findall(r'\{% if [^%]*%\}([a-z_.0-9]+)\{% else(?:if)?[^%]*%\}([a-z_.0-9]+)', text):
                keys.update((a, b))
        for rel in self.SCRIPTS:
            text = (BASE / rel).read_text(encoding="utf-8")
            keys.update(re.findall(r"""\bt\(\s*'([a-z_.0-9]+)'""", text))
            keys.update(re.findall(r"""keyed\([^)]*?'((?:chat|chatpage|review)\.[a-z_.0-9]+)'""", text))
        keys.update(f"review.tag.{tag}" for tag in REVIEW_TAGS)
        keys.update(f"review.star_{n}" for n in range(1, 6))
        keys.update(f"review.err.{code}" for code in (feedback.INVALID_STARS, feedback.COMMENT_TOO_LONG,
                                                       feedback.INVALID_TAGS))
        keys.update(chat.UI_STRINGS)
        return {k for k in keys if k.startswith(self.PREFIXES)}

    def test_all_new_keys_exist_in_every_language(self):
        used = self._used()
        self.assertGreater(len(used), 80)
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            missing = sorted(k for k in used if not table.get(k))
            self.assertEqual(missing, [], f"{lang} is missing {missing}")

    def test_chat_ui_strings_match_i18n_js(self):
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key, text in chat.ui_i18n_keys(lang).items():
                self.assertEqual(table.get(key), text, f"{lang} {key}")
