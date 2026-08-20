from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Profile, Users


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AccountsTests(TestCase):
    def setUp(self):
        self.user = Users.objects.create_user(
            username="volunteer_one",
            email="vol@example.com",
            password="Volunteer2026!",
            is_volunteer=True,
        )

    def test_profile_created_by_signal(self):
        self.assertTrue(Profile.objects.filter(user=self.user).exists())

    def test_role_property(self):
        self.assertEqual(self.user.role, Users.ROLE_VOLUNTEER)
        self.assertEqual(self.user.role_display, "Волонтер")

    def test_single_role_validation(self):
        from django.core.exceptions import ValidationError

        self.user.is_client = True
        with self.assertRaises(ValidationError):
            self.user.save()

    def test_login_page_loads(self):
        self.assertEqual(self.client.get(reverse("login")).status_code, 200)

    def test_register_flow(self):
        response = self.client.post(
            reverse("register"),
            {
                "username": "newvolunteer",
                "email": "new@example.com",
                "role": "volunteer",
                "region": "dushanbe",
                "password": "Volunteer2026!",
                "confirm_password": "Volunteer2026!",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Users.objects.filter(username="newvolunteer", is_volunteer=True).exists())

    def test_forgot_password_does_not_crash(self):
        # Regression: send_mail() was previously called with the wrong arguments,
        # which raised a 500 on this endpoint.
        response = self.client.post(reverse("forgot_password"), {"email": "vol@example.com"})
        self.assertEqual(response.status_code, 302)

    def test_profile_requires_login(self):
        response = self.client.get(reverse("profile"))
        self.assertEqual(response.status_code, 302)
        self.client.login(username="volunteer_one", password="Volunteer2026!")
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)
