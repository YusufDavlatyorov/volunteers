"""Milestone 5 follow-ups (owner, 2026-10-03):

1. the client's phone on the site is hidden from a volunteer until they accept
   the request (the bot already did this); staff always see it;
5. a local DEBUG run shouts in the Terminal when `.env` holds real delivery keys.
"""

import io

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from accounts.models import Users
from myapp.models import HelpRequest
from myapp.services import access
from server import dev_safety

PASSWORD = "M5-Followups-2026!"
PHONE = "+992901234567"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD,
        region=flags.pop("region", "dushanbe"), **flags,
    )


@override_settings(TELEGRAM_BOT_TOKEN="")
class ClientPhoneVisibilityTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client_user = _make("ph_client", is_client=True)
        self.vol = _make("ph_vol", is_volunteer=True)
        self.other_vol = _make("ph_vol2", is_volunteer=True)
        self.curator = _make("ph_curator", is_curator=True)
        self.admin = Users.objects.create_superuser(username="ph_admin", email="ph_admin@example.com",
                                                    password=PASSWORD)
        self.task = HelpRequest.objects.create(client=self.client_user, help_type="grocery", description="d",
                                               address="ул. Рудаки 1", phone=PHONE, region="dushanbe")

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("task_detail", args=[self.task.pk]))

    def test_volunteer_sees_address_but_not_phone_while_pending(self):
        response = self.page(self.vol)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ул. Рудаки 1")
        self.assertNotContains(response, PHONE)
        self.assertContains(response, 'data-i18n="detail.phone_after_accept"')

    def test_phone_appears_after_accepting(self):
        self.client.force_login(self.vol)
        self.client.post(reverse("accept_task", args=[self.task.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.volunteer_id, self.vol.pk)
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertContains(response, PHONE)
        self.assertNotContains(response, "detail.phone_after_accept")

    def test_staff_and_client_always_see_phone(self):
        for user in (self.curator, self.admin, self.client_user):
            with self.subTest(user=user.username):
                self.assertContains(self.page(user), PHONE)

    def test_predicate(self):
        self.assertFalse(access.can_see_client_phone(self.vol, self.task))
        self.assertFalse(access.can_see_client_phone(None, self.task))
        self.assertTrue(access.can_see_client_phone(self.curator, self.task))
        self.assertTrue(access.can_see_client_phone(self.client_user, self.task))
        HelpRequest.objects.filter(pk=self.task.pk).update(volunteer=self.vol, status="active")
        self.task.refresh_from_db()
        self.assertTrue(access.can_see_client_phone(self.vol, self.task))
        # Another volunteer never sees someone else's task phone.
        self.assertFalse(access.can_see_client_phone(self.other_vol, self.task))


class DevSafetyWarningTests(SimpleTestCase):
    def run_warn(self, env, *, debug=True, argv=("manage.py", "runserver")):
        out = io.StringIO()
        names = dev_safety.warn_if_real_keys(debug=debug, env=env, argv=list(argv), stream=out)
        return names, out.getvalue()

    def test_warns_with_names_only_never_values(self):
        names, text = self.run_warn({"SMTP_PASSWORD": "s3cret-value", "TELEGRAM_BOT_TOKEN": "123:abcdef"})
        self.assertEqual(names, ["SMTP_PASSWORD", "TELEGRAM_BOT_TOKEN"])
        self.assertIn("SMTP_PASSWORD", text)
        self.assertIn("ВНИМАНИЕ", text)
        self.assertNotIn("s3cret-value", text)
        self.assertNotIn("123:abcdef", text)

    def test_silent_with_empty_keys(self):
        names, text = self.run_warn({"SMTP_USER": "", "SMTP_PASSWORD": " ", "TELEGRAM_BOT_TOKEN": ""})
        self.assertEqual((names, text), ([], ""))

    def test_silent_in_production_and_tests(self):
        env = {"TELEGRAM_BOT_TOKEN": "123:abc"}
        self.assertEqual(self.run_warn(dict(env), debug=False), ([], ""))
        self.assertEqual(self.run_warn(dict(env), argv=("manage.py", "test")), ([], ""))

    def test_printed_once_per_process_tree(self):
        env = {"TELEGRAM_COMMUNITY_BOT_TOKEN": "1:x"}
        _, first = self.run_warn(env)
        _, second = self.run_warn(env)  # runserver's autoreload child inherits env
        self.assertTrue(first)
        self.assertEqual(second, "")


class RedactSecretsFilterTests(SimpleTestCase):
    TOKEN = "1234567890:AAH-abcdefghijklmnopqrstuvwxyz_0123"

    def emit(self, logger_name, *args, exc=None):
        import logging

        from server.log_filters import RedactSecretsFilter

        out = io.StringIO()
        handler = logging.StreamHandler(out)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler.addFilter(RedactSecretsFilter())
        log = logging.getLogger(logger_name)
        log.addHandler(handler)
        try:
            if exc is not None:
                try:
                    raise exc
                except Exception:
                    log.error(*args, exc_info=True)
            else:
                log.error(*args)
        finally:
            log.removeHandler(handler)
        return out.getvalue()

    def test_token_in_message_and_args_is_redacted(self):
        text = self.emit("m5f.redact.a", "POST https://api.telegram.org/bot%s/getUpdates failed", self.TOKEN)
        self.assertNotIn(self.TOKEN, text)
        self.assertIn("bot<redacted-token>/getUpdates", text)

    def test_token_in_traceback_is_redacted(self):
        text = self.emit("m5f.redact.b", "send failed",
                         exc=RuntimeError(f"https://api.telegram.org/bot{self.TOKEN}/sendMessage"))
        self.assertIn("RuntimeError", text)
        self.assertNotIn(self.TOKEN, text)

    def test_console_handler_and_httpx_level_are_configured(self):
        import logging

        from django.conf import settings

        self.assertIn("redact_secrets", settings.LOGGING["handlers"]["console"]["filters"])
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        # Plain text passes through untouched (ids, coordinates, times).
        from server.log_filters import redact

        plain = "task #12 accepted by volunteer #7 at 38.5598:68.7870 12:30:45"
        self.assertEqual(redact(plain), plain)
