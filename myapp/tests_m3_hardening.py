"""Milestone 3 follow-ups (owner review, 2026-10-02):

1. quiet safety flags on chat messages (money / SMS codes / other messengers /
   phone numbers) → in-app-only staff notice, the message is never blocked;
2. chat photo originals stored without EXIF / GPS / XMP (not only the thumbs);
3. the comment of a 1–2 star review is for coordinators, not the volunteer;
4. ``check_realtime`` — the channel-layer self-check used at deploy.
"""

import io
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image
import requests

from accounts.models import Profile, Users
from myapp.testing import TempMediaRoot
from myapp.models import ChatMessage, HelpRequest, Notification
from myapp.realtime import events
from myapp.services import chat, chat_flags, feedback
from myapp.services import notify as notify_service

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "M3-Hardening-2026!"
_MEDIA_ROOT = TempMediaRoot("gc-m3h-tests-")  # fork-safe (myapp/testing.py)
_MEDIA = _MEDIA_ROOT.path


def tearDownModule():
    _MEDIA_ROOT.cleanup()


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


# --------------------------------------------------------------------------- #
# 1. detector (pure)
# --------------------------------------------------------------------------- #

class ChatFlagDetectorTests(SimpleTestCase):
    def assertFlags(self, text, expected):
        self.assertEqual(chat_flags.detect(text), expected, text)

    def test_ordinary_conversation_is_not_flagged(self):
        for text in (
            "Я в пути", "Я на месте", "Позвоните мне", "Здравствуйте! Буду через 20 минут.",
            "Посмотрите на карте, я у второго подъезда", "Встреча 02.10.2026 в 14:00",
            "Дом 12, кв 45, подъезд 3, этаж 5", "Код домофона 45", "Принесу продукты к 15:30",
            "Ман дар роҳ ҳастам", "Ташаккур!", "Thank you, see you tomorrow", "", "   ",
        ):
            with self.subTest(text=text):
                self.assertFlags(text, [])

    def test_sms_codes_and_pins(self):
        for text in ("Скажите код из смс", "продиктуйте смс-код", "какой код пришёл в сообщении?",
                     "назовите код подтверждения", "нужен ПИН-код", "send me the code from the SMS",
                     "CVV на обороте", "Рамзро гӯед", "коди аз SMS-ро гӯед"):
            with self.subTest(text=text):
                self.assertIn(chat_flags.SMS_CODE, chat_flags.detect(text))

    def test_money_transfers_and_cards(self):
        for text in ("Переведите 200 сомони", "скиньте денег на карту", "дайте в долг",
                     "номер карты скажите", "оплатите заранее", "пулро интиқол диҳед",
                     "Корти миллиро диҳед", "Карта 4444 5555 6666 7777", "send money please", "50$"):
            with self.subTest(text=text):
                self.assertIn(chat_flags.MONEY, chat_flags.detect(text))

    def test_other_messengers(self):
        for text in ("напишите мне в WhatsApp", "давайте в ватсап", "пишите в телеграм",
                     "мой ник @ivan_petrov", "есть вайбер?", "t.me/somebody", "лучше в личку"):
            with self.subTest(text=text):
                self.assertIn(chat_flags.OFF_PLATFORM, chat_flags.detect(text))

    def test_phone_numbers(self):
        for text in ("мой номер +992 90 123 45 67", "901234567", "(992) 93-555-44-33", "992 900 11 22 33"):
            with self.subTest(text=text):
                self.assertEqual(chat_flags.detect(text), [chat_flags.PHONE])

    def test_a_card_number_is_money_not_phone(self):
        self.assertEqual(chat_flags.detect("4444-5555-6666-7777"), [chat_flags.MONEY])

    def test_categories_come_in_priority_order(self):
        self.assertEqual(
            chat_flags.detect("Переведите деньги и скажите код из смс в WhatsApp +992901234567"),
            [chat_flags.SMS_CODE, chat_flags.MONEY, chat_flags.OFF_PLATFORM, chat_flags.PHONE],
        )

    def test_non_string_is_safe(self):
        self.assertEqual(chat_flags.detect(None), [])
        self.assertEqual(chat_flags.detect(123), [])


# --------------------------------------------------------------------------- #
# 1. flags in the chat flow
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="123:abc", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class ChatFlagFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        # The fake token used to reach the live Bot API (which answered 401):
        # the same answer now comes from a mock, so the tests never touch the
        # network and don't depend on it.
        rejected = mock.Mock(status_code=401)
        rejected.raise_for_status.side_effect = requests.HTTPError(response=rejected)
        patcher = mock.patch("myapp.notifications.requests.post", return_value=rejected)
        self.telegram_post = patcher.start()
        self.addCleanup(patcher.stop)
        self.client_user = _make("fl_client", is_client=True, telegram_id="111")
        self.vol = _make("fl_vol", is_volunteer=True, telegram_id="222")
        self.curator = _make("fl_curator", is_curator=True, telegram_id="333")
        self.admin = _make("fl_admin", is_superuser=True, is_staff=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.vol, status="active", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )

    def _staff_flags(self):
        return Notification.objects.filter(type="chat_flagged")

    def test_ordinary_message_reaches_no_staff(self):
        outcome = chat.send_message(self.vol, self.task, text="Я в пути, буду через 10 минут")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message.flags, [])
        self.assertFalse(self._staff_flags().exists())
        self.assertFalse(Notification.objects.filter(recipient__in=[self.curator, self.admin]).exists())

    def test_flagged_message_is_delivered_and_staff_get_one_quiet_notice(self):
        mail.outbox = []  # welcome mails from user creation
        with mock.patch("myapp.tasks.send_telegram_task.delay") as tg, \
                mock.patch("myapp.tasks.send_email_task.delay") as em:
            outcome = chat.send_message(self.vol, self.task, text="Переведите 500 сомони на карту")
        self.assertTrue(outcome.ok)
        message = ChatMessage.objects.get(pk=outcome.message.pk)
        self.assertEqual(message.body, "Переведите 500 сомони на карту")  # not blocked, not altered
        self.assertEqual(message.flags, ["money"])
        rows = self._staff_flags()
        self.assertEqual(set(rows.values_list("recipient_id", flat=True)), {self.curator.pk, self.admin.pk})
        row = rows.first()
        self.assertEqual(row.body_key, "ntf.chat_flagged.money.body")
        self.assertEqual(row.link, reverse("task_chat", args=[self.task.pk]))
        self.assertNotIn("сомони", str(row.params))  # never the text itself
        # quiet: no email / Telegram to staff (in-app only by default)
        staff_chats = {"333"}
        sent_to = {c.args[0] for c in tg.call_args_list}
        self.assertFalse(staff_chats & sent_to)
        self.assertFalse(any(c.args[0] in (self.curator.email, self.admin.email) for c in em.call_args_list))
        self.assertFalse([m for m in mail.outbox if set(m.to) & {self.curator.email, self.admin.email}])

    def test_one_notice_per_task_category_and_day(self):
        chat.send_message(self.client_user, self.task, text="мой номер 901234567")
        chat.send_message(self.client_user, self.task, text="ещё раз: 93 555 44 33")
        chat.send_message(self.vol, self.task, text="напишите в WhatsApp")
        per_curator = self._staff_flags().filter(recipient=self.curator)
        self.assertEqual(sorted(per_curator.values_list("body_key", flat=True)),
                         ["ntf.chat_flagged.off_platform.body", "ntf.chat_flagged.phone.body"])
        # every message still carries its own flags
        self.assertEqual(ChatMessage.objects.filter(task=self.task).exclude(flags=[]).count(), 3)

    def test_staff_posts_are_never_flagged(self):
        outcome = chat.send_message(self.curator, self.task, text="Не переводите деньги, звоните 901234567")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message.flags, [])
        self.assertFalse(self._staff_flags().exists())

    def test_flags_reach_staff_payload_only(self):
        message = chat.send_message(self.vol, self.task, text="код из смс скажите").message
        staff_view = chat.message_payload(message, self.curator)
        self.assertEqual(staff_view["flags"], ["sms_code"])
        for viewer in (self.client_user, self.vol):
            with self.subTest(viewer=viewer.username):
                self.assertNotIn("flags", chat.message_payload(message, viewer))
        internal = chat.internal_payload(message)
        self.assertNotIn("flags", {k: v for k, v in internal.items() if not k.startswith("_")})
        self.assertNotIn("flags", events.for_viewer(internal, self.client_user.pk, False))

    def test_history_shows_flags_to_staff_only(self):
        chat.send_message(self.client_user, self.task, text="давайте в телеграм")
        staff = chat.history(self.curator, self.task)
        party = chat.history(self.vol, self.task)
        self.assertEqual(staff["messages"][-1]["flags"], ["off_platform"])
        self.assertNotIn("flags", party["messages"][-1])

    def test_notice_type_is_quiet_by_default(self):
        ntype = notify_service.NOTIFICATION_TYPES["chat_flagged"]
        self.assertEqual(ntype.defaults, frozenset())
        self.assertFalse(ntype.forced)
        self.assertEqual(set(ntype.roles), {"admin", "curator"})

    def test_a_notify_failure_never_breaks_sending(self):
        real = notify_service.notify

        def flaky(recipients, type, **kw):
            if type == "chat_flagged":
                raise RuntimeError("boom")
            return real(recipients, type, **kw)

        with mock.patch("myapp.services.notify.notify", side_effect=flaky):
            outcome = chat.send_message(self.vol, self.task, text="одолжите денег")
        self.assertTrue(outcome.ok)
        self.assertEqual(ChatMessage.objects.get(pk=outcome.message.pk).flags, ["money"])


# --------------------------------------------------------------------------- #
# 2. EXIF / GPS stripped from the stored original
# --------------------------------------------------------------------------- #

def _photo(fmt="JPEG", size=(400, 200), orientation=6):
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "SecretCam"
    exif[0x8825] = {1: "N", 2: (38.0, 33.0, 12.0), 3: "E", 4: (68.0, 46.0, 30.0)}
    buf = io.BytesIO()
    extra = {"xmp": b"<x:xmpmeta>SecretXMP</x:xmpmeta>"} if fmt == "JPEG" else {}
    Image.new("RGB", size, (20, 120, 200)).save(buf, format=fmt, exif=exif.tobytes(), **extra)
    return buf.getvalue()


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM, MEDIA_ROOT=_MEDIA)
class ChatOriginalMetadataTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client_user = _make("ex_client", is_client=True)
        self.vol = _make("ex_vol", is_volunteer=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.vol, status="active", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )

    def _send(self, fmt, name):
        upload = SimpleUploadedFile(name, _photo(fmt), content_type=f"image/{fmt.lower()}")
        outcome = chat.send_message(self.vol, self.task, image=upload)
        self.assertTrue(outcome.ok, outcome.code)
        return ChatMessage.objects.get(pk=outcome.message.pk)

    def test_stored_original_has_no_exif_gps_or_xmp_for_every_format(self):
        for fmt, name, ext in (("JPEG", "p.jpg", ".jpg"), ("PNG", "p.png", ".png"), ("WEBP", "p.webp", ".webp")):
            with self.subTest(fmt=fmt):
                message = self._send(fmt, name)
                self.assertTrue(message.image.name.endswith(ext))
                with message.image.open("rb") as fh:
                    raw = fh.read()
                for secret in (b"SecretCam", b"SecretXMP", b"GPS"):
                    self.assertNotIn(secret, raw)
                stored = Image.open(io.BytesIO(raw))
                self.assertEqual(dict(stored.getexif()), {})
                self.assertNotIn("exif", stored.info)
                self.assertNotIn("xmp", stored.info)
                # orientation 6 was applied to the pixels: 400×200 → 200×400
                self.assertEqual(stored.size, (200, 400))

    def test_strip_metadata_keeps_the_upload_readable_afterwards(self):
        from myapp.services import images

        upload = io.BytesIO(_photo("JPEG"))
        content, ext = images.strip_metadata(upload)
        self.assertEqual(ext, "jpg")
        self.assertEqual(upload.tell(), 0)
        self.assertGreater(len(content.read()), 0)

    def test_a_codec_failure_is_reported_not_raised(self):
        upload = SimpleUploadedFile("p.jpg", _photo("JPEG"), content_type="image/jpeg")
        with mock.patch("myapp.services.images.strip_metadata", side_effect=OSError("codec")):
            outcome = chat.send_message(self.vol, self.task, image=upload)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "invalid_image")
        self.assertFalse(ChatMessage.objects.filter(task=self.task).exists())


# --------------------------------------------------------------------------- #
# 3. low-rating comments are for coordinators
# --------------------------------------------------------------------------- #

@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class LowRatingCommentTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = _make("lr_admin", is_superuser=True, is_staff=True)
        self.curator = _make("lr_curator", is_curator=True)
        self.volunteer = _make("lr_vol", is_volunteer=True)
        self.client_user = _make("lr_client", is_client=True)
        self.low = self._review(2, "Опоздал на два часа")
        self.high = self._review(4, "Спасибо, всё хорошо")

    def _review(self, stars, comment):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="d", address="a",
            phone="+992901234567", region="dushanbe",
        )
        HelpRequest.objects.filter(pk=task.pk).update(status="completed", volunteer=self.volunteer)
        task.refresh_from_db()
        outcome = feedback.submit_review(self.client_user, task, stars=stars, comment=comment, tags=["polite"])
        self.assertTrue(outcome.ok)
        return outcome.review

    def test_can_see_comment_matrix(self):
        self.assertFalse(feedback.can_see_comment(self.volunteer, self.low))
        self.assertTrue(feedback.can_see_comment(self.volunteer, self.high))
        for user in (self.curator, self.admin, self.client_user):
            with self.subTest(user=user.username):
                self.assertTrue(feedback.can_see_comment(user, self.low))
        self.assertFalse(feedback.can_see_comment(None, self.high))

    def test_my_reviews_hides_the_low_comment_but_keeps_stars_and_tags(self):
        self.client.force_login(self.volunteer)
        response = self.client.get(reverse("my_reviews"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Спасибо, всё хорошо")
        self.assertNotContains(response, "Опоздал на два часа")
        self.assertContains(response, 'data-i18n="review.comment_staff_only"', count=1)
        self.assertContains(response, 'aria-label="2', html=False)
        self.assertContains(response, 'data-i18n="review.tag.polite"', count=2)

    def test_task_review_page_for_the_volunteer(self):
        self.client.force_login(self.volunteer)
        response = self.client.get(reverse("task_review", args=[self.low.task_id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Опоздал на два часа")
        self.assertContains(response, "review.comment_staff_only")

    def test_task_detail_for_the_volunteer(self):
        self.client.force_login(self.volunteer)
        response = self.client.get(reverse("task_detail", args=[self.low.task_id]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Опоздал на два часа")

    def test_staff_and_author_still_read_it(self):
        for user in (self.curator, self.admin, self.client_user):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("task_review", args=[self.low.task_id]))
                self.assertContains(response, "Опоздал на два часа")
        self.client.force_login(self.curator)
        self.assertContains(self.client.get(reverse("crm_reviews")), "Опоздал на два часа")

    def test_raising_the_rating_reveals_the_comment(self):
        feedback.submit_review(self.client_user, self.low.task, stars=3, comment=self.low.comment, tags=[])
        self.low.refresh_from_db()
        self.assertTrue(feedback.can_see_comment(self.volunteer, self.low))

    def test_review_received_notice_never_carries_the_comment(self):
        row = Notification.objects.filter(type="review_received", recipient=self.volunteer).first()
        self.assertIsNotNone(row)
        self.assertNotIn("Опоздал", str(row.params))


# --------------------------------------------------------------------------- #
# 4. check_realtime
# --------------------------------------------------------------------------- #

class CheckRealtimeCommandTests(SimpleTestCase):
    def test_in_memory_layer_fails_unless_allowed(self):
        with self.assertRaises(CommandError):
            call_command("check_realtime", stdout=io.StringIO())
        out = io.StringIO()
        call_command("check_realtime", "--allow-memory", stdout=out)
        self.assertIn("InMemoryChannelLayer", out.getvalue())
        self.assertIn("round trip: OK", out.getvalue())

    def test_password_is_masked(self):
        from myapp.management.commands.check_realtime import _masked

        self.assertEqual(_masked("redis://:s3cret@redis:6379/2"), "redis://:***@redis:6379/2")
        self.assertEqual(_masked("redis://user:s3cret@redis:6379/2"), "redis://user:***@redis:6379/2")
        self.assertEqual(_masked("redis://redis:6379/2"), "redis://redis:6379/2")
