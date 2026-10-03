"""The Telegram link page tolerates "@Name" in TELEGRAM_BOT_USERNAME (milestone 5)."""

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Users


class BotUsernameTests(TestCase):
    @override_settings(TELEGRAM_BOT_USERNAME="@KhayrKhohBot")
    def test_at_sign_is_not_doubled_and_the_deep_link_works(self):
        user = Users.objects.create_user(username="tgname", email="tgname@example.com", password="Pw-123456789!",
                                         is_client=True)
        self.client.force_login(user)
        response = self.client.post(reverse("telegram_link"))
        self.assertContains(response, "@KhayrKhohBot")
        self.assertNotContains(response, "@@KhayrKhohBot")
        self.assertContains(response, "https://t.me/KhayrKhohBot?start=")
