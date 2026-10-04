"""Client <-> volunteer chat (Feature Pack v2, feature 8): service, HTTP views,
images, notifications, WebSocket handlers and per-socket delivery guards.

WebSocket tests are TransactionTestCase (the consumer reads the DB from
another thread); everything else is a plain TestCase. Media goes to a temp dir.
"""

import io
from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from accounts.models import Profile, Users
from myapp.testing import TempMediaRoot
from myapp.models import ChatMessage, ChatReadState, ChatReport, HelpRequest, Notification
from myapp.realtime import consumers, events, publish
from myapp.services import access, chat, images, notify as notify_service, notify_texts
from myapp.tests_realtime import CLIENT_PHONE, _LayerMixin, _connect, _make, assert_no_pii
from myapp.tests_realtime_tracking import collect, subscribe, types_of

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
_MEDIA_ROOT = TempMediaRoot("gc-chat-tests-")  # fork-safe (myapp/testing.py)
_MEDIA = _MEDIA_ROOT.path


def tearDownModule():
    _MEDIA_ROOT.cleanup()


def _image_bytes(fmt="PNG", size=(400, 200), color=(200, 30, 30), exif=None):
    buf = io.BytesIO()
    img = Image.new("RGB", size, color)
    kwargs = {}
    if exif is not None:
        kwargs["exif"] = exif
    img.save(buf, format=fmt, **kwargs)
    return buf.getvalue()


def _upload(name="photo.png", fmt="PNG", **kw):
    content_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}[fmt]
    return SimpleUploadedFile(name, _image_bytes(fmt, **kw), content_type=content_type)


class _ChatFixture:
    def setUp(self):
        super().setUp()
        cache.clear()
        self.client_user = _make("chat_client", is_client=True)
        self.vol = _make("chat_vol", is_volunteer=True)
        self.other_vol = _make("chat_other_vol", is_volunteer=True)
        self.other_client = _make("chat_other_client", is_client=True)
        self.curator = _make("chat_curator", is_curator=True)
        self.admin = _make("chat_admin", is_superuser=True, is_staff=True)
        Profile.objects.filter(user=self.curator).update(full_name="Curator Realname")
        Profile.objects.filter(user=self.vol).update(full_name="Vol Fullname")
        # fresh objects: the ones above cache the profile row from before the update
        self.curator = Users.objects.get(pk=self.curator.pk)
        self.vol = Users.objects.get(pk=self.vol.pk)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.vol, status="active", help_type="grocery",
            description="d", address="Rudaki 10", phone=CLIENT_PHONE, region="dushanbe",
        )

    def fresh(self, task=None):
        return HelpRequest.objects.get(pk=(task or self.task).pk)

    def complete(self, days_ago=0):
        HelpRequest.objects.filter(pk=self.task.pk).update(
            status="completed", completed_at=timezone.now() - timedelta(days=days_ago))
        self.task = self.fresh()

    def send(self, user, text="hello", **kw):
        return chat.send_message(user, self.fresh(), text=text, **kw)


# --------------------------------------------------------------------------- #
# permissions
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class PermissionMatrixTests(_ChatFixture, TestCase):
    def test_active_task_matrix(self):
        task = self.fresh()
        for user, read, write in (
            (self.client_user, True, True), (self.vol, True, True), (self.curator, True, True),
            (self.admin, True, True), (self.other_vol, False, False), (self.other_client, False, False),
        ):
            with self.subTest(user=user.username):
                self.assertEqual(access.can_read_chat(user, task), read)
                self.assertEqual(access.can_write_chat(user, task), write)

    def test_pending_task_has_no_chat_even_for_a_volunteer_who_can_view_it(self):
        task = HelpRequest.objects.create(client=self.client_user, status="pending", help_type="grocery",
                                          description="d", region="dushanbe")
        self.assertTrue(access.can_view_task(self.other_vol, task))
        self.assertFalse(access.can_read_chat(self.other_vol, task))
        self.assertFalse(access.can_read_chat(self.client_user, task))
        self.client.force_login(self.other_vol)
        self.assertEqual(self.client.get(reverse("task_chat", args=[task.pk])).status_code, 403)
        self.assertEqual(chat.send_message(self.client_user, task, text="x").code, chat.FORBIDDEN)

    def test_completed_task_is_read_only(self):
        self.send(self.client_user, "before")
        self.complete()
        for user in (self.client_user, self.vol, self.curator):
            with self.subTest(user=user.username):
                self.assertTrue(access.can_read_chat(user, self.task))
                self.assertEqual(self.send(user, "after").code, chat.FORBIDDEN)
        self.client.force_login(self.client_user)
        page = self.client.get(reverse("task_chat", args=[self.task.pk]))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-can-write="0"')
        self.assertNotContains(page, "data-chat-form")
        response = self.client.post(reverse("task_chat_send", args=[self.task.pk]), {"text": "x"})
        self.assertEqual(response.status_code, 403)

    def test_archived_chat_parties_lose_access_staff_keep_it(self):
        self.send(self.client_user, "old")
        self.complete(days_ago=31)
        self.assertTrue(access.chat_archived(self.task))
        for user in (self.client_user, self.vol):
            self.assertFalse(access.can_read_chat(user, self.task))
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("task_chat", args=[self.task.pk])).status_code, 403)
            self.assertEqual(self.client.get(reverse("task_chat_messages", args=[self.task.pk])).status_code, 403)
        self.client.force_login(self.curator)
        response = self.client.get(reverse("task_chat_messages", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["archived"])
        self.assertEqual([m["body"] for m in response.json()["messages"]], ["old"])

    def test_unrelated_users_get_404_not_403(self):
        for user in (self.other_vol, self.other_client):
            self.client.force_login(user)
            for name in ("task_chat", "task_chat_messages"):
                self.assertEqual(self.client.get(reverse(name, args=[self.task.pk])).status_code, 404)
            for name in ("task_chat_send", "task_chat_read"):
                self.assertEqual(self.client.post(reverse(name, args=[self.task.pk]), {"text": "x", "last_id": 1}).status_code, 404)
        self.assertEqual(self.client.get(reverse("task_chat", args=[999999])).status_code, 404)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("task_chat", args=[self.task.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_mutations_require_post(self):
        self.client.force_login(self.client_user)
        for name in ("task_chat_send", "task_chat_upload", "task_chat_read"):
            self.assertEqual(self.client.get(reverse(name, args=[self.task.pk])).status_code, 405)
        message = self.send(self.vol).message
        for name in ("chat_message_report", "chat_message_hide"):
            self.assertEqual(self.client.get(reverse(name, args=[message.pk])).status_code, 405)

    def test_reassigned_volunteer_loses_the_chat(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(volunteer=self.other_vol)
        self.assertFalse(access.can_read_chat(self.vol, self.fresh()))
        self.assertTrue(access.can_read_chat(self.other_vol, self.fresh()))


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class SendTests(_ChatFixture, TestCase):
    def test_text_is_stripped_and_stored(self):
        outcome = self.send(self.client_user, "  hi there \n")
        self.assertTrue(outcome.ok and outcome.created)
        self.assertEqual(outcome.message.body, "hi there")
        self.assertEqual(outcome.message.kind, "text")
        self.assertFalse(outcome.message.is_staff_post)

    def test_empty_and_whitespace_rejected(self):
        for text in ("", "   ", "\n\t", None):
            self.assertEqual(self.send(self.client_user, text).code, chat.EMPTY)
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_length_limit(self):
        self.assertTrue(self.send(self.client_user, "a" * 1000).ok)
        self.assertEqual(self.send(self.client_user, "a" * 1001).code, chat.TOO_LONG)

    def test_non_string_text_rejected(self):
        self.assertEqual(chat.send_message(self.client_user, self.task, text=["x"]).code, chat.INVALID)

    def test_staff_posts_are_flagged(self):
        for staff in (self.curator, self.admin):
            outcome = self.send(staff, "from staff")
            self.assertTrue(outcome.message.is_staff_post)
        self.assertFalse(self.send(self.vol, "x").message.is_staff_post)

    def test_dedupe_by_client_msg_id(self):
        first = self.send(self.client_user, "once", client_msg_id="abc-123")
        again = self.send(self.client_user, "once", client_msg_id="abc-123")
        self.assertTrue(again.ok)
        self.assertFalse(again.created)
        self.assertEqual(first.message.pk, again.message.pk)
        self.assertEqual(ChatMessage.objects.count(), 1)
        # the same id from another sender is a different message
        self.assertTrue(self.send(self.vol, "once", client_msg_id="abc-123").created)

    def test_dedupe_survives_the_integrity_error_race(self):
        winner = self.send(self.client_user, "race", client_msg_id="race-1").message
        with mock.patch.object(chat, "_existing", side_effect=[None, winner]):
            outcome = self.send(self.client_user, "race", client_msg_id="race-1")
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.created)
        self.assertEqual(outcome.message.pk, winner.pk)
        self.assertEqual(ChatMessage.objects.filter(client_msg_id="race-1").count(), 1)

    def test_malformed_client_msg_id_rejected(self):
        self.assertEqual(self.send(self.client_user, "x", client_msg_id="a" * 65).code, chat.INVALID)
        self.assertEqual(self.send(self.client_user, "x", client_msg_id="bad id <>").code, chat.INVALID)

    def test_rate_limit_20_per_minute_per_task(self):
        for i in range(chat.RATE_LIMIT):
            self.assertTrue(self.send(self.client_user, f"m{i}").ok, i)
        self.assertEqual(self.send(self.client_user, "one too many").code, chat.RATE_LIMITED)
        # per user: the volunteer is unaffected
        self.assertTrue(self.send(self.vol, "still fine").ok)
        # a deduplicated retry doesn't count / isn't blocked
        dup = ChatMessage.objects.filter(sender=self.client_user).first()
        ChatMessage.objects.filter(pk=dup.pk).update(client_msg_id="kept")
        self.assertTrue(self.send(self.client_user, "x", client_msg_id="kept").ok)

    def test_http_send_json_and_form(self):
        self.client.force_login(self.client_user)
        url = reverse("task_chat_send", args=[self.task.pk])
        response = self.client.post(url, {"text": "via json", "client_msg_id": "h1"},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["message"]["mine"])
        self.assertEqual(data["message"]["body"], "via json")
        response = self.client.post(url, {"text": "via form"})
        self.assertRedirects(response, reverse("task_chat", args=[self.task.pk]), fetch_redirect_response=False)
        response = self.client.post(url, {"text": "x" * 1001}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "too_long"))


# --------------------------------------------------------------------------- #
# history, payloads, read state
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class HistoryTests(_ChatFixture, TestCase):
    def _fill(self, n):
        task = self.fresh()
        return [ChatMessage.objects.create(task=task, sender=self.client_user if i % 2 else self.vol, body=f"m{i}")
                for i in range(n)]

    def test_pagination_by_cursor(self):
        rows = self._fill(35)
        page = chat.history(self.client_user, self.fresh())
        self.assertEqual(len(page["messages"]), 30)
        self.assertTrue(page["has_more"])
        self.assertEqual([m["id"] for m in page["messages"]], [r.pk for r in rows[5:]])
        older = chat.history(self.client_user, self.fresh(), before_id=page["cursor"])
        self.assertEqual([m["id"] for m in older["messages"]], [r.pk for r in rows[:5]])
        self.assertFalse(older["has_more"])
        newer = chat.history(self.client_user, self.fresh(), after_id=rows[32].pk)
        self.assertEqual([m["id"] for m in newer["messages"]], [rows[33].pk, rows[34].pk])

    def test_history_view_and_query_count_bounded(self):
        self._fill(40)
        self.client.force_login(self.vol)
        url = reverse("task_chat_messages", args=[self.task.pk])
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["messages"]), 30)
        self.assertLess(len(ctx.captured_queries), 15)  # no per-message query
        cursor = response.json()["cursor"]
        self.assertEqual(len(self.client.get(url, {"before": cursor}).json()["messages"]), 10)

    def test_hidden_message_is_a_stub_for_parties_and_full_for_staff(self):
        message = self.send(self.vol, "rude words").message
        self.assertTrue(chat.hide(self.curator, message).ok)
        for party in (self.client_user, self.vol):
            [payload] = chat.history(party, self.fresh())["messages"]
            self.assertTrue(payload["hidden"])
            self.assertEqual(payload["body"], "")
            self.assertNotIn("rude", str(payload))
        [payload] = chat.history(self.curator, self.fresh())["messages"]
        self.assertTrue(payload["hidden"])
        self.assertEqual(payload["body"], "rude words")

    def test_staff_identity_hidden_from_non_staff(self):
        message = self.send(self.curator, "curator speaking").message
        for party in (self.client_user, self.vol):
            payload = chat.message_payload(message, party)
            self.assertEqual(payload["sender_display"], "Куратор")
            self.assertEqual(payload["label_key"], "chat.staff_label")
            self.assertEqual(payload["sender_role"], "staff")
            text = str(payload)
            self.assertNotIn("Curator Realname", text)
            self.assertNotIn("chat_curator", text)
        staff_view = chat.message_payload(message, self.admin)
        self.assertEqual(staff_view["sender_display"], "Curator Realname")
        self.assertTrue(chat.message_payload(message, self.curator)["mine"])

    def test_payload_shape_has_no_contact_details_or_internal_keys(self):
        Profile.objects.filter(user=self.client_user).update(full_name="")
        message = self.send(self.client_user, "hi").message
        for viewer in (self.client_user, self.vol, self.curator):
            payload = chat.message_payload(message, viewer)
            self.assertFalse([k for k in payload if k.startswith("_")])
            self.assertNotIn("sender_id", payload)
            assert_no_pii(self, payload, CLIENT_PHONE, self.client_user.email, self.vol.email)
        self.assertEqual(chat.message_payload(message, self.vol)["sender_display"], "chat_client")
        self.assertEqual(chat.message_payload(message, self.client_user)["sender_role"], "client")
        vol_msg = self.send(self.vol, "x").message
        self.assertEqual(chat.message_payload(vol_msg, self.client_user)["sender_display"], "Vol Fullname")

    def test_mark_read_is_monotonic_and_clamped(self):
        rows = self._fill(5)
        task = self.fresh()
        self.assertEqual(chat.mark_read(self.client_user, task, rows[3].pk), rows[3].pk)
        self.assertIsNone(chat.mark_read(self.client_user, task, rows[1].pk))
        self.assertEqual(ChatReadState.objects.get(task=task, user=self.client_user).last_read_id, rows[3].pk)
        self.assertEqual(chat.mark_read(self.client_user, task, 10 ** 9), rows[4].pk)  # clamped
        self.assertIsNone(chat.mark_read(self.other_vol, task, rows[4].pk))  # no access
        self.client.force_login(self.vol)
        response = self.client.post(reverse("task_chat_read", args=[task.pk]), {"last_id": rows[2].pk})
        self.assertEqual(response.json(), {"ok": True, "last_id": rows[2].pk})

    def test_unread_counts_one_query(self):
        task2 = HelpRequest.objects.create(client=self.client_user, volunteer=self.vol, status="active",
                                           help_type="grocery", description="d2", region="dushanbe")
        task3 = HelpRequest.objects.create(client=self.other_client, volunteer=self.other_vol, status="active",
                                           help_type="grocery", description="d3", region="dushanbe")
        t1 = self.fresh()
        a = ChatMessage.objects.create(task=t1, sender=self.vol, body="1")
        ChatMessage.objects.create(task=t1, sender=self.vol, body="2")
        ChatMessage.objects.create(task=t1, sender=self.client_user, body="own")  # own: not unread
        ChatMessage.objects.create(task=t1, sender=None, kind="system", body=chat.SYSTEM_CLOSED)
        hidden = ChatMessage.objects.create(task=t1, sender=self.vol, body="hidden")
        ChatMessage.objects.filter(pk=hidden.pk).update(hidden_at=timezone.now())
        for i in range(3):
            ChatMessage.objects.create(task=task2, sender=self.vol, body=f"t2-{i}")
        ChatMessage.objects.create(task=task3, sender=self.other_vol, body="not mine")
        ChatReadState.objects.create(task=t1, user=self.client_user, last_read_id=a.pk)
        with self.assertNumQueries(1):
            counts = chat.unread_counts(self.client_user, [t1.pk, task2.pk, task3.pk])
        self.assertEqual(counts, {t1.pk: 2, task2.pk: 3})  # "2" + system; task3 is not theirs
        staff_counts = chat.unread_counts(self.curator, [t1.pk, task3.pk])
        self.assertEqual(staff_counts, {t1.pk: 5, task3.pk: 1})  # staff: everything incl. hidden
        self.assertEqual(chat.unread_counts(self.client_user, []), {})

    def test_staff_page_shows_moderation_and_real_names(self):
        message = self.send(self.vol, "rude").message
        chat.hide(self.curator, message)
        self.send(self.curator, "note")
        self.client.force_login(self.admin)
        html = self.client.get(reverse("task_chat", args=[self.task.pk])).content.decode()
        self.assertIn(reverse("chat_message_hide", args=[message.pk]), html)
        self.assertIn('value="unhide"', html)
        self.assertIn("rude", html)
        self.assertIn("Curator Realname", html)
        self.client.force_login(self.client_user)
        html = self.client.get(reverse("task_chat", args=[self.task.pk])).content.decode()
        self.assertNotIn("rude", html)
        self.assertNotIn(reverse("chat_message_hide", args=[message.pk]), html)
        self.assertIn('data-i18n="chat.hidden_stub"', html)

    def test_chat_page_renders_messages_safely(self):
        self.send(self.vol, "<script>alert(1)</script> https://evil.example")
        self.send(self.curator, "curator note")
        self.client.force_login(self.client_user)
        page = self.client.get(reverse("task_chat", args=[self.task.pk]))
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('href="https://evil.example"', html)  # never auto-linked
        self.assertNotIn("Curator Realname", html)
        self.assertNotIn(CLIENT_PHONE, html)
        self.assertIn('data-i18n="chat.staff_label"', html)
        self.assertIn("js/chat.js", html)
        # The client gets only "Позвоните мне"; "Я в пути"/"Я на месте" are the volunteer's.
        self.assertIn('data-quick="chat.quick.call_me"', html)
        self.assertNotIn('data-quick="chat.quick.on_way"', html)
        self.assertNotIn('data-quick="chat.quick.arrived"', html)


# --------------------------------------------------------------------------- #
# moderation + lifecycle
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class ModerationTests(_ChatFixture, TestCase):
    def test_report_once_and_staff_notified_in_app(self):
        message = self.send(self.vol, "bad").message
        outcome = chat.report(self.client_user, message, "rude")
        self.assertEqual((outcome.ok, outcome.code), (True, chat.OK))
        self.assertEqual(chat.report(self.client_user, message, "again").code, chat.ALREADY)
        self.assertEqual(ChatReport.objects.filter(message=message).count(), 1)
        rows = Notification.objects.filter(type="chat_reported")
        self.assertEqual({n.recipient_id for n in rows}, {self.curator.pk, self.admin.pk})
        n = rows.first()
        self.assertEqual(n.params, {"task_id": self.task.pk, "message_id": message.pk})
        self.assertEqual(n.link, reverse("task_chat", args=[self.task.pk]))
        # a second reporter: one more report row, no second staff notification
        self.assertTrue(chat.report(self.curator, message).ok)
        self.assertEqual(Notification.objects.filter(type="chat_reported").count(), 2)

    def test_report_rules(self):
        own = self.send(self.client_user, "mine").message
        self.assertEqual(chat.report(self.client_user, own).code, chat.OWN_MESSAGE)
        other = self.send(self.vol, "x").message
        self.assertEqual(chat.report(self.other_vol, other).code, chat.FORBIDDEN)
        self.client.force_login(self.other_vol)
        self.assertEqual(self.client.post(reverse("chat_message_report", args=[other.pk])).status_code, 404)
        self.client.force_login(self.vol)
        response = self.client.post(reverse("chat_message_report", args=[own.pk]), {"reason": "spam"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json(), {"ok": True, "code": "ok"})

    def test_report_staff_notification_is_in_app_only_by_default(self):
        ntype = notify_service.NOTIFICATION_TYPES["chat_reported"]
        self.assertEqual(notify_service.default_channels(ntype, "curator"), frozenset())

    def test_hide_is_staff_only_and_logged(self):
        message = self.send(self.vol, "bad").message
        chat.report(self.client_user, message)
        self.assertEqual(chat.hide(self.client_user, message).code, chat.FORBIDDEN)
        self.assertEqual(chat.hide(self.vol, message).code, chat.FORBIDDEN)
        with self.assertLogs("myapp.services.chat", "INFO") as logs:
            self.assertTrue(chat.hide(self.curator, message).ok)
        self.assertIn(f"message={message.pk}", logs.output[0])
        self.assertNotIn("bad", logs.output[0])
        message.refresh_from_db()
        self.assertIsNotNone(message.hidden_at)
        self.assertEqual(message.hidden_by, self.curator)
        self.assertIsNotNone(ChatReport.objects.get(message=message).handled_at)
        self.assertTrue(chat.unhide(self.admin, message).ok)
        message.refresh_from_db()
        self.assertIsNone(message.hidden_at)

    def test_hide_views(self):
        message = self.send(self.vol, "bad").message
        url = reverse("chat_message_hide", args=[message.pk])
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 403)
        self.client.force_login(self.other_client)
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_login(self.curator)
        response = self.client.post(url, {"action": "hide"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["message"]["hidden"])
        self.assertEqual(response.json()["message"]["body"], "bad")  # staff still see it
        response = self.client.post(url, {"action": "unhide"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertFalse(response.json()["message"]["hidden"])

    def test_system_messages_cannot_be_hidden_or_reported(self):
        self.complete()
        system = chat.close(self.task)
        self.assertEqual(chat.hide(self.curator, system).code, chat.INVALID)
        self.assertEqual(chat.report(self.client_user, system).code, chat.INVALID)

    def test_close_is_idempotent(self):
        self.assertIsNone(chat.close(self.fresh()))  # still active
        self.complete()
        first = chat.close(self.task)
        self.assertEqual((first.kind, first.body, first.sender_id), ("system", chat.SYSTEM_CLOSED, None))
        self.assertIsNone(chat.close(self.task))
        self.assertIsNone(chat.close(self.fresh()))
        self.assertEqual(ChatMessage.objects.filter(task=self.task, kind="system").count(), 1)
        payload = chat.message_payload(first, self.client_user)
        self.assertEqual((payload["kind"], payload["label_key"], payload["sender_display"]),
                         ("system", "chat.system_label", ""))

    def test_close_cancelled_and_pending_without_volunteer(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(status="cancelled", cancelled_at=timezone.now())
        self.assertEqual(chat.close(self.fresh()).body, chat.SYSTEM_CANCELLED)
        lonely = HelpRequest.objects.create(client=self.client_user, status="cancelled", help_type="grocery",
                                            description="d", region="dushanbe")
        self.assertIsNone(chat.close(lonely))
        self.assertFalse(ChatMessage.objects.filter(task=lonely).exists())

    def test_close_never_raises(self):
        self.complete()
        with mock.patch.object(chat, "_close", side_effect=RuntimeError("db down")):
            with self.assertLogs("myapp.services.chat", "ERROR"):
                self.assertIsNone(chat.close(self.task))


# --------------------------------------------------------------------------- #
# images
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class ImageTests(_ChatFixture, TestCase):
    def test_validate_upload_codes(self):
        self.assertIsNone(images.validate_upload(_upload()))
        self.assertIsNone(images.validate_upload(_upload("a.jpg", "JPEG")))
        self.assertIsNone(images.validate_upload(_upload("a.webp", "WEBP")))
        self.assertEqual(images.validate_upload(_upload("a.gif", "GIF")), images.ERR_FORMAT)
        fake = SimpleUploadedFile("evil.png", b"<?php echo 1; ?>" * 10, content_type="image/png")
        self.assertEqual(images.validate_upload(fake), images.ERR_INVALID)
        truncated = SimpleUploadedFile("cut.png", _image_bytes()[:60], content_type="image/png")
        self.assertEqual(images.validate_upload(truncated), images.ERR_INVALID)
        big = SimpleUploadedFile("big.png", b"0" * (5 * 1024 * 1024 + 1), content_type="image/png")
        self.assertEqual(images.validate_upload(big), images.ERR_TOO_LARGE)

    def test_heic_only_when_a_decoder_is_registered(self):
        Image.init()
        self.assertEqual("HEIF" in images.allowed_formats(), "HEIF" in Image.OPEN)

    def test_upload_view_creates_image_message_with_webp_variants(self):
        self.client.force_login(self.client_user)
        url = reverse("task_chat_upload", args=[self.task.pk])
        response = self.client.post(url, {"image": _upload(size=(1600, 800)), "text": "caption",
                                          "client_msg_id": "img-1"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200, response.content)
        message = ChatMessage.objects.get()
        self.assertEqual((message.kind, message.body), ("image", "caption"))
        self.assertNotIn("photo", message.image.name)  # random, unguessable name
        message.refresh_from_db()  # variants made by the eager Celery task
        self.assertEqual(set(message.image_variants), {"thumb", "medium"})
        storage = message.image.storage
        with storage.open(message.image_variants["thumb"]) as fh, Image.open(fh) as thumb:
            self.assertEqual(thumb.format, "WEBP")
            self.assertEqual(thumb.size, (320, 160))
            self.assertFalse(thumb.info.get("exif"))
        with storage.open(message.image_variants["medium"]) as fh, Image.open(fh) as medium:
            self.assertEqual(medium.size, (960, 480))
        payload = chat.message_payload(message, self.vol)
        # Private: URLs go through the permission-checked view, never /media/.
        self.assertEqual(payload["thumb_url"], reverse("chat_message_image", args=[message.pk, "thumb"]))
        self.assertEqual(payload["image_url"], reverse("chat_message_image", args=[message.pk, "full"]))
        self.assertEqual(chat.image_storage_path(message, "thumb"), message.image_variants["thumb"])
        self.assertTrue(chat.image_storage_path(message, "full").endswith("_medium.webp"))

    def test_variants_never_upscale_and_fix_exif_orientation(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90° CW on display
        exif[0x010F] = "SecretCam"  # Make — must not survive
        message = ChatMessage.objects.create(task=self.task, sender=self.vol, kind="image")
        message.image.save("x.jpg", SimpleUploadedFile("x.jpg", _image_bytes("JPEG", size=(200, 100), exif=exif)))
        variants = images.make_webp_variants(message.image)
        with message.image.storage.open(variants["medium"]) as fh, Image.open(fh) as medium:
            self.assertEqual(medium.size, (100, 200))  # rotated, not upscaled
            self.assertNotIn(b"SecretCam", medium.info.get("exif", b"") or b"")
        with message.image.storage.open(variants["thumb"]) as fh:
            self.assertNotIn(b"SecretCam", fh.read())

    def test_upload_rejections(self):
        self.client.force_login(self.vol)
        url = reverse("task_chat_upload", args=[self.task.pk])
        ajax = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}
        big = SimpleUploadedFile("big.jpg", b"0" * (5 * 1024 * 1024 + 10), content_type="image/jpeg")
        self.assertEqual(self.client.post(url, {"image": big}, **ajax).status_code, 413)
        fake = SimpleUploadedFile("fake.jpg", b"not an image at all", content_type="image/jpeg")
        self.assertEqual(self.client.post(url, {"image": fake}, **ajax).json()["code"], "invalid_image")
        self.assertEqual(self.client.post(url, {"image": _upload("a.gif", "GIF")}, **ajax).status_code, 415)
        self.assertEqual(self.client.post(url, {}, **ajax).json()["code"], "empty")
        self.assertEqual(ChatMessage.objects.count(), 0)
        self.client.force_login(self.other_vol)
        self.assertEqual(self.client.post(url, {"image": _upload()}, **ajax).status_code, 404)

    def test_variant_failure_is_logged_not_raised(self):
        from myapp.tasks import make_chat_image_variants

        message = self.send(self.vol, "x").message  # no image → no-op
        self.assertIsNone(make_chat_image_variants(message.pk))
        with mock.patch.object(chat, "apply_image_variants", side_effect=OSError("broken")):
            with self.assertLogs("myapp.tasks", "WARNING"):
                self.assertIsNone(make_chat_image_variants(message.pk))

    def test_variants_publish_message_updated(self):
        with mock.patch.object(publish, "send_to_task") as sent:
            self.send(self.vol, None, image=_upload())
        kinds = [c.args[1] for c in sent.call_args_list]
        self.assertEqual(kinds, [events.CHAT_MESSAGE, events.CHAT_MESSAGE_UPDATED])
        self.assertTrue(sent.call_args_list[1].args[2]["thumb_url"])


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class ChatNotificationTests(_ChatFixture, TestCase):
    def _rows(self, user):
        return list(Notification.objects.filter(recipient=user, type="chat_message"))

    def test_other_party_notified_without_message_text(self):
        self.send(self.client_user, "secret body text")
        [n] = self._rows(self.vol)
        self.assertEqual(n.params, {"task_id": self.task.pk, "sender_display": "chat_client"})
        self.assertEqual(n.link, reverse("task_chat", args=[self.task.pk]))
        self.assertEqual((n.title_key, n.body_key), ("ntf.chat_message.title", "ntf.chat_message.body"))
        self.assertEqual(self._rows(self.client_user), [])  # not the sender
        self.assertFalse(Notification.objects.filter(type="chat_message", recipient=self.curator).exists())

    def test_throttled_until_read(self):
        first = self.send(self.client_user, "1").message
        self.send(self.client_user, "2")
        self.send(self.client_user, "3")
        self.assertEqual(len(self._rows(self.vol)), 1)
        # the volunteer read it, then left the chat (cursor older than 2 min)
        chat.mark_read(self.vol, self.fresh(), first.pk + 2)
        ChatReadState.objects.filter(user=self.vol).update(updated_at=timezone.now() - timedelta(minutes=5))
        self.send(self.client_user, "4")
        self.assertEqual(len(self._rows(self.vol)), 2)

    def test_reminder_after_the_throttle_window(self):
        self.send(self.client_user, "1")
        cache.delete(chat._throttle_key(self.task.pk, self.vol.pk))  # 10 min passed
        self.send(self.client_user, "2")
        self.assertEqual(len(self._rows(self.vol)), 2)

    def test_recipient_active_in_chat_is_skipped(self):
        m = self.send(self.vol, "hello").message
        chat.mark_read(self.client_user, self.fresh(), m.pk)  # client reading right now
        self.send(self.vol, "are you there?")
        self.assertEqual(len(self._rows(self.client_user)), 1)  # only the first one
        cache.clear()
        self.send(self.vol, "still?")  # still read recently and at the previous message? no: cursor behind
        self.assertEqual(len(self._rows(self.client_user)), 2)

    def test_staff_post_notifies_both_parties_without_identity(self):
        self.send(self.curator, "hello from staff")
        for party in (self.client_user, self.vol):
            [n] = self._rows(party)
            self.assertEqual(n.title_key, "ntf.chat_message.staff.title")
            self.assertNotIn("Curator", str(n.params))
            self.assertNotIn("chat_curator", str(n.params))

    @override_settings(TELEGRAM_BOT_TOKEN="t")
    def test_preview_only_in_telegram_text(self):
        self.vol.telegram_id = 12345
        self.vol.save(update_fields=["telegram_id"])
        with mock.patch.object(notify_service.TelegramChannel, "send") as tg:
            self.send(self.client_user, "please bring bread")
        _, _, text = tg.call_args.args
        self.assertIn("please bring bread", text)
        self.assertIn("chat_client", text)
        self.assertNotIn("bread", str(self._rows(self.vol)[0].params))

    def test_defaults(self):
        ntype = notify_service.NOTIFICATION_TYPES["chat_message"]
        self.assertEqual(notify_service.default_channels(ntype, "client"), frozenset({"telegram"}))


class NewNotificationTypeTests(TestCase):
    def test_registered_with_expected_defaults(self):
        expect = {
            "chat_reported": ({"admin", "curator"}, frozenset()),
            "review_low": ({"admin", "curator"}, frozenset({"email"})),
            "review_request": ({"client"}, frozenset({"telegram"})),
            "location_stale": ({"volunteer"}, frozenset({"telegram"})),
        }
        for code, (roles, defaults) in expect.items():
            ntype = notify_service.NOTIFICATION_TYPES[code]
            self.assertEqual(set(ntype.roles), roles)
            self.assertEqual(ntype.defaults, defaults)

    def test_texts_render_in_every_language(self):
        ctx = {"task_id": 7, "stars": 2, "volunteer_name": "Vol", "message_id": 9}
        for code in ("chat_reported", "review_low", "review_request", "location_stale"):
            for lang in notify_texts.LANGS:
                subject, body = notify_texts.render_channel(code, lang, ctx)
                self.assertTrue(subject and body)
                self.assertNotIn("{", body)
        _, body = notify_texts.render_channel("review_request", "ru", {"task_id": 7})
        self.assertEqual(body, "Оцените помощь волонтёра по запросу #7.")
        _, body = notify_texts.render_channel("chat_message", "en", {"task_id": 7, "sender_display": "A",
                                                                     "preview": None})
        self.assertIn("[photo]", body)


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #

def _chat_frames(frames):
    return [f for f in frames if f["type"].startswith("chat.")]


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class ChatSocketTests(_ChatFixture, _LayerMixin, TransactionTestCase):
    def _run(self, coro_fn):
        return async_to_sync(coro_fn)()

    def test_send_ack_and_per_viewer_mine(self):
        async def run():
            vol, _ = await _connect(self.vol)
            cli, _ = await _connect(self.client_user)
            for comm in (vol, cli):
                await subscribe(comm, self.task.pk)
            await vol.send_json_to({"type": "chat.send", "id": "cm-1",
                                    "payload": {"task_id": self.task.pk, "text": " hi ", "client_msg_id": "cm-1"}})
            vol_frames, cli_frames = await collect(vol), await collect(cli)
            for comm in (vol, cli):
                await comm.disconnect()
            return vol_frames, cli_frames
        vol_frames, cli_frames = self._run(run)
        message = ChatMessage.objects.get()
        ack = next(f for f in vol_frames if f["type"] == "ack")
        self.assertEqual(ack["payload"], {"for": "chat.send", "task_id": self.task.pk, "code": "ok",
                                          "client_msg_id": "cm-1", "id": message.pk})
        self.assertEqual(ack["id"], "cm-1")
        [mine] = _chat_frames(vol_frames)
        [theirs] = _chat_frames(cli_frames)
        self.assertEqual((mine["type"], mine["payload"]["mine"], mine["payload"]["body"]), ("chat.message", True, "hi"))
        self.assertFalse(theirs["payload"]["mine"])
        self.assertEqual(theirs["payload"]["sender_display"], "Vol Fullname")
        for frame in vol_frames + cli_frames:
            assert_no_pii(self, frame, CLIENT_PHONE, self.client_user.email, self.vol.email)
            self.assertFalse([k for k in frame.get("payload", {}) if str(k).startswith("_")])

    def test_send_errors(self):
        async def run():
            comm, _ = await _connect(self.other_vol)
            await comm.send_json_to({"type": "chat.send", "id": "x",
                                     "payload": {"task_id": self.task.pk, "text": "hi"}})
            forbidden = await comm.receive_json_from()
            await comm.send_json_to({"type": "chat.send", "payload": {"task_id": self.task.pk}})
            bad = await comm.receive_json_from()
            await comm.disconnect()
            cli, _ = await _connect(self.client_user)
            await cli.send_json_to({"type": "chat.send", "payload": {"task_id": self.task.pk, "text": "x" * 1001}})
            too_long = await cli.receive_json_from()
            await cli.send_json_to({"type": "chat.send", "payload": {"task_id": 999999, "text": "x"}})
            missing = await cli.receive_json_from()
            await cli.disconnect()
            return forbidden, bad, too_long, missing
        forbidden, bad, too_long, missing = self._run(run)
        self.assertEqual(forbidden["payload"], {"code": "forbidden", "ref": "x"})
        self.assertEqual(bad["payload"]["code"], "bad_request")
        self.assertEqual(too_long["payload"]["code"], "too_long")
        self.assertEqual(missing["payload"]["code"], "forbidden")  # no existence oracle
        self.assertEqual(ChatMessage.objects.count(), 0)

    def test_staff_identity_never_reaches_party_sockets(self):
        async def run():
            cur, _ = await _connect(self.curator)
            adm, _ = await _connect(self.admin)
            cli, _ = await _connect(self.client_user)
            vol, _ = await _connect(self.vol)
            for comm in (cur, adm, cli, vol):
                await subscribe(comm, self.task.pk)
            await cur.send_json_to({"type": "chat.typing", "payload": {"task_id": self.task.pk}})
            await cur.send_json_to({"type": "chat.send", "payload": {"task_id": self.task.pk, "text": "staff here"}})
            out = [await collect(c) for c in (cur, adm, cli, vol)]
            for comm in (cur, adm, cli, vol):
                await comm.disconnect()
            return out
        cur_f, adm_f, cli_f, vol_f = self._run(run)
        for frames in (cli_f, vol_f):
            chat_frames = _chat_frames(frames)
            self.assertEqual(types_of(chat_frames), ["chat.typing", "chat.message"])
            for frame in frames:
                text = str(frame)
                self.assertNotIn("Curator Realname", text)
                self.assertNotIn("chat_curator", text)
            self.assertEqual(chat_frames[0]["payload"]["sender_display"], "Куратор")
            self.assertEqual(chat_frames[1]["payload"]["sender_display"], "Куратор")
            self.assertEqual(chat_frames[1]["payload"]["label_key"], "chat.staff_label")
        [adm_typing, adm_msg] = _chat_frames(adm_f)
        self.assertEqual(adm_typing["payload"]["sender_display"], "Curator Realname")
        self.assertEqual(adm_msg["payload"]["sender_display"], "Curator Realname")
        # the typist's own socket gets no typing echo
        self.assertEqual(types_of(_chat_frames(cur_f)), ["chat.message"])
        self.assertTrue(_chat_frames(cur_f)[0]["payload"]["mine"])

    def test_typing_and_read_receipts(self):
        message = ChatMessage.objects.create(task=self.task, sender=self.vol, body="hi")

        async def run():
            vol, _ = await _connect(self.vol)
            cli, _ = await _connect(self.client_user)
            for comm in (vol, cli):
                await subscribe(comm, self.task.pk)
            await vol.send_json_to({"type": "chat.typing", "payload": {"task_id": self.task.pk}})
            await cli.send_json_to({"type": "chat.read", "id": "r",
                                    "payload": {"task_id": self.task.pk, "last_id": message.pk}})
            vol_frames, cli_frames = await collect(vol), await collect(cli)
            await cli.send_json_to({"type": "chat.typing", "payload": {"task_id": self.task.pk}})
            await cli.send_json_to({"type": "chat.typing", "payload": {"task_id": self.task.pk}})
            limited = await collect(cli)
            for comm in (vol, cli):
                await comm.disconnect()
            return vol_frames, cli_frames, limited
        vol_frames, cli_frames, limited = self._run(run)
        self.assertEqual(types_of(_chat_frames(vol_frames)), ["chat.read"])
        read = _chat_frames(vol_frames)[0]["payload"]
        self.assertEqual(read, {"task_id": self.task.pk, "user_display": "chat_client", "last_id": message.pk,
                                "mine": False})
        # two publishers (the volunteer's socket, the client's mark_read): no global order
        self.assertEqual(sorted(types_of(_chat_frames(cli_frames))), ["chat.read", "chat.typing"])
        typing = next(f for f in cli_frames if f["type"] == "chat.typing")
        self.assertEqual(typing["payload"], {"task_id": self.task.pk, "sender_display": "Vol Fullname"})
        ack = next(f for f in cli_frames if f["type"] == "ack")
        self.assertEqual(ack["payload"]["last_id"], message.pk)
        self.assertIn("rate_limited", [f["payload"].get("code") for f in limited if f["type"] == "error"])
        self.assertEqual(ChatReadState.objects.get(user=self.client_user).last_read_id, message.pk)

    def test_staff_read_receipt_is_staff_only(self):
        message = ChatMessage.objects.create(task=self.task, sender=self.vol, body="hi")

        async def run():
            cur, _ = await _connect(self.curator)
            adm, _ = await _connect(self.admin)
            cli, _ = await _connect(self.client_user)
            for comm in (cur, adm, cli):
                await subscribe(comm, self.task.pk)
            await cur.send_json_to({"type": "chat.read", "payload": {"task_id": self.task.pk, "last_id": message.pk}})
            out = await collect(adm), await collect(cli)
            for comm in (cur, adm, cli):
                await comm.disconnect()
            return out
        adm_f, cli_f = self._run(run)
        self.assertEqual(_chat_frames(cli_f), [])
        self.assertEqual(_chat_frames(adm_f)[0]["payload"]["user_display"], "Curator Realname")

    def test_unrelated_volunteer_subscribed_while_pending_never_gets_chat(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(status="pending", volunteer=None)

        async def run():
            other, _ = await _connect(self.other_vol)
            await subscribe(other, self.task.pk)  # allowed: a pending task in their region
            # the task gets taken (DB change only — no task.updated yet)
            await consumers.database_sync_to_async(
                lambda: HelpRequest.objects.filter(pk=self.task.pk).update(status="active", volunteer=self.vol))()
            await consumers.database_sync_to_async(lambda: self.send(self.client_user, "private"))()
            await consumers.database_sync_to_async(lambda: self.send(self.vol, "reply"))()
            frames = await collect(other)
            await other.disconnect()
            return frames
        frames = self._run(run)
        self.assertEqual(_chat_frames(frames), [])
        self.assertNotIn("private", str(frames))

    def test_archived_chat_stops_reaching_parties(self):
        message = ChatMessage.objects.create(task=self.task, sender=self.vol, body="hi")

        async def run():
            cli, _ = await _connect(self.client_user)
            cur, _ = await _connect(self.curator)
            for comm in (cli, cur):
                await subscribe(comm, self.task.pk)
            payload = await consumers.database_sync_to_async(lambda: chat.internal_payload(message))()
            await consumers.database_sync_to_async(
                lambda: publish.send_to_task(self.task.pk, events.CHAT_MESSAGE_UPDATED, payload))()
            before = await collect(cli)

            def archive():
                HelpRequest.objects.filter(pk=self.task.pk).update(
                    status="completed", completed_at=timezone.now() - timedelta(days=31))
                publish.task_changed(HelpRequest.objects.get(pk=self.task.pk))
                publish.send_to_task(self.task.pk, events.CHAT_MESSAGE_UPDATED, payload)
            await consumers.database_sync_to_async(archive)()
            after_cli, after_cur = await collect(cli), await collect(cur)
            for comm in (cli, cur):
                await comm.disconnect()
            return before, after_cli, after_cur
        before, after_cli, after_cur = self._run(run)
        self.assertEqual(types_of(_chat_frames(before)), ["chat.message_updated"])
        self.assertIn("task.updated", types_of(after_cli))
        self.assertEqual(_chat_frames(after_cli), [])
        # staff: the first event (not collected above) and the one after archival
        self.assertEqual(types_of(_chat_frames(after_cur)), ["chat.message_updated"] * 2)

    def test_reassigned_volunteer_stops_receiving(self):
        async def run():
            vol, _ = await _connect(self.vol)
            await subscribe(vol, self.task.pk)

            def reassign():
                HelpRequest.objects.filter(pk=self.task.pk).update(volunteer=self.other_vol)
                publish.task_changed(HelpRequest.objects.get(pk=self.task.pk))
                self.send(self.client_user, "for the new volunteer")
            await consumers.database_sync_to_async(reassign)()
            frames = await collect(vol)
            await vol.disconnect()
            return frames
        frames = self._run(run)
        self.assertEqual(_chat_frames(frames), [])
        self.assertIn("unsubscribed", types_of(frames))

    def test_hidden_event_and_stub_over_ws(self):
        message = ChatMessage.objects.create(task=self.task, sender=self.vol, body="rude")

        async def run():
            cli, _ = await _connect(self.client_user)
            cur, _ = await _connect(self.curator)
            for comm in (cli, cur):
                await subscribe(comm, self.task.pk)
            await consumers.database_sync_to_async(lambda: chat.hide(self.admin, message))()
            out = await collect(cli), await collect(cur)
            for comm in (cli, cur):
                await comm.disconnect()
            return out
        cli_f, cur_f = self._run(run)
        self.assertEqual(types_of(_chat_frames(cli_f)), ["chat.hidden", "chat.message_updated"])
        self.assertEqual(_chat_frames(cli_f)[0]["payload"], {"task_id": self.task.pk, "id": message.pk, "hidden": True})
        self.assertEqual(_chat_frames(cli_f)[1]["payload"]["body"], "")
        self.assertNotIn("rude", str(cli_f))
        self.assertEqual(_chat_frames(cur_f)[1]["payload"]["body"], "rude")

    def test_close_reaches_parties_as_system_message(self):
        async def run():
            cli, _ = await _connect(self.client_user)
            await subscribe(cli, self.task.pk)

            def finish():
                HelpRequest.objects.filter(pk=self.task.pk).update(status="completed", completed_at=timezone.now())
                chat.close(HelpRequest.objects.get(pk=self.task.pk))
            await consumers.database_sync_to_async(finish)()
            frames = await collect(cli)
            await cli.disconnect()
            return frames
        [frame] = _chat_frames(self._run(run))
        self.assertEqual((frame["payload"]["kind"], frame["payload"]["body"]), ("system", "chat.system.closed"))

    def test_every_chat_event_type_is_guarded_and_personalised(self):
        for event_type in events.CHAT_EVENTS:
            self.assertIs(consumers.DELIVERY_GUARDS[event_type], consumers.CHAT_GUARD)
            self.assertIn(event_type, consumers.DELIVERY_TRANSFORMS)
        self.assertEqual(consumers.FUTURE_TYPES, set())


class ForViewerTests(TestCase):
    def test_strips_private_keys_and_applies_staff_overrides(self):
        payload = events.chat_internal({"task_id": 1, "sender_display": "Куратор"}, sender_id=5,
                                       staff={"sender_display": "Real"}, skip_self=True)
        self.assertEqual(events.for_viewer(payload, 9, False), {"task_id": 1, "sender_display": "Куратор"})
        self.assertEqual(events.for_viewer(payload, 9, True), {"task_id": 1, "sender_display": "Real"})
        self.assertIsNone(events.for_viewer(payload, 5, True))  # skip_self
        staff_only = events.chat_internal({"task_id": 1}, sender_id=5, staff_only=True)
        self.assertIsNone(events.for_viewer(staff_only, 9, False))
        self.assertIsNotNone(events.for_viewer(staff_only, 9, True))


class ChatUiStringsTests(TestCase):
    """Every chat.* key the template / chat.js use has EN/RU/TJ text."""

    def test_keys_complete_in_every_language(self):
        import re
        from pathlib import Path

        from django.conf import settings

        base = Path(settings.BASE_DIR)
        used = set(re.findall(r'data-i18n(?:-ph)?="(chat\.[a-z0-9_.]+)"',
                              (base / "templates/myapp/chat.html").read_text(encoding="utf-8")))
        used |= set(re.findall(r"""t\(\s*'(chat\.[a-z0-9_.]+)'""",
                               (base / "static/js/chat.js").read_text(encoding="utf-8")))
        used |= {f"chat.error.{code}" for code in ("empty", "too_long", "rate_limited", "forbidden", "invalid",
                                                    "too_large", "invalid_image", "unsupported_format")}
        used |= {chat.SYSTEM_CLOSED, chat.SYSTEM_CANCELLED, events.CHAT_STAFF_LABEL_KEY, events.CHAT_SYSTEM_LABEL_KEY}
        self.assertFalse(used - set(chat.UI_STRINGS), used - set(chat.UI_STRINGS))
        for lang in ("en", "ru", "tj"):
            self.assertEqual(set(chat.ui_i18n_keys(lang)), set(chat.UI_STRINGS))
            self.assertTrue(all(chat.ui_i18n_keys(lang).values()))
        self.assertEqual(chat.UI_STRINGS["chat.staff_label"]["ru"], chat.STAFF_LABEL)
