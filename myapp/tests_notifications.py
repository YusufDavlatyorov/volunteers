"""Typed notifications (Feature Pack v2, feature 11): notify() fan-out,
preferences, language rendering, dedupe, DND, endpoints, poll fallback, prune."""

import json
import re
import string
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import HelpRequest, Notification, NotificationPreference
from myapp.services import notify as notify_service
from myapp.services import notify_texts
from myapp.services.notify import notify

PASSWORD = "Notify-Tests-2026!"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


def _make(username, region="dushanbe", **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region=region, **flags
    )


def _set_lang(user, lang):
    Profile.objects.filter(user=user).update(language=lang)


@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
class NotifyFanOutTests(TestCase):
    def setUp(self):
        self.a = _make("nf_a", is_volunteer=True)
        self.b = _make("nf_b", is_volunteer=True)
        mail.outbox = []

    def test_in_app_row_and_one_email_per_recipient(self):
        reached = notify([self.a, self.b, self.a], "task_completed", params={"task_id": 5}, link="/myapp/tasks/5/")
        self.assertEqual(reached, 2)
        rows = Notification.objects.order_by("recipient_id")
        self.assertEqual([r.recipient_id for r in rows], [self.a.pk, self.b.pk])
        self.assertEqual(rows[0].title_key, "ntf.task_completed.title")
        self.assertEqual(rows[0].params, {"task_id": 5})
        self.assertEqual(sorted(m.to for m in mail.outbox), [[self.a.email], [self.b.email]])

    def test_queryset_recipients_and_unsaved_users(self):
        reached = notify(Users.objects.filter(pk=self.a.pk), "broadcast", subject="S", message="M")
        self.assertEqual(reached, 1)
        self.assertEqual(notify([Users(username="ghost")], "broadcast", subject="S", message="M"), 0)
        self.assertEqual(notify([], "broadcast", subject="S", message="M"), 0)

    def test_unknown_type_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            notify([self.a], "nope")

    def test_free_text_override_is_used_verbatim(self):
        notify([self.a], "broadcast", params={"subject": "Hi"}, subject="Сбор вещей", message="Приходите в субботу")
        self.assertEqual((mail.outbox[0].subject, mail.outbox[0].body), ("Сбор вещей", "Приходите в субботу"))

    def test_user_without_email_still_reached_in_app(self):
        Users.objects.filter(pk=self.a.pk).update(email="")
        self.a.refresh_from_db()
        self.assertEqual(notify([self.a], "task_completed", params={"task_id": 1}), 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.a).count(), 1)

    @override_settings(TELEGRAM_BOT_TOKEN="t")
    def test_telegram_delivery_per_linked_chat(self):
        Users.objects.filter(pk=self.a.pk).update(telegram_id=4242)
        self.a.refresh_from_db()
        with mock.patch("myapp.notifications.send_telegram_message", return_value=True) as tg:
            notify([self.a, self.b], "task_completed", params={"task_id": 3})
        tg.assert_called_once()
        self.assertEqual(tg.call_args.args[0], 4242)
        self.assertIn("#3", tg.call_args.args[1])

    def test_dedupe_key_prevents_duplicates_on_retry(self):
        for _ in range(3):
            reached = notify([self.a, self.b], "sos_new", params={"task_id": 9}, dedupe_key="sos:9")
            self.assertEqual(reached, 2)  # a deduped recipient already has it
        self.assertEqual(Notification.objects.filter(dedupe_key="sos:9").count(), 2)
        self.assertEqual(len(mail.outbox), 2)

    def test_dedupe_is_per_recipient(self):
        notify([self.a], "sos_new", params={"task_id": 9}, dedupe_key="sos:9")
        notify([self.a, self.b], "sos_new", params={"task_id": 9}, dedupe_key="sos:9")
        self.assertEqual(Notification.objects.filter(dedupe_key="sos:9").count(), 2)
        self.assertEqual(len(mail.outbox), 2)

    def test_logs_are_id_only(self):
        with self.assertLogs("myapp.services.notify", level="INFO") as logs:
            notify([self.a], "broadcast", subject="Secret subject", message="Secret body")
        text = "\n".join(logs.output)
        self.assertNotIn("Secret", text)
        self.assertNotIn(self.a.email, text)

    def test_safe_params_strip_contact_details(self):
        clean = notify_service.safe_params({
            "task_id": 1, "phone": "+992901234567", "client_email": "x@y.tj",
            "note": "call +992 90 123 45 67 or mail me@x.tj", "date": "02.10.2026", "ok": True,
        })
        self.assertEqual(set(clean), {"task_id", "note", "date", "ok"})
        self.assertNotIn("123 45 67", clean["note"])
        self.assertNotIn("me@x.tj", clean["note"])
        self.assertEqual(clean["date"], "02.10.2026")


@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
class PreferenceTests(TestCase):
    def setUp(self):
        self.vol = _make("pref_vol", is_volunteer=True)
        self.cur = _make("pref_cur", is_curator=True)
        mail.outbox = []

    def test_disabled_email_is_respected_in_app_still_created(self):
        NotificationPreference.objects.create(user=self.vol, type="broadcast", channel="email", enabled=False)
        reached = notify([self.vol], "broadcast", subject="S", message="M")
        self.assertEqual(reached, 1)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.vol).count(), 1)

    def test_override_is_per_type(self):
        NotificationPreference.objects.create(user=self.vol, type="broadcast", channel="email", enabled=False)
        notify([self.vol], "event_new", params={"title": "T"}, extra={"description": "d", "date": "x"})
        self.assertEqual(len(mail.outbox), 1)

    def test_enabling_a_non_default_channel(self):
        # chat_message defaults to Telegram only; email can be switched on.
        notify([self.vol], "chat_message", params={"task_id": 1})
        self.assertEqual(len(mail.outbox), 0)
        NotificationPreference.objects.create(user=self.vol, type="chat_message", channel="email", enabled=True)
        notify([self.vol], "chat_message", params={"task_id": 1})
        self.assertEqual(len(mail.outbox), 1)

    def test_sos_alerts_cannot_be_switched_off(self):
        NotificationPreference.objects.create(user=self.cur, type="sos_new", channel="email", enabled=False)
        notify([self.cur], "sos_new", params={"task_id": 1})
        self.assertEqual(len(mail.outbox), 1)

    def test_matrix_only_lists_role_types_and_locks_in_app(self):
        vol_types = {row["type"] for row in notify_service.preference_matrix(self.vol)}
        self.assertIn("task_new_in_region", vol_types)
        self.assertNotIn("sos_new", vol_types)
        self.assertNotIn("task_overdue", vol_types)
        cur_types = {row["type"] for row in notify_service.preference_matrix(self.cur)}
        self.assertIn("sos_new", cur_types)
        self.assertNotIn("task_new_in_region", cur_types)
        for row in notify_service.preference_matrix(self.vol):
            in_app = row["cells"][0]
            self.assertEqual((in_app["channel"], in_app["enabled"], in_app["locked"]), ("in_app", True, True))

    def test_preferences_page_get_and_post(self):
        self.client.force_login(self.vol)
        response = self.client.get(reverse("notification_preferences"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="broadcast:email"')
        self.assertNotContains(response, 'value="sos_new:email"')
        # Untick everything except broadcast telegram; try to sneak in a staff type + in_app off.
        response = self.client.post(reverse("notification_preferences"),
                                    {"pref": ["broadcast:telegram", "sos_new:email", "bogus:email"]})
        self.assertEqual(response.status_code, 302)
        prefs = {(p.type, p.channel): p.enabled for p in NotificationPreference.objects.filter(user=self.vol)}
        self.assertFalse(prefs[("broadcast", "email")])
        self.assertNotIn(("broadcast", "telegram"), prefs)  # equals the default → no override row
        self.assertNotIn(("sos_new", "email"), prefs)       # not a volunteer type
        self.assertFalse(any(channel == "in_app" for _, channel in prefs))
        # Back to defaults removes the overrides again.
        all_on = [f"{row['type']}:{c['channel']}" for row in notify_service.preference_matrix(self.vol)
                  for c in row["cells"] if c["channel"] != "in_app" and
                  c["channel"] in notify_service.default_channels(notify_service.NOTIFICATION_TYPES[row["type"]], "volunteer")]
        self.client.post(reverse("notification_preferences"), {"pref": all_on})
        self.assertFalse(NotificationPreference.objects.filter(user=self.vol).exists())

    def test_preferences_require_login(self):
        response = self.client.get(reverse("notification_preferences"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])


@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
class LanguageRenderingTests(TestCase):
    def setUp(self):
        self.ru = _make("lang_ru", is_client=True)
        self.en = _make("lang_en", is_client=True)
        self.tj = _make("lang_tj", is_client=True)
        _set_lang(self.en, "en")
        _set_lang(self.tj, "tj")
        mail.outbox = []

    def _mail_for(self, user):
        return next(m for m in mail.outbox if m.to == [user.email])

    def test_each_recipient_gets_their_language(self):
        notify([self.ru, self.en, self.tj], "task_completed", params={"task_id": 7})
        self.assertEqual(self._mail_for(self.ru).subject, "Запрос выполнен")
        self.assertEqual(self._mail_for(self.ru).body, "Ваш запрос #7 отмечен как выполненный. Спасибо!")
        self.assertEqual(self._mail_for(self.en).subject, "Request completed")
        self.assertEqual(self._mail_for(self.tj).subject, "Дархост иҷро шуд")

    def test_codes_are_localized_per_language(self):
        notify([self.ru, self.en], "volunteer_pinged",
               params={"task_id": 3, "region": "sogd", "help_type": "medical"})
        self.assertIn("(Медицинская помощь) в регионе Согд", self._mail_for(self.ru).body)
        self.assertIn("(Medical help) in Sughd", self._mail_for(self.en).body)

    def test_ru_texts_reproduce_the_legacy_messages(self):
        """The pre-Feature-Pack Russian strings, byte for byte."""
        cases = {
            ("task_accepted", "Запрос принят волонтером",
             "Запрос #4 принят.\nКлиент: cl, телефон: +992901234567\nВолонтер: vo\nАдрес: Rudaki 1"):
                {"params": {"task_id": 4, "volunteer_name": "vo"},
                 "extra": {"client_name": "cl", "phone": "+992901234567", "address": "Rudaki 1"}},
            ("task_stage_changed", "Волонтёр на месте", "Запрос #4: на месте."):
                {"params": {"task_id": 4, "work_stage": "arrived"}},
            ("sos_update", "Сигнал опасности: обновление", "Ваш сигнал по запросу #4 принят координатором."):
                {"params": {"task_id": 4, "action": "acknowledged"}},
            ("application_submitted", "Новая заявка волонтера",
             "Пользователь bob подал заявку на волонтерство в регионе —."):
                {"params": {"username": "bob", "kind": "new", "region": ""}, "extra": {"region_name": None}},
            ("application_submitted", "Повторная заявка волонтера",
             "Пользователь bob подал заявку на волонтерство повторно."):
                {"params": {"username": "bob", "kind": "again"}},
            ("pet_match", "Возможное совпадение по вашему объявлению",
             "По вашему объявлению #2 появилось новое объявление о найденном животном "
             "(#3, Кошка, регион не указан). Проверьте, не совпадают ли они."):
                {"params": {"pet_id": 3, "other_id": 2, "report_type": "found", "species": "cat", "region": ""},
                 "extra": {"region_name": None}},
            ("donation_status", "Статус предложения обновлён", "Ваше предложение #8 теперь: Одобрено."):
                {"params": {"donation_id": 8, "status": "approved"}},
        }
        for (code, subject, body), kw in cases.items():
            with self.subTest(code=code, body=body[:30]):
                self.assertEqual(notify_texts.render_channel(code, "ru", {**kw["params"], **kw.get("extra", {})}),
                                 (subject, body))

    def test_language_endpoint(self):
        self.client.force_login(self.ru)
        url = reverse("profile_language")
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url, {"language": "xx"}).status_code, 400)
        response = self.client.post(url, {"language": "tj"})
        self.assertEqual(response.json(), {"success": True, "language": "tj"})
        self.assertEqual(Profile.objects.get(user=self.ru).language, "tj")
        self.client.logout()
        self.assertEqual(self.client.post(url, {"language": "en"}).status_code, 302)


class TextRegistryTests(TestCase):
    _FIELDS = re.compile(r"{(\w+)}")

    def test_every_type_has_all_texts(self):
        for code in notify_service.NOTIFICATION_TYPES:
            with self.subTest(code=code):
                for lang in notify_texts.LANGS:
                    self.assertEqual(len(notify_texts.CHANNEL_TEXTS[code][lang]), 2)
                    self.assertEqual(len(notify_texts.INAPP_TEXTS[code][lang]), 2)
                    self.assertIn(code, notify_texts.TYPE_LABELS)

    def test_inapp_placeholders_use_only_safe_param_names(self):
        allowed = {"task_id", "volunteer_name", "username", "donation_id", "pet_id", "other_id",
                   "subject", "message", "title", "date", "time", "hours", "count",
                   "sender_display", "stars"}
        for lang in notify_texts.LANGS:
            for key, text in notify_texts.all_i18n_keys(lang).items():
                for name in self._FIELDS.findall(text):
                    self.assertIn(name, allowed, f"{key} uses {{{name}}}")

    def test_key_sets_match_across_languages(self):
        en = set(notify_texts.all_i18n_keys("en"))
        self.assertEqual(en, set(notify_texts.all_i18n_keys("ru")))
        self.assertEqual(en, set(notify_texts.all_i18n_keys("tj")))

    def test_rendering_never_raises_on_missing_context(self):
        for code in notify_service.NOTIFICATION_TYPES:
            for lang in notify_texts.LANGS + ("xx",):
                subject, body = notify_texts.render_channel(code, lang, {})
                self.assertIsInstance(subject, str)
                self.assertIsInstance(body, str)

    def test_service_subject_constants_still_match(self):
        from myapp.services import emergency, overdue, pets, stale

        self.assertEqual(overdue.OVERDUE_SUBJECT, "Просроченный запрос")
        self.assertEqual(stale.STALE_SUBJECT, "Запрос долго ждёт волонтёра")
        self.assertEqual(emergency.NEW_SUBJECT, "⚠️ SOS: волонтёр сообщил об опасности")
        self.assertEqual(pets.NEW_REPORT_SUBJECT, "Новое объявление о животном")


@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
class DoNotDisturbTests(TestCase):
    """Owner decision §9.3: DND excludes a volunteer from new-task pings only."""

    def setUp(self):
        self.client_user = _make("dnd_client", is_client=True)
        self.here = _make("dnd_here", is_volunteer=True)
        self.away = _make("dnd_away", is_volunteer=True)
        self.expired = _make("dnd_expired", is_volunteer=True)
        Profile.objects.filter(user=self.away).update(is_away=True)
        Profile.objects.filter(user=self.expired).update(is_away=True, away_until=timezone.now() - timedelta(hours=1))
        mail.outbox = []

    def test_new_request_skips_away_volunteers(self):
        self.client.force_login(self.client_user)
        response = self.client.post(reverse("create_request"), {
            "help_type": "grocery", "description": "Need bread", "address": "Street 1",
            "phone": "+992901234567", "priority": "normal",
        })
        self.assertEqual(response.status_code, 302)
        notified = set(Notification.objects.filter(type="task_new_in_region").values_list("recipient_id", flat=True))
        self.assertEqual(notified, {self.here.pk, self.expired.pk})
        self.assertNotIn([self.away.email], [m.to for m in mail.outbox])

    def test_broadcast_still_reaches_away_volunteers(self):
        admin = Users.objects.create_superuser(username="dnd_admin", email="dnd_admin@example.com", password=PASSWORD)
        self.client.force_login(admin)
        self.client.post(reverse("broadcast"), {"subject": "Hi", "message": "All hands", "region": "all"})
        self.assertTrue(Notification.objects.filter(type="broadcast", recipient=self.away).exists())


@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
class NotificationEndpointTests(TestCase):
    def setUp(self):
        cache.clear()
        self.me = _make("ep_me", is_volunteer=True)
        self.other = _make("ep_other", is_volunteer=True)
        self.mine = [
            Notification.objects.create(recipient=self.me, type="task_completed",
                                        title_key="ntf.task_completed.title", body_key="ntf.task_completed.body",
                                        params={"task_id": i}, link=f"/myapp/tasks/{i}/")
            for i in range(1, 4)
        ]
        self.theirs = Notification.objects.create(recipient=self.other, type="broadcast",
                                                  title_key="ntf.broadcast.title", body_key="ntf.broadcast.body")

    def test_anonymous_is_redirected_everywhere(self):
        for name, args, method in [
            ("notifications", [], "get"), ("notifications_bell", [], "get"), ("realtime_poll", [], "get"),
            ("notifications_read_all", [], "post"), ("notification_read", [self.mine[0].pk], "post"),
        ]:
            response = getattr(self.client, method)(reverse(name, args=args))
            self.assertEqual(response.status_code, 302, name)

    def test_bell_returns_count_and_latest_own_only(self):
        self.client.force_login(self.me)
        data = self.client.get(reverse("notifications_bell")).json()
        self.assertEqual(data["unread_count"], 3)
        self.assertEqual([i["id"] for i in data["items"]], [n.pk for n in reversed(self.mine)])
        self.assertEqual(set(data["items"][0]), {"id", "type", "title_key", "body_key", "params", "link", "read", "created_at"})

    def test_bell_query_count_is_constant(self):
        self.client.force_login(self.me)
        self.client.get(reverse("notifications_bell"))  # warm the session/user caches

        def count():
            from django.db import connection
            from django.test.utils import CaptureQueriesContext

            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("notifications_bell"))
            return len(ctx.captured_queries)

        before = count()
        for i in range(30):
            Notification.objects.create(recipient=self.me, type="broadcast", title_key="t", body_key="b")
        self.assertEqual(count(), before)
        self.assertLessEqual(before, 5)

    def test_other_users_notification_is_404(self):
        self.client.force_login(self.me)
        response = self.client.post(reverse("notification_read", args=[self.theirs.pk]))
        self.assertEqual(response.status_code, 404)
        self.theirs.refresh_from_db()
        self.assertIsNone(self.theirs.read_at)

    def test_mark_one_read_json_and_get_not_allowed(self):
        self.client.force_login(self.me)
        url = reverse("notification_read", args=[self.mine[0].pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, HTTP_ACCEPT="application/json")
        self.assertEqual(response.json(), {"success": True, "ids": [self.mine[0].pk], "unread": 2})
        self.mine[0].refresh_from_db()
        self.assertIsNotNone(self.mine[0].read_at)

    def test_open_follows_internal_link_only(self):
        self.client.force_login(self.me)
        response = self.client.post(reverse("notification_read", args=[self.mine[1].pk]), {"open": "1"})
        self.assertEqual(response["Location"], "/myapp/tasks/2/")
        Notification.objects.filter(pk=self.mine[2].pk).update(link="https://evil.example/x")
        response = self.client.post(reverse("notification_read", args=[self.mine[2].pk]), {"open": "1"})
        self.assertEqual(response["Location"], reverse("notifications"))

    def test_read_all_touches_only_mine(self):
        self.client.force_login(self.me)
        response = self.client.post(reverse("notifications_read_all"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Notification.objects.filter(recipient=self.me, read_at__isnull=True).count(), 0)
        self.theirs.refresh_from_db()
        self.assertIsNone(self.theirs.read_at)

    def test_list_page_filters_and_never_shows_others(self):
        Notification.objects.filter(pk=self.mine[0].pk).update(read_at=timezone.now())
        self.client.force_login(self.me)
        response = self.client.get(reverse("notifications"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["rows"]), 3)
        self.assertNotContains(response, f'data-notification-id="{self.theirs.pk}"')
        self.assertContains(response, 'data-i18n="ntf.task_completed.title"')
        self.assertContains(response, 'name="robots" content="noindex')
        response = self.client.get(reverse("notifications"), {"unread": "1"})
        self.assertEqual(len(response.context["rows"]), 2)
        response = self.client.get(reverse("notifications"), {"type": "broadcast"})
        self.assertEqual(len(response.context["rows"]), 0)
        response = self.client.get(reverse("notifications"), {"type": "<script>"})
        self.assertEqual(len(response.context["rows"]), 3)

    def test_poll_returns_envelopes_since_cursor(self):
        self.client.force_login(self.me)
        data = self.client.get(reverse("realtime_poll"), {"since": self.mine[0].pk}).json()
        self.assertEqual([e["payload"]["id"] for e in data["events"]], [self.mine[1].pk, self.mine[2].pk])
        self.assertEqual({e["type"] for e in data["events"]}, {"notification.new"})
        self.assertEqual(data["cursor"], self.mine[2].pk)
        self.assertEqual(data["unread_count"], 3)
        data = self.client.get(reverse("realtime_poll"), {"since": data["cursor"]}).json()
        self.assertEqual((data["events"], data["cursor"]), ([], self.mine[2].pk))
        self.assertEqual(self.client.get(reverse("realtime_poll"), {"since": "junk"}).status_code, 200)


@override_settings(EMAIL_BACKEND=LOCMEM, TELEGRAM_BOT_TOKEN="")
class CallSiteMigrationTests(TestCase):
    """The migrated call sites create typed in-app rows with safe params."""

    def setUp(self):
        self.client_user = _make("cs_client", is_client=True)
        self.vol = _make("cs_vol", is_volunteer=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="d", address="Rudaki 5",
            phone="+992901234567", region="dushanbe",
        )
        mail.outbox = []

    def test_accept_then_complete(self):
        self.client.force_login(self.vol)
        self.client.post(reverse("accept_task", args=[self.task.pk]))
        rows = Notification.objects.filter(type="task_accepted")
        self.assertEqual({r.recipient_id for r in rows}, {self.client_user.pk, self.vol.pk})
        self.assertNotIn("phone", rows[0].params)
        self.assertTrue(any("+992901234567" in m.body for m in mail.outbox))  # email keeps the legacy body
        self.client.post(reverse("complete_task", args=[self.task.pk]))
        self.assertTrue(Notification.objects.filter(type="task_completed", recipient=self.client_user).exists())

    def test_stale_sweep_zero_staff_still_returns_zero(self):
        from myapp.services import stale

        HelpRequest.objects.filter(pk=self.task.pk).update(created_at=timezone.now() - timedelta(hours=60))
        with self.assertLogs("myapp.services.stale", level="ERROR"):
            self.assertEqual(stale.sweep_stale_pending(), [])
        self.assertFalse(Notification.objects.filter(type="task_stale").exists())


class PruneCommandTests(TestCase):
    def setUp(self):
        self.user = _make("prune_u", is_client=True)
        self.old = Notification.objects.create(recipient=self.user, type="broadcast", title_key="t", body_key="b")
        self.new = Notification.objects.create(recipient=self.user, type="broadcast", title_key="t", body_key="b")
        Notification.objects.filter(pk=self.old.pk).update(created_at=timezone.now() - timedelta(days=91))

    def test_dry_run_counts_only(self):
        out = StringIO()
        call_command("prune_notifications", "--dry-run", stdout=out)
        self.assertIn("Would delete 1", out.getvalue())
        self.assertEqual(Notification.objects.count(), 2)

    def test_deletes_older_than_retention(self):
        out = StringIO()
        call_command("prune_notifications", stdout=out)
        self.assertIn("Deleted 1", out.getvalue())
        self.assertEqual(list(Notification.objects.values_list("pk", flat=True)), [self.new.pk])

    @override_settings(NOTIFICATION_RETENTION_DAYS=200)
    def test_respects_setting(self):
        call_command("prune_notifications", stdout=StringIO())
        self.assertEqual(Notification.objects.count(), 2)
