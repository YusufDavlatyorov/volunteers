"""Chat photos are private: served only through the permission-checked view
(X-Accel-Redirect in production), never by a guessable /media/ URL."""

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import Users
from myapp.testing import TempMediaRoot
from myapp.models import ChatMessage, HelpRequest
from myapp.services import chat

PASSWORD = "Chat-Media-2026!"
_MEDIA_ROOT = TempMediaRoot("chat-media-tests-")  # fork-safe (myapp/testing.py)
MEDIA_TMP = _MEDIA_ROOT.path


def _png():
    buf = io.BytesIO()
    Image.new("RGB", (40, 30), (20, 120, 90)).save(buf, "PNG")
    return SimpleUploadedFile("photo.png", buf.getvalue(), content_type="image/png")


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class ChatImageAccessTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _MEDIA_ROOT.cleanup()

    def setUp(self):
        self.client_user = _make("cm_client", is_client=True)
        self.volunteer = _make("cm_vol", is_volunteer=True)
        self.other = _make("cm_other", is_volunteer=True)
        self.curator = _make("cm_curator", is_curator=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, status="active", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )
        self.message = ChatMessage.objects.create(
            task=self.task, sender=self.volunteer, kind="image", image=_png()
        )

    def _url(self, variant="full"):
        return reverse("chat_message_image", args=[self.message.pk, variant])

    def test_payload_urls_never_point_at_media(self):
        payload = chat.message_payload(self.message, self.client_user)
        urls = [v for v in payload.values() if isinstance(v, str) and "/" in v]
        self.assertTrue(any("/chat/messages/" in u for u in urls))
        self.assertFalse(any("/media/" in u for u in urls))

    def test_parties_and_staff_get_the_file(self):
        for user in (self.client_user, self.volunteer, self.curator):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(self._url())
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "image/png")
                self.assertIn("private", response["Cache-Control"])
                b"".join(response.streaming_content)

    def test_unrelated_and_anonymous_get_nothing(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self._url()).status_code, 302)

    def test_hidden_message_image_is_staff_only(self):
        ChatMessage.objects.filter(pk=self.message.pk).update(hidden_at=self.message.created_at)
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(self._url()).status_code, 404)
        self.client.force_login(self.curator)
        self.assertEqual(self.client.get(self._url()).status_code, 200)

    def test_unknown_variant_and_missing_thumb_are_404(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(self._url("original")).status_code, 404)
        ChatMessage.objects.filter(pk=self.message.pk).update(image_variants={})
        self.assertEqual(self.client.get(self._url("thumb")).status_code, 404)

    def test_path_outside_chat_folder_is_refused(self):
        ChatMessage.objects.filter(pk=self.message.pk).update(image_variants={"thumb": "avatars/../x.png"})
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(self._url("thumb")).status_code, 404)

    @override_settings(MEDIA_X_ACCEL=True)
    def test_production_mode_hands_off_to_nginx(self):
        self.client.force_login(self.client_user)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["X-Accel-Redirect"].startswith("/protected-media/chat/"))
        self.assertEqual(response.content, b"")
