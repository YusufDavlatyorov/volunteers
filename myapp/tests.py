from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Users
from .models import HelpRequest


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class HelpRequestFlowTests(TestCase):
    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="client_one", email="client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.volunteer = Users.objects.create_user(
            username="vol_one", email="vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )

    def test_client_creates_request(self):
        self.client.login(username="client_one", password="pass12345")
        response = self.client.post(
            reverse("create_request"),
            {"help_type": "grocery", "description": "Need groceries", "address": "Some street", "phone": "+992"},
        )
        self.assertEqual(response.status_code, 302)
        request = HelpRequest.objects.get(client=self.client_user)
        self.assertEqual(request.status, "pending")
        self.assertEqual(request.region, "dushanbe")

    def test_volunteer_accept_and_complete(self):
        request = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="Buy food", address="Addr", phone="+992", region="dushanbe"
        )
        self.client.login(username="vol_one", password="pass12345")

        self.client.get(reverse("accept_task", args=[request.pk]))
        request.refresh_from_db()
        self.assertEqual(request.status, "active")
        self.assertEqual(request.volunteer, self.volunteer)

        self.client.get(reverse("complete_task", args=[request.pk]))
        request.refresh_from_db()
        self.assertEqual(request.status, "completed")
        self.volunteer.profile.refresh_from_db()
        self.assertEqual(self.volunteer.profile.rating, 3)

    def test_public_pages_load(self):
        for name in ["about", "rating"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_ai_chat_returns_fallback_without_api_key(self):
        self.client.login(username="vol_one", password="pass12345")
        # Clear both settings and env vars so the view takes the offline fallback
        # path (no external network call during tests).
        with override_settings(GROQ_API_KEY="", GEMINI_API_KEY=""), mock.patch.dict(
            "os.environ", {"GROQ_API_KEY": "", "GEMINI_API_KEY": ""}
        ):
            response = self.client.post(
                reverse("ai_chat"), data='{"message": "привет"}', content_type="application/json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reply", response.json())
