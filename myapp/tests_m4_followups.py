"""Milestone 4 follow-ups (owner, 2026-10-03):

1. chat retention — photos 90 d / messages 180 d after the task ended, flagged
   or reported messages kept until reviewed + 180 d (``purge_chats`` cron),
   and the curator's "Проверено" action that closes a case;
2. public rating without places or points, by region then name;
3. chat-flag statistics in the CRM analytics.
"""

import io
import shutil
import tempfile
from datetime import timedelta

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import ChatMessage, ChatReadState, ChatReport, HelpRequest, TaskReview
from myapp.services import analytics_v2, chat, chat_retention

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "M4-Followups-2026!"
_MEDIA = tempfile.mkdtemp(prefix="gc-m4f-tests-")


def tearDownModule():
    shutil.rmtree(_MEDIA, ignore_errors=True)


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD,
        region=flags.pop("region", "dushanbe"), **flags,
    )


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA,
                   CHAT_PHOTO_RETENTION_DAYS=90, CHAT_TEXT_RETENTION_DAYS=180, CHAT_HOLD_DAYS=180)
class RetentionBase(TestCase):
    def setUp(self):
        cache.clear()
        self.client_user = _make("rt_client", is_client=True)
        self.vol = _make("rt_vol", is_volunteer=True)
        self.curator = _make("rt_curator", is_curator=True)

    def task(self, *, ended_days_ago=None, status="completed"):
        task = HelpRequest.objects.create(client=self.client_user, help_type="grocery", description="d",
                                          address="a", phone="+992901234567", region="dushanbe")
        fields = {"volunteer": self.vol, "status": "active"}
        if ended_days_ago is not None:
            when = timezone.now() - timedelta(days=ended_days_ago)
            fields["status"] = status
            fields["completed_at" if status == "completed" else "cancelled_at"] = when
        HelpRequest.objects.filter(pk=task.pk).update(**fields)
        task.refresh_from_db()
        return task

    def message(self, task, body="hello", *, photo=False, flags=(), sender=None):
        msg = ChatMessage.objects.create(task=task, sender=sender or self.vol, body=body, flags=list(flags),
                                         kind="image" if photo else "text")
        if photo:
            msg.image.save("p.jpg", ContentFile(b"jpegbytes"), save=False)
            thumb = msg.image.storage.save("chat/p_thumb.webp", ContentFile(b"webp"))
            msg.image_variants = {"thumb": thumb}
            msg.save()
        return msg


class RetentionTests(RetentionBase):
    def test_photos_go_after_90_days_text_stays(self):
        old = self.task(ended_days_ago=91)
        recent = self.task(ended_days_ago=89)
        a = self.message(old, "фото", photo=True)
        b = self.message(recent, "фото", photo=True)
        storage = a.image.storage
        a_files = [a.image.name, a.image_variants["thumb"]]
        result = chat_retention.purge()
        self.assertEqual((result.photos, result.messages), (1, 0))
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.image.name, a.image_variants, a.body), ("", {}, "фото"))
        self.assertTrue(b.image)
        for path in a_files:
            self.assertFalse(storage.exists(path))

    def test_messages_go_after_180_days_with_reports_and_read_cursors(self):
        old = self.task(ended_days_ago=181)
        keep = self.task(ended_days_ago=170)
        m1 = self.message(old, photo=True)
        storage, path = m1.image.storage, m1.image.name
        self.message(old, "ещё")
        self.message(keep, "свежее")
        ChatReadState.objects.create(task=old, user=self.vol, last_read_id=m1.pk)
        ChatReadState.objects.create(task=keep, user=self.vol, last_read_id=1)
        result = chat_retention.purge()
        self.assertEqual(result.messages, 2)
        self.assertFalse(ChatMessage.objects.filter(task=old).exists())
        self.assertTrue(ChatMessage.objects.filter(task=keep).exists())
        self.assertFalse(storage.exists(path))
        self.assertEqual(list(ChatReadState.objects.values_list("task_id", flat=True)), [keep.pk])
        self.assertEqual(result.read_states, 1)

    def test_open_tasks_are_never_touched(self):
        active = self.task()
        msg = self.message(active, photo=True)
        ChatMessage.objects.filter(pk=msg.pk).update(created_at=timezone.now() - timedelta(days=400))
        chat_retention.purge()
        msg.refresh_from_db()
        self.assertTrue(msg.image)

    def test_cancelled_tasks_follow_the_same_clock(self):
        old = self.task(ended_days_ago=200, status="cancelled")
        self.message(old)
        self.assertEqual(chat_retention.purge().messages, 1)

    def test_unreviewed_flags_and_open_reports_are_held(self):
        old = self.task(ended_days_ago=400)
        flagged = self.message(old, "переведите деньги", photo=True, flags=["money"])
        reported = self.message(old, "грубость")
        ChatReport.objects.create(message=reported, reporter=self.client_user)
        plain = self.message(old, "обычное")
        result = chat_retention.purge()
        self.assertEqual(result.messages, 1)
        self.assertEqual(result.held, 2)
        self.assertEqual(set(ChatMessage.objects.values_list("pk", flat=True)), {flagged.pk, reported.pk})
        flagged.refresh_from_db()
        self.assertTrue(flagged.image)  # held messages keep their photo too
        self.assertFalse(ChatMessage.objects.filter(pk=plain.pk).exists())

    def test_reviewed_cases_are_kept_180_more_days(self):
        old = self.task(ended_days_ago=400)
        recent_review = self.message(old, "деньги", flags=["money"])
        long_ago = self.message(old, "деньги", flags=["money"])
        handled = self.message(old, "жалоба")
        ChatMessage.objects.filter(pk=recent_review.pk).update(reviewed_at=timezone.now() - timedelta(days=10))
        ChatMessage.objects.filter(pk=long_ago.pk).update(reviewed_at=timezone.now() - timedelta(days=200))
        ChatReport.objects.create(message=handled, reporter=self.client_user,
                                  handled_at=timezone.now() - timedelta(days=200))
        chat_retention.purge()
        self.assertEqual(list(ChatMessage.objects.values_list("pk", flat=True)), [recent_review.pk])

    def test_dry_run_and_idempotent(self):
        old = self.task(ended_days_ago=200)
        self.message(old)
        self.message(old, photo=True)
        dry = chat_retention.purge(dry_run=True)
        self.assertEqual((dry.messages, dry.photos), (2, 1))
        self.assertEqual(ChatMessage.objects.count(), 2)
        chat_retention.purge()
        again = chat_retention.purge()
        self.assertEqual((again.photos, again.messages, again.read_states), (0, 0, 0))

    def test_periods_come_from_settings(self):
        old = self.task(ended_days_ago=40)
        self.message(old)
        with self.settings(CHAT_TEXT_RETENTION_DAYS=30):
            self.assertEqual(chat_retention.purge().messages, 1)

    def test_command(self):
        old = self.task(ended_days_ago=200)
        self.message(old)
        out = io.StringIO()
        call_command("purge_chats", "--dry-run", stdout=out)
        self.assertIn("would delete: 0 photo(s) (> 90 d), 1 message(s) (> 180 d)", out.getvalue())
        out = io.StringIO()
        call_command("purge_chats", stdout=out)
        self.assertIn("deleted: 0 photo(s)", out.getvalue())
        self.assertFalse(ChatMessage.objects.exists())

    def test_query_count_does_not_grow_with_rows(self):
        old = self.task(ended_days_ago=200)
        for _ in range(3):
            self.message(old)
        with CaptureQueriesContext(connection) as small:
            chat_retention.purge(dry_run=True)
        for _ in range(30):
            self.message(old)
        with CaptureQueriesContext(connection) as big:
            chat_retention.purge(dry_run=True)
        self.assertEqual(len(small.captured_queries), len(big.captured_queries))

    def test_expired_photo_shows_a_note_in_the_chat(self):
        task = self.task(ended_days_ago=100)
        self.message(task, "", photo=True)
        chat_retention.purge()
        self.client.force_login(self.curator)
        response = self.client.get(reverse("task_chat", args=[task.pk]))
        self.assertContains(response, 'data-i18n="chat.photo_expired"')

    def test_about_page_states_the_retention(self):
        self.assertContains(self.client.get(reverse("about")), 'data-i18n="about.privacy_chat"')


class ReviewActionTests(RetentionBase):
    def setUp(self):
        super().setUp()
        self.active = self.task()

    def test_review_closes_flags_and_reports(self):
        msg = self.message(self.active, "переведите деньги", flags=["money"], sender=self.vol)
        ChatReport.objects.create(message=msg, reporter=self.client_user)
        payload = chat.message_payload(msg, self.curator)
        self.assertTrue(payload["needs_review"])
        self.assertEqual(payload["open_reports"], 1)
        outcome = chat.review(self.curator, msg)
        self.assertTrue(outcome.ok and outcome.created)
        msg.refresh_from_db()
        self.assertIsNotNone(msg.reviewed_at)
        self.assertEqual(msg.reviewed_by, self.curator)
        self.assertIsNotNone(ChatReport.objects.get(message=msg).handled_at)
        payload = chat.message_payload(msg, self.curator)
        self.assertEqual((payload["needs_review"], payload["reviewed"], payload["open_reports"]), (False, True, 0))
        self.assertIsNone(msg.hidden_at)  # still visible — hide is separate
        self.assertFalse(chat.review(self.curator, msg).created)  # idempotent

    def test_only_staff(self):
        msg = self.message(self.active, "деньги", flags=["money"])
        for user in (self.vol, self.client_user):
            self.assertEqual(chat.review(user, msg).code, chat.FORBIDDEN)

    def test_parties_never_see_review_fields(self):
        msg = self.message(self.active, "деньги", flags=["money"])
        for viewer in (self.vol, self.client_user):
            payload = chat.message_payload(msg, viewer)
            for key in ("needs_review", "reviewed", "open_reports", "flags"):
                self.assertNotIn(key, payload)

    def test_history_annotates_reports_in_one_query(self):
        for i in range(5):
            msg = self.message(self.active, f"m{i}")
            ChatReport.objects.create(message=msg, reporter=self.client_user)
        with CaptureQueriesContext(connection) as ctx:
            page = chat.history(self.curator, self.active)
        self.assertTrue(all(m["open_reports"] == 1 for m in page["messages"]))
        self.assertLess(len(ctx.captured_queries), 10)

    def test_view_and_button(self):
        msg = self.message(self.active, "деньги", flags=["money"])
        self.client.force_login(self.curator)
        page = self.client.get(reverse("task_chat", args=[self.active.pk]))
        self.assertContains(page, reverse("chat_message_review", args=[msg.pk]))
        self.assertContains(page, 'data-i18n="chat.flag.money"')
        response = self.client.post(reverse("chat_message_review", args=[msg.pk]),
                                    HTTP_ACCEPT="application/json", HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json()["ok"], True)
        page = self.client.get(reverse("task_chat", args=[self.active.pk]))
        self.assertNotContains(page, reverse("chat_message_review", args=[msg.pk]))
        self.assertContains(page, 'data-i18n="chat.reviewed_tag"')
        self.client.force_login(self.vol)
        self.assertNotContains(self.client.get(reverse("task_chat", args=[self.active.pk])), "chat.flag.money")
        response = self.client.post(reverse("chat_message_review", args=[msg.pk]))
        self.assertNotEqual(response.status_code, 200)
        msg.refresh_from_db()
        self.assertEqual(msg.reviewed_by, self.curator)


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class RatingPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.people = {}
        for username, region, name, points in (
            ("rp_b", "sogd", "Бахтиёр", 900), ("rp_a", "sogd", "азиза", 5),
            ("rp_c", "dushanbe", "Шамсия", 10), ("rp_d", "", "Ёдгор", 50), ("rp_e", "dushanbe", "Алишер", 1),
        ):
            user = _make(username, is_volunteer=True, region=region)
            Profile.objects.filter(user=user).update(full_name=name, rating=points)
            self.people[username] = user
        _make("rp_client", is_client=True)

    def _names(self, response):
        return [p.full_name for p in response.context["page_obj"]]

    def test_sorted_by_region_then_name_not_points(self):
        response = self.client.get(reverse("rating"))
        self.assertEqual(self._names(response), ["Алишер", "Шамсия", "азиза", "Бахтиёр", "Ёдгор"])

    def test_no_places_and_no_points(self):
        content = self.client.get(reverse("rating")).content.decode()
        for marker in ("rank-num", "request-row--first", "rank-1", '<div class="metric">', "900"):
            self.assertNotIn(marker, content)
        self.assertIn('class="rating-region"', content)
        self.assertIn('data-i18n="region.sogd"', content)

    def test_shows_average_and_badges(self):
        from myapp.models import Achievement, UserAchievement

        Profile.objects.filter(user=self.people["rp_a"]).update(review_avg=4.5, review_count=2)
        UserAchievement.objects.create(user=self.people["rp_a"], achievement=Achievement.objects.get(code="first_help"))
        cache.clear()
        response = self.client.get(reverse("rating"))
        self.assertContains(response, "4.5")
        self.assertContains(response, "images/badges/first_help")

    def test_paginated_and_cached(self):
        for i in range(35):
            _make(f"rp_more{i}", is_volunteer=True)
        cache.clear()
        first = self.client.get(reverse("rating"))
        self.assertEqual(len(first.context["page_obj"]), 30)
        self.assertEqual(len(self.client.get(reverse("rating") + "?page=2").context["page_obj"]), 10)
        with CaptureQueriesContext(connection) as ctx:
            self.client.get(reverse("rating") + "?page=2")
        self.assertLessEqual(len(ctx.captured_queries), 3)

    def test_only_active_volunteers(self):
        Users.objects.filter(pk=self.people["rp_e"].pk).update(is_active=False)
        cache.clear()
        self.assertNotIn("Алишер", self._names(self.client.get(reverse("rating"))))


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class FlagStatsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client_user = _make("fs_client", is_client=True)
        self.vol = _make("fs_vol", is_volunteer=True)
        self.curator = _make("fs_curator", is_curator=True)
        self.f = analytics_v2.parse_filters({"days": "7"})

    def _task(self, help_type, region="dushanbe"):
        return HelpRequest.objects.create(client=self.client_user, volunteer=self.vol, status="active",
                                          help_type=help_type, description="d", address="a",
                                          phone="+992901234567", region=region)

    def _msg(self, task, flags, sender=None, staff=False):
        return ChatMessage.objects.create(task=task, sender=sender or self.vol, body="x", flags=flags,
                                          is_staff_post=staff)

    def test_topics_help_types_share_and_unreviewed(self):
        grocery, medical = self._task("grocery"), self._task("medical", region="sogd")
        self._msg(grocery, ["money"])
        self._msg(grocery, ["money", "phone"])
        reviewed = self._msg(medical, ["off_platform"])
        ChatMessage.objects.filter(pk=reviewed.pk).update(reviewed_at=timezone.now())
        self._msg(grocery, [])
        self._msg(grocery, [], sender=self.curator, staff=True)  # staff posts don't count
        topics = analytics_v2.chat_flags_topics(self.f)
        self.assertEqual(dict(zip(topics["labels"], topics["series"][0]["values"])),
                         {"sms_code": 0, "money": 2, "off_platform": 1, "phone": 1})
        self.assertEqual(topics["kpis"], {"flagged": 3, "messages": 4, "share": 75.0, "unreviewed": 2})
        by_type = analytics_v2.chat_flags_help_types(self.f)
        matrix = {s["key"]: dict(zip(by_type["labels"], s["values"])) for s in by_type["series"]}
        self.assertEqual((matrix["money"]["grocery"], matrix["phone"]["grocery"], matrix["off_platform"]["medical"]),
                         (2, 1, 1))
        sogd = analytics_v2.Filters(self.f.start_date, self.f.end_date, "sogd", 7)
        self.assertEqual(analytics_v2.chat_flags_topics(sogd)["kpis"]["flagged"], 1)

    def test_constant_queries(self):
        task = self._task("grocery")
        self._msg(task, ["money"])
        with CaptureQueriesContext(connection) as small:
            analytics_v2.chat_flags_topics(self.f)
            analytics_v2.chat_flags_help_types(self.f)
        for _ in range(20):
            self._msg(task, ["phone"])
        with CaptureQueriesContext(connection) as big:
            analytics_v2.chat_flags_topics(self.f)
            analytics_v2.chat_flags_help_types(self.f)
        self.assertEqual(len(small.captured_queries), len(big.captured_queries))

    def test_page_and_csv(self):
        self._msg(self._task("grocery"), ["money"])
        self.client.force_login(self.curator)
        page = self.client.get(reverse("crm_analytics") + "?days=7")
        self.assertContains(page, 'data-chart="chat_flags_topics"')
        self.assertContains(page, 'data-chart="chat_flags_help_types"')
        self.assertContains(page, 'data-i18n="an.flag_summary"')
        csv = self.client.get(reverse("crm_analytics_csv", args=["chat_flags_help_types"]) + "?days=7&lang=ru")
        header = csv.content.decode("utf-8-sig").splitlines()[0]
        self.assertEqual(header, "Категория,Код из СМС / ПИН,Деньги и переводы,Другой мессенджер,Номер телефона")
        self.assertNotIn("x", csv.content.decode("utf-8-sig").splitlines()[1].split(",")[0])
