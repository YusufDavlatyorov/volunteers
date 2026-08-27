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
        # Choosing "volunteer" at registration no longer grants the role directly:
        # it only creates a pending VolunteerApplication (see myapp.tests.VolunteerApplicationTests).
        user = Users.objects.get(username="newvolunteer")
        self.assertFalse(user.is_volunteer)

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

    def test_edit_profile_page_loads_and_saves_location(self):
        self.client.login(username="volunteer_one", password="Volunteer2026!")
        self.assertEqual(self.client.get(reverse("edit_profile")).status_code, 200)
        response = self.client.post(reverse("edit_profile"), {
            "email": self.user.email, "region": "dushanbe", "telegram_id": "",
            "full_name": "Vol One", "bio": "", "latitude": "38.55", "longitude": "68.78",
            "availability_status": "available",
        })
        self.assertRedirects(response, reverse("profile"))
        self.user.profile.refresh_from_db()
        self.assertAlmostEqual(float(self.user.profile.latitude), 38.55)
        self.assertIsNotNone(self.user.profile.location_updated_at)
