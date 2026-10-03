"""Main Telegram bot (Feature Pack v2, milestone 5, feature 4).

The core (myapp/telegram/main_bot/core.py) is tested directly — no Telegram;
the python-telegram-bot adapter is tested offline with a fake HTTP layer.
Every action must go through the website's services, mutating buttons must be
idempotent, and nothing private may leak into bot messages or logs.
"""

import asyncio
import io
import json
import logging
import re
import shutil
import tempfile
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from PIL import Image

from accounts.models import Profile, Users
from myapp.models import (
    ChatMessage,
    Donation,
    EmergencyReport,
    HelpRequest,
    Notification,
    TaskReview,
    VolunteerApplication,
)
from myapp.services import notify as notify_service
from myapp.telegram import i18n
from myapp.telegram.main_bot import core
from myapp.telegram.main_bot.core import Event

PASSWORD = "Tg-Main-Tests-2026!"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
_MEDIA = tempfile.mkdtemp(prefix="gc-tg-tests-")


def tearDownModule():
    shutil.rmtree(_MEDIA, ignore_errors=True)


def _make(username, tg=None, **flags):
    user = Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD,
        region=flags.pop("region", "dushanbe"), telegram_id=tg, **flags,
    )
    Profile.objects.filter(user=user).update(full_name=username.title())
    return Users.objects.get(pk=user.pk)


def _buttons(reply):
    return [b for row in reply.buttons for b in row]


def _data(reply, prefix):
    return next(b.data for b in _buttons(reply) if b.data.startswith(prefix))


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA, SITE_URL="https://khayrkhoh.tj")
class BotBase(TestCase):
    def setUp(self):
        cache.clear()
        self.vol = _make("tg_vol", tg=1001, is_volunteer=True)
        self.client_user = _make("tg_client", tg=1002, is_client=True)
        self.curator = _make("tg_curator", tg=1003, is_curator=True)
        self.admin = _make("tg_admin", tg=1004, is_superuser=True, is_staff=True)
        self.other_vol = _make("tg_vol_sogd", tg=1005, is_volunteer=True, region="sogd")

    def say(self, user_or_chat, kind="text", **kw):
        chat_id = user_or_chat if isinstance(user_or_chat, int) else user_or_chat.telegram_id
        return core.handle(Event(chat_id=chat_id, kind=kind, **kw))

    def press(self, user, data):
        return self.say(user, "callback", data=data)

    def cmd(self, user, text):
        return self.say(user, "command", text=text)

    def task(self, **kw):
        defaults = dict(client=self.client_user, help_type="grocery", description="Хлеб и молоко",
                        address="ул. Рудаки 10", phone="+992901234567", region="dushanbe",
                        latitude=38.56, longitude=68.78)
        defaults.update(kw)
        return HelpRequest.objects.create(**defaults)

    def all_text(self, result):
        return "\n".join(r.text + " " + " ".join(b.label for b in _buttons(r)) for r in result.replies)


class LinkingTests(BotBase):
    def test_unlinked_chat_gets_instructions_only(self):
        result = self.cmd(5555, "/start")
        self.assertEqual(len(result.replies), 1)
        self.assertIn("/link", result.replies[0].text)
        self.assertIn("https://khayrkhoh.tj/profile/telegram/", result.replies[0].text)
        self.assertEqual(self.press(5555, "vt|0").replies[0].text, result.replies[0].text)

    def test_deep_link_start_code_links_the_chat_and_shows_the_menu(self):
        user = _make("tg_new", is_client=True)
        code = user.generate_telegram_link_token()
        result = self.cmd(7777, f"/start {code}")
        user.refresh_from_db()
        self.assertEqual(user.telegram_id, 7777)
        self.assertTrue(any(b.data == "cn" for b in _buttons(result.replies[-1])))

    def test_relink_tells_the_previous_chat(self):
        code = self.vol.generate_telegram_link_token()
        result = self.cmd(8888, f"/link {code}")
        self.assertEqual(result.previous_chat_notice, 1001)

    def test_groups_only_answer_id(self):
        self.assertIn("-100555", self.say(-100555, "command", text="/id", is_private=False).replies[0].text)
        self.assertEqual(self.say(-100555, "command", text="/start", is_private=False).replies, [])
        self.assertEqual(self.say(-100555, "text", text="hello", is_private=False).replies, [])

    def test_inactive_account_is_treated_as_unlinked(self):
        Users.objects.filter(pk=self.vol.pk).update(is_active=False)
        self.assertIn("/link", self.cmd(self.vol, "/start").replies[0].text)


class MenuAndCommonTests(BotBase):
    def test_role_menus(self):
        vol = {b.data for b in _buttons(self.cmd(self.vol, "/start").replies[0])}
        self.assertTrue({"vt|0", "my", "ach", "ntf", "lang"} <= vol)
        client = {b.data for b in _buttons(self.cmd(self.client_user, "/start").replies[0])}
        self.assertTrue({"cn", "cl|0"} <= client)
        self.assertFalse(client & {"vt|0", "so|0"})
        curator = {b.data for b in _buttons(self.cmd(self.curator, "/start").replies[0])}
        self.assertTrue({"so|0", "ss|0", "su|0", "se|0", "sdo|0", "sb"} <= curator)
        self.assertNotIn("sap|0", curator)  # applications: admin only
        admin = {b.data for b in _buttons(self.cmd(self.admin, "/start").replies[0])}
        self.assertIn("sap|0", admin)

    def test_role_checks_on_buttons(self):
        for data in ("so|0", "sap|0", "vt|0", "sb"):
            with self.subTest(data=data):
                self.assertEqual(self.press(self.client_user, data).toast, i18n.t("ru", "forbidden"))
        self.assertEqual(self.press(self.curator, "sap|0").toast, i18n.t("ru", "forbidden"))

    def test_language_switch_changes_texts(self):
        self.press(self.vol, "lang|tj")
        self.assertEqual(Profile.objects.get(user=self.vol).language, "tj")
        menu = self.cmd(self.vol, "/start").replies[0]
        self.assertIn("Дархостҳои наздик", " ".join(b.label for b in _buttons(menu)))

    def test_rate_limit_answers_once_then_stays_silent(self):
        for _ in range(core.RATE_LIMIT):
            self.cmd(self.vol, "/menu")
        first = self.cmd(self.vol, "/menu")
        self.assertEqual(first.replies[0].text, i18n.t("ru", "rate_limited"))
        self.assertEqual(self.cmd(self.vol, "/menu").replies, [])

    def test_notifications_list_and_mark_read(self):
        notify_service.notify([self.vol], "broadcast", params={"subject": "Субботник", "message": "x"})
        reply = self.press(self.vol, "ntf").replies[0]
        self.assertIn("Субботник", reply.text)
        self.press(self.vol, _data(reply, "ntfr"))
        self.assertFalse(Notification.objects.filter(recipient=self.vol, read_at__isnull=True).exists())

    def test_presence_is_marked_from_telegram(self):
        with mock.patch("myapp.services.presence.mark_seen") as seen:
            self.cmd(self.vol, "/start")
        seen.assert_called_once()
        self.assertEqual(seen.call_args.kwargs["source"], "telegram")

    def test_every_text_exists_in_three_languages(self):
        for key, entry in i18n.TEXTS.items():
            for lang in ("ru", "tj", "en"):
                self.assertTrue(entry.get(lang), f"{key} {lang}")
        self.assertEqual(i18n.t("ru", "missing.key"), "missing.key")
        self.assertIn("—", i18n.t("ru", "km"))  # missing placeholder never raises


class VolunteerTests(BotBase):
    def test_task_list_follows_the_website_visibility(self):
        mine = self.task()
        sogd = self.task(region="sogd")
        reply = self.press(self.vol, "vt|0").replies[0]
        data = {b.data for b in _buttons(reply)}
        self.assertIn(f"vd|{mine.pk}", data)
        self.assertNotIn(f"vd|{sogd.pk}", data)
        self.assertEqual(self.press(self.vol, f"vd|{sogd.pk}").replies[0].text, i18n.t("ru", "not_found"))

    def test_pending_card_hides_the_phone_until_accepted(self):
        task = self.task()
        text = self.press(self.vol, f"vd|{task.pk}").replies[0].text
        self.assertNotIn("+992901234567", text)
        self.assertIn("Рудаки", text)

    def test_accept_uses_the_service_and_a_double_tap_runs_once(self):
        task = self.task()
        accept = _data(self.press(self.vol, f"vd|{task.pk}").replies[0], "va|")
        with mock.patch("myapp.services.task_actions.accept", wraps=core.task_actions.accept) as svc:
            first = self.press(self.vol, accept)
            second = self.press(self.vol, accept)
        svc.assert_called_once()
        self.assertEqual(second.toast, i18n.t("ru", "already_done"))
        task.refresh_from_db()
        self.assertEqual((task.status, task.volunteer_id), ("active", self.vol.pk))
        self.assertIn("+992901234567", self.all_text(first))  # now the volunteer sees the phone

    def test_busy_and_taken(self):
        first, second = self.task(), self.task()
        self.press(self.vol, _data(self.press(self.vol, f"vd|{first.pk}").replies[0], "va|"))
        busy = self.press(self.vol, _data(self.press(self.vol, f"vd|{second.pk}").replies[0], "va|"))
        self.assertIn(i18n.t("ru", "v.accept.busy"), self.all_text(busy))
        other = _make("tg_vol2", tg=1006, is_volunteer=True)
        button = _data(self.press(other, f"vd|{second.pk}").replies[0], "va|")
        HelpRequest.objects.filter(pk=second.pk).update(status="active", volunteer=self.other_vol)
        self.assertEqual(self.press(other, button).replies[0].text, i18n.t("ru", "not_found"))

    def _active(self):
        task = self.task()
        self.press(self.vol, _data(self.press(self.vol, f"vd|{task.pk}").replies[0], "va|"))
        task.refresh_from_db()
        return task

    def test_stage_complete_and_points(self):
        task = self._active()
        my = self.press(self.vol, "my").replies[0]
        result = self.press(self.vol, _data(my, "vs|"))
        task.refresh_from_db()
        self.assertEqual(task.work_stage, "en_route")
        self.assertIn("в пути", result.toast)
        confirm = self.press(self.vol, f"vc|{task.pk}").replies[0]
        done = _data(confirm, "vcy|")
        self.press(self.vol, done)
        self.assertEqual(self.press(self.vol, done).toast, i18n.t("ru", "already_done"))
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertEqual(Profile.objects.get(user=self.vol).rating, 3)  # points awarded once

    def test_sos_once(self):
        task = self._active()
        confirm = self.press(self.vol, f"sos|{task.pk}").replies[0]
        send = _data(confirm, "sosy|")
        self.assertIn(i18n.t("ru", "v.sos_sent"), self.press(self.vol, send).replies[0].text)
        again = _data(self.press(self.vol, f"sos|{task.pk}").replies[0], "sosy|")
        self.assertIn(i18n.t("ru", "v.sos_exists"), self.press(self.vol, again).replies[0].text)
        self.assertEqual(EmergencyReport.objects.filter(help_request=task).count(), 1)

    def test_route_buttons_are_https_only(self):
        task = self._active()
        reply = self.press(self.vol, f"rt|{task.pk}").replies[0]
        urls = [b.url for b in _buttons(reply) if b.url]
        self.assertTrue(urls)
        self.assertTrue(all(u.startswith("https://") for u in urls))

    def test_location_needs_consent_then_feeds_tracking(self):
        task = self._active()
        reply = self.say(self.vol, "location", lat=38.561, lng=68.781).replies[0]
        self.assertEqual(reply.text, i18n.t("ru", "loc.consent"))
        self.press(self.vol, _data(reply, "lcons|"))
        with mock.patch("myapp.services.tracking.accept_point",
                        return_value=mock.Mock(ok=True, code="ok")) as accept:
            saved = self.say(self.vol, "location", lat=38.561, lng=68.781, accuracy=12)
            silent = self.say(self.vol, "location", lat=38.562, lng=68.782, live=True)
        self.assertEqual(accept.call_args_list[0].kwargs["source"], "telegram")
        self.assertEqual(accept.call_args_list[0].args[:2], (self.vol, task.pk))
        self.assertIn(i18n.t("ru", "loc.saved"), saved.replies[0].text)
        self.assertEqual(silent.replies, [])

    def test_achievements_page(self):
        text = self.press(self.vol, "ach").replies[0].text
        self.assertIn(i18n.t("ru", "ach.title"), text)
        self.assertIn("Баллы: 0", text)


class ClientTests(BotBase):
    def _wizard(self, phone_event):
        self.press(self.client_user, "cn")
        self.press(self.client_user, "ct|grocery")
        address_step = self.say(self.client_user, text="Купить хлеб и молоко").replies[0]
        self.assertTrue(address_step.request_location)
        self.say(self.client_user, "location", lat=38.5601, lng=68.7802)
        self.say(self.client_user, text="ул. Рудаки 10, подъезд 2")
        phone_event()
        prio = self.press(self.client_user, "cp|high").replies[0]
        return prio

    def test_full_wizard_creates_one_request_through_the_service(self):
        summary = self._wizard(lambda: self.say(self.client_user, "contact", contact_phone="992 90 123 45 67"))
        self.assertIn("+992901234567", summary.text)
        send = _data(summary, "cok|")
        with mock.patch("myapp.services.task_actions.create_request", wraps=core.task_actions.create_request) as svc:
            self.press(self.client_user, send)
            again = self.press(self.client_user, send)
        svc.assert_called_once()
        self.assertEqual(again.toast, i18n.t("ru", "already_done"))
        task = HelpRequest.objects.get(client=self.client_user)
        self.assertEqual((task.help_type, task.priority, task.phone, task.region), ("grocery", "high", "+992901234567", "dushanbe"))
        self.assertEqual((float(task.latitude), float(task.longitude)), (38.5601, 68.7802))
        self.assertTrue(task.is_urgent)

    def test_bad_phone_is_asked_again(self):
        self.press(self.client_user, "cn")
        self.press(self.client_user, "ct|other")
        self.say(self.client_user, text="Нужна помощь по дому")
        self.say(self.client_user, text="Сино 3")
        reply = self.say(self.client_user, text="12345").replies[0]
        self.assertEqual(reply.text, i18n.t("ru", "c.bad_phone"))
        self.assertTrue(reply.request_contact)

    def test_cancel_button_text_ends_the_wizard(self):
        self.press(self.client_user, "cn")
        self.press(self.client_user, "ct|other")
        self.say(self.client_user, text=i18n.t("ru", "kb.cancel"))
        self.assertIsNone(core.get_state(self.client_user.telegram_id))
        self.assertFalse(HelpRequest.objects.exists())

    def test_my_requests_detail_and_cancel(self):
        task = self.task()
        lst = self.press(self.client_user, "cl|0").replies[0]
        self.assertIn(f"cd|{task.pk}", {b.data for b in _buttons(lst)})
        detail = self.press(self.client_user, f"cd|{task.pk}").replies[0]
        confirm = self.press(self.client_user, _data(detail, "ccan|")).replies[0]
        self.press(self.client_user, _data(confirm, "ccany|"))
        task.refresh_from_db()
        self.assertEqual(task.status, "cancelled")
        other = self.task(client=_make("tg_client2", tg=1010, is_client=True))
        self.assertEqual(self.press(self.client_user, f"cd|{other.pk}").replies[0].text, i18n.t("ru", "not_found"))

    def test_client_sees_volunteer_public_info_and_eta_only(self):
        task = self.task(status="active", volunteer=self.vol, accepted_at=timezone.now())
        HelpRequest.objects.filter(pk=task.pk).update(volunteer_last_lat=38.57, volunteer_last_lng=68.79,
                                                      volunteer_last_at=timezone.now())
        Users.objects.filter(pk=self.vol.pk).update(email="vol-secret@example.com")
        text = self.press(self.client_user, f"cd|{task.pk}").replies[0].text
        self.assertIn("Tg_Vol", text)
        self.assertIn("км", text)
        self.assertNotIn("vol-secret", text)

    def test_review_flow(self):
        task = self.task(status="completed", volunteer=self.vol, accepted_at=timezone.now(), completed_at=timezone.now())
        start = self.press(self.client_user, f"rv|{task.pk}").replies[0]
        tags = self.press(self.client_user, _data(start, f"rs|{task.pk}|5")).replies[0]
        tags = self.press(self.client_user, f"rtg|{task.pk}|polite").replies[0]
        self.assertIn("✅", " ".join(b.label for b in _buttons(tags)))
        comment = self.press(self.client_user, f"rdone|{task.pk}").replies[0]
        self.assertIn("rskip", " ".join(b.data for b in _buttons(comment)))
        self.say(self.client_user, text="Спасибо большое!")
        review = TaskReview.objects.get(task=task)
        self.assertEqual((review.stars, review.tags, review.comment), (5, ["polite"], "Спасибо большое!"))


class ChatRelayTests(BotBase):
    def setUp(self):
        super().setUp()
        self.task_obj = self.task(status="active", volunteer=self.vol, accepted_at=timezone.now())

    def test_text_goes_to_the_task_chat_via_the_service(self):
        start = self.cmd(self.client_user, f"/chat_{self.task_obj.pk}").replies[0]
        self.assertIn(i18n.t("ru", "chat.compose"), start.text)
        self.say(self.client_user, text="Я дома, звоните")
        message = ChatMessage.objects.get(task=self.task_obj)
        self.assertEqual((message.sender_id, message.body), (self.client_user.pk, "Я дома, звоните"))

    def test_flags_apply_to_bot_messages_too(self):
        self.cmd(self.vol, f"/chat_{self.task_obj.pk}")
        self.say(self.vol, text="переведите деньги на карту")
        self.assertEqual(ChatMessage.objects.get(task=self.task_obj).flags, ["money"])

    def test_photo_is_stored_without_exif(self):
        buf = io.BytesIO()
        exif = Image.Exif()
        exif[0x010F] = "SecretCam"
        Image.new("RGB", (60, 40), (10, 200, 30)).save(buf, format="JPEG", exif=exif.tobytes())
        self.press(self.vol, f"ch|{self.task_obj.pk}")
        self.say(self.vol, "photo", photo=buf.getvalue(), text="чек")
        message = ChatMessage.objects.get(task=self.task_obj)
        self.assertEqual(message.body, "чек")
        with message.image.open("rb") as fh:
            self.assertNotIn(b"SecretCam", fh.read())

    def test_too_large_photo_and_closed_chat(self):
        self.press(self.vol, f"ch|{self.task_obj.pk}")
        self.assertEqual(self.say(self.vol, "photo", photo_too_large=True).replies[0].text,
                         i18n.t("ru", "chat.err.too_large"))
        outsider = _make("tg_outsider", tg=1020, is_client=True)
        self.assertEqual(self.cmd(outsider, f"/chat_{self.task_obj.pk}").replies[0].text, i18n.t("ru", "chat.closed"))

    def test_chat_notifications_to_telegram_carry_the_reply_command(self):
        sent = []
        with override_settings(TELEGRAM_BOT_TOKEN="123:TEST"), \
                mock.patch("myapp.tasks.send_telegram_task.delay", side_effect=lambda chat, text: sent.append((chat, text))):
            from myapp.services import chat

            chat.send_message(self.vol, self.task_obj, text="Я в пути")
        to_client = [text for chat_id, text in sent if chat_id == self.client_user.telegram_id]
        self.assertTrue(to_client)
        self.assertIn(f"/chat_{self.task_obj.pk}", to_client[0])


class StaffTests(BotBase):
    def test_lists(self):
        pending = self.task()
        HelpRequest.objects.filter(pk=pending.pk).update(created_at=timezone.now() - timedelta(days=3))
        overdue = self.task(status="active", volunteer=self.vol, accepted_at=timezone.now() - timedelta(hours=5))
        self.assertIn(f"sd|{overdue.pk}", {b.data for b in _buttons(self.press(self.curator, "so|0").replies[0])})
        self.assertIn(f"sd|{pending.pk}", {b.data for b in _buttons(self.press(self.curator, "ss|0").replies[0])})
        self.assertIn(f"sd|{pending.pk}", {b.data for b in _buttons(self.press(self.curator, "su|0").replies[0])})

    def test_recommend_and_assign_with_do_not_disturb_confirmation(self):
        task = self.task()
        Profile.objects.filter(user=self.vol).update(latitude=38.561, longitude=68.781, availability_status="available",
                                                     location_updated_at=timezone.now())
        recs = self.press(self.curator, f"sr|{task.pk}").replies[0]
        assign = _data(recs, f"sa|{task.pk}|{self.vol.pk}")
        Profile.objects.filter(user=self.vol).update(is_away=True)
        confirm = self.press(self.curator, assign).replies[0]
        self.assertIn("не беспокоить", confirm.text)
        self.press(self.curator, _data(confirm, "saf|"))
        task.refresh_from_db()
        self.assertEqual((task.status, task.volunteer_id), ("active", self.vol.pk))

    def test_sos_acknowledge_and_resolve(self):
        task = self.task(status="active", volunteer=self.vol, accepted_at=timezone.now())
        report = EmergencyReport.objects.create(help_request=task, volunteer=self.vol)
        card = self.press(self.curator, f"sed|{report.pk}").replies[0]
        self.press(self.curator, _data(card, "sea|"))
        report.refresh_from_db()
        self.assertEqual(report.status, "acknowledged")
        card = self.press(self.curator, f"sed|{report.pk}").replies[0]
        self.press(self.curator, _data(card, "ser|"))
        report.refresh_from_db()
        self.assertEqual(report.status, "resolved")

    def test_applications_admin_only(self):
        applicant = _make("tg_applicant", tg=1030)
        app = VolunteerApplication.objects.create(user=applicant, region="dushanbe", reason="Хочу помогать")
        card = self.press(self.admin, f"sapd|{app.pk}").replies[0]
        self.assertIn("Хочу помогать", card.text)
        self.press(self.admin, _data(card, "sapy|"))
        applicant.refresh_from_db()
        self.assertTrue(applicant.is_volunteer)
        self.assertEqual(self.press(self.curator, f"sapd|{app.pk}").toast, i18n.t("ru", "forbidden"))

    def test_donation_approval(self):
        offer = Donation.objects.create(donor=self.client_user, item_name="Мука", category="food", quantity=3)
        card = self.press(self.curator, f"sdod|{offer.pk}").replies[0]
        self.press(self.curator, _data(card, "sdoy|"))
        offer.refresh_from_db()
        self.assertEqual(offer.status, "approved")

    def test_broadcast_flow_sends_once(self):
        self.press(self.curator, "sb")
        self.press(self.curator, "sbr|dushanbe")
        self.say(self.curator, text="Субботник")
        confirm = self.say(self.curator, text="В субботу в 10:00 у парка").replies[0]
        send = _data(confirm, "sby|")
        with mock.patch("myapp.services.task_actions.send_broadcast", wraps=core.task_actions.send_broadcast) as svc:
            self.press(self.curator, send)
            self.press(self.curator, send)
        svc.assert_called_once()
        self.assertTrue(Notification.objects.filter(recipient=self.vol, type="broadcast").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.other_vol, type="broadcast").exists())


class SecurityTests(TestCase):
    def test_telegram_send_failure_never_logs_the_token(self):
        import requests

        from myapp.notifications import send_telegram_message

        error = requests.HTTPError("400 Client Error: Bad Request for url: https://api.telegram.org/bot123:SECRET/sendMessage")
        error.response = mock.Mock(status_code=400)
        with override_settings(TELEGRAM_BOT_TOKEN="123:SECRET"), \
                mock.patch("myapp.notifications.requests.post", side_effect=error), \
                self.assertLogs("myapp.notifications", level="WARNING") as logs:
            self.assertFalse(send_telegram_message(42, "hi"))
        self.assertNotIn("SECRET", "\n".join(logs.output))

    def test_telegram_text_is_html_escaped(self):
        from myapp.notifications import send_telegram_message

        with override_settings(TELEGRAM_BOT_TOKEN="123:T"), mock.patch("myapp.notifications.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            send_telegram_message(42, "люблю <3 & a<b>")
        self.assertEqual(post.call_args.kwargs["json"]["text"], "люблю &lt;3 &amp; a&lt;b&gt;")

    def test_api_client_never_logs_the_token(self):
        from myapp.telegram import api

        with mock.patch("myapp.telegram.api.requests.post") as post, \
                self.assertLogs("myapp.telegram.api", level="WARNING") as logs:
            post.return_value.status_code = 403
            post.return_value.json.return_value = {"ok": False, "error_code": 403}
            self.assertIsNone(api.call("999:TOPSECRET", "sendMessage", chat_id=1, text="x"))
        self.assertNotIn("TOPSECRET", "\n".join(logs.output))
        self.assertIsNone(api.call("", "sendMessage"))


# --------------------------------------------------------------------------- #
# python-telegram-bot adapter, offline
# --------------------------------------------------------------------------- #

class _FakeRequest:
    """telegram.request.BaseRequest stand-in: records calls, answers like the Bot API."""

    def __init__(self):
        from telegram.request import BaseRequest

        recorder = self

        class Fake(BaseRequest):
            @property
            def read_timeout(self):
                return 5

            async def initialize(self):
                pass

            async def shutdown(self):
                pass

            async def do_request(self, url, method, request_data=None, **kwargs):
                endpoint = url.rsplit("/", 1)[-1]
                params = request_data.parameters if request_data else {}
                recorder.calls.append((endpoint, params))
                if endpoint == "getMe":
                    result = {"id": 1, "is_bot": True, "first_name": "kk", "username": "kk_bot"}
                elif endpoint in ("answerCallbackQuery", "editMessageText"):
                    result = True if endpoint == "answerCallbackQuery" else {
                        "message_id": 9, "date": 0, "chat": {"id": params.get("chat_id", 1), "type": "private"}, "text": "x"}
                else:
                    result = {"message_id": 9, "date": 0, "chat": {"id": params.get("chat_id", 1), "type": "private"},
                              "text": params.get("text", "")}
                return 200, json.dumps({"ok": True, "result": result}).encode()

        self.calls = []
        self.request = Fake()


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class AdapterTests(TransactionTestCase):
    """The adapter runs core.handle in a worker thread (sync_to_async) with its own
    DB connection, so these are TransactionTestCase (committed data, like the WS
    tests) and run the whole PTB pipeline — Update parsing, handlers, rendering."""

    def setUp(self):
        cache.clear()
        self.user = _make("tg_adapter", tg=4242, is_volunteer=True)

    def _run(self, payload):
        from telegram import Update

        from myapp.telegram.main_bot import app as bot_app

        fake = _FakeRequest()

        async def go():
            application = bot_app.build("123:TEST", request=fake.request, get_updates_request=fake.request)
            async with application:
                await application.process_update(Update.de_json(payload, application.bot))

        asyncio.run(go())
        return fake.calls

    def test_start_renders_the_menu_with_an_inline_keyboard(self):
        calls = self._run({"update_id": 1, "message": {
            "message_id": 1, "date": 0, "chat": {"id": 4242, "type": "private"},
            "from": {"id": 4242, "is_bot": False, "first_name": "a"}, "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}]}})
        sends = [p for e, p in calls if e == "sendMessage"]
        self.assertEqual(len(sends), 1)
        markup = sends[0]["reply_markup"]
        markup = markup if isinstance(markup, dict) else json.loads(markup)
        data = [b.get("callback_data") for row in markup["inline_keyboard"] for b in row]
        self.assertIn("vt|0", data)

    def test_callback_is_answered_and_edits_in_place(self):
        calls = self._run({"update_id": 2, "callback_query": {
            "id": "q1", "chat_instance": "c", "data": "vt|0",
            "from": {"id": 4242, "is_bot": False, "first_name": "a"},
            "message": {"message_id": 5, "date": 0, "chat": {"id": 4242, "type": "private"}, "text": "menu"}}})
        endpoints = [e for e, _ in calls]
        self.assertIn("answerCallbackQuery", endpoints)
        self.assertIn("editMessageText", endpoints)

    def test_location_request_uses_a_reply_keyboard(self):
        from myapp.telegram.main_bot import app as bot_app

        reply = core.Reply("x", keyboard=core._kb("ru", "contact"))
        markup = bot_app._markup(reply).to_dict()
        self.assertTrue(markup["keyboard"][0][0]["request_contact"])
        self.assertEqual(markup["keyboard"][1][0]["text"], i18n.t("ru", "kb.cancel"))

    def test_live_location_edit_maps_to_a_live_event(self):
        from telegram import Bot, Update

        from myapp.telegram.main_bot import app as bot_app

        bot = Bot("123:TEST")
        update = Update.de_json({"update_id": 3, "edited_message": {
            "message_id": 6, "date": 0, "edit_date": 1, "chat": {"id": 4242, "type": "private"},
            "location": {"latitude": 38.5, "longitude": 68.7, "horizontal_accuracy": 15}}}, bot)
        event = bot_app.event_from_message(update)
        self.assertEqual((event.kind, event.live, event.lat, event.accuracy), ("location", True, 38.5, 15))


class TestRunSafetyTests(TestCase):
    def test_bot_tokens_are_blank_under_manage_py_test(self):
        """server/settings.py: a production token in a developer's .env can never
        be used by the test suite."""
        from django.conf import settings

        self.assertEqual(settings.TELEGRAM_BOT_TOKEN, "")
        self.assertEqual(settings.TELEGRAM_COMMUNITY_BOT_TOKEN, "")
