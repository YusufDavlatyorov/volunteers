import os
import subprocess
import sys
from pathlib import Path

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import MAX_UPLOAD_SIZE_BYTES, Profile, Users, hash_token


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

    def test_edit_profile_rejects_out_of_range_coordinates(self):
        # LOW finding: DecimalField(max_digits=9) alone accepts values well
        # outside real WGS84 latitude/longitude ranges (e.g. 999.999999).
        self.client.login(username="volunteer_one", password="Volunteer2026!")
        response = self.client.post(reverse("edit_profile"), {
            "email": self.user.email, "region": "dushanbe", "telegram_id": "",
            "full_name": "Vol One", "bio": "", "latitude": "999.5", "longitude": "68.78",
            "availability_status": "available",
        })
        self.assertEqual(response.status_code, 200)  # form re-rendered, not saved
        self.user.profile.refresh_from_db()
        self.assertIsNone(self.user.profile.latitude)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class LoginRateLimitTests(TestCase):
    """MEDIUM: login had no brute-force protection at all."""

    def setUp(self):
        cache.clear()
        self.user = Users.objects.create_user(
            username="throttle_user", email="throttle@example.com", password="Volunteer2026!", is_client=True,
        )

    def tearDown(self):
        cache.clear()

    def test_repeated_failed_logins_get_rate_limited(self):
        for _ in range(5):
            response = self.client.post(reverse("login"), {"username": "throttle_user", "password": "wrong"})
            self.assertEqual(response.status_code, 200)

        # 6th attempt: even the *correct* password is now refused by the
        # throttle itself, proving the limiter runs before authenticate().
        response = self.client.post(reverse("login"), {"username": "throttle_user", "password": "Volunteer2026!"})
        self.assertContains(response, "Слишком много попыток")
        self.assertEqual(self.client.get(reverse("profile")).status_code, 302)  # still logged out

    def test_successful_login_clears_the_failed_attempt_counter(self):
        self.client.post(reverse("login"), {"username": "throttle_user", "password": "wrong"})
        response = self.client.post(reverse("login"), {"username": "throttle_user", "password": "Volunteer2026!"})
        self.assertRedirects(response, reverse("profile"))

    def test_rate_limit_is_scoped_per_username_not_global(self):
        Users.objects.create_user(username="other_user", email="other@example.com", password="Volunteer2026!", is_client=True)
        for _ in range(5):
            self.client.post(reverse("login"), {"username": "throttle_user", "password": "wrong"})
        response = self.client.post(reverse("login"), {"username": "other_user", "password": "Volunteer2026!"})
        self.assertRedirects(response, reverse("profile"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetRateLimitTests(TestCase):
    """MEDIUM (extra finding): forgot_password had no throttle, so it could
    be used to mail-bomb a victim's inbox with reset links."""

    def setUp(self):
        cache.clear()
        self.user = Users.objects.create_user(
            username="reset_throttle", email="reset_throttle@example.com", password="Volunteer2026!", is_client=True,
        )

    def tearDown(self):
        cache.clear()

    def test_repeated_reset_requests_from_same_ip_are_throttled(self):
        from django.core import mail

        for _ in range(5):
            self.client.post(reverse("forgot_password"), {"email": self.user.email})
        mail.outbox.clear()
        self.client.post(reverse("forgot_password"), {"email": self.user.email})
        self.assertEqual(len(mail.outbox), 0)


class LogoutMethodTests(TestCase):
    """MEDIUM: GET /logout/ performed a state change (logging the user out)
    from a plain link, with no CSRF protection at all for that action."""

    def setUp(self):
        self.user = Users.objects.create_user(
            username="logout_user", email="logout_user@example.com", password="Volunteer2026!", is_client=True,
        )

    def test_get_logout_is_rejected(self):
        self.client.login(username="logout_user", password="Volunteer2026!")
        response = self.client.get(reverse("logout"))
        self.assertEqual(response.status_code, 405)
        # Session must be untouched by the rejected GET.
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)

    def test_post_logout_works(self):
        self.client.login(username="logout_user", password="Volunteer2026!")
        response = self.client.post(reverse("logout"))
        self.assertRedirects(response, reverse("login"))
        self.assertEqual(self.client.get(reverse("profile")).status_code, 302)


class TokenHashingTests(TestCase):
    """MEDIUM: reset/email-verification tokens were stored in the DB in
    plaintext, so a DB read (backup leak, SQLi elsewhere, curious admin)
    handed out working reset/confirmation links."""

    def setUp(self):
        self.user = Users.objects.create_user(
            username="token_user", email="token_user@example.com", password="Volunteer2026!", is_client=True,
        )

    def test_reset_password_token_is_not_stored_in_plaintext(self):
        raw_token = self.user.generate_reset_password_token()
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.reset_password_token, raw_token)
        self.assertEqual(self.user.reset_password_token, hash_token(raw_token))

    def test_reset_password_confirm_still_works_with_the_emailed_raw_token(self):
        raw_token = self.user.generate_reset_password_token()
        response = self.client.post(
            reverse("reset_password", args=[raw_token]),
            {"new_password": "NewPassw0rd!23", "confirm_password": "NewPassw0rd!23"},
        )
        self.assertRedirects(response, reverse("login"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("NewPassw0rd!23"))

    def test_email_verification_token_is_not_stored_in_plaintext(self):
        raw_token = self.user.generate_email_verification_token()
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.email_verification_token, raw_token)
        self.assertEqual(self.user.email_verification_token, hash_token(raw_token))

    def test_confirm_email_still_works_with_the_emailed_raw_token(self):
        raw_token = self.user.generate_email_verification_token()
        response = self.client.get(reverse("confirm_email", args=[raw_token]))
        # fetch_redirect_response=False: confirm_email_view always redirects to
        # "profile" regardless of whether *this* browser is logged in as the
        # confirmed user, so the target page itself may in turn redirect to
        # login — that's pre-existing view behavior, not what this test covers.
        self.assertRedirects(response, reverse("profile"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_email_verified)


class ProductionSecuritySettingsTests(TestCase):
    """CRITICAL: server/settings.py used to fall back to a committed insecure
    SECRET_KEY, DEBUG=True, and ALLOWED_HOSTS=['*']. These checks spawn a
    fresh interpreter per case (settings are evaluated at import time, so
    they can't be toggled via override_settings) to prove the *startup*
    validation itself, not just its current values in this test process."""

    SETTINGS_PROBE = (
        "import django\n"
        "django.setup()\n"
        "from django.conf import settings as s\n"
        "print(s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_SSL_REDIRECT, "
        "s.SECURE_HSTS_SECONDS, s.SECURE_CONTENT_TYPE_NOSNIFF, s.X_FRAME_OPTIONS)\n"
    )

    def _run(self, extra_env):
        env = os.environ.copy()
        env["DJANGO_SETTINGS_MODULE"] = "server.settings"
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-c", self.SETTINGS_PROBE],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_missing_secret_key_fails_fast(self):
        result = self._run({"DJANGO_SECRET_KEY": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)

    def test_wildcard_allowed_hosts_refused_once_debug_is_off(self):
        result = self._run({
            "DJANGO_SECRET_KEY": "a-valid-looking-test-secret-key",
            "DJANGO_DEBUG": "False",
            "DJANGO_ALLOWED_HOSTS": "*",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_ALLOWED_HOSTS", result.stderr)

    def test_production_flags_enabled_once_debug_is_off(self):
        result = self._run({
            "DJANGO_SECRET_KEY": "a-valid-looking-test-secret-key",
            "DJANGO_DEBUG": "False",
            "DJANGO_ALLOWED_HOSTS": "example.com",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "True True True 31536000 True DENY")

    def test_debug_mode_keeps_dev_friendly_cookie_settings(self):
        # Reflects this test process's own settings. Note: Django's test
        # runner itself forces settings.DEBUG=False for the live test run
        # (regardless of .env), but SESSION_COOKIE_SECURE/CSRF_COOKIE_SECURE/
        # etc. are computed once at settings-import time from the *real*
        # DJANGO_DEBUG=True in this repo's .env, before the test runner
        # touches anything — so they should still read as dev-friendly here,
        # proving the hardening doesn't leak into the plain-HTTP local
        # dev/runserver workflow.
        from django.conf import settings as live_settings

        self.assertFalse(live_settings.SESSION_COOKIE_SECURE)
        self.assertFalse(live_settings.CSRF_COOKIE_SECURE)
        self.assertFalse(live_settings.SECURE_SSL_REDIRECT)
        self.assertEqual(live_settings.SECURE_HSTS_SECONDS, 0)
        self.assertTrue(live_settings.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertEqual(live_settings.X_FRAME_OPTIONS, "DENY")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TelegramLinkAccountTests(TestCase):
    """HIGH #1: the Telegram bot's old `/link <username>` bound the sender's
    chat to ANY account by username alone. The account side of the replacement:
    telegram_id is now writable ONLY through the verified one-time-code flow
    (telegram_link_view), never through any profile form, and the code follows
    the same hashed-at-rest pattern as the reset/verification tokens."""

    def setUp(self):
        cache.clear()
        self.user = Users.objects.create_user(
            username="tg_link_user", email="tg_link@example.com", password="Volunteer2026!", is_client=True,
        )

    def tearDown(self):
        cache.clear()

    # --- token: raw returned to the UI, only the hash stored -----------------
    def test_link_token_is_not_stored_in_plaintext(self):
        raw_code = self.user.generate_telegram_link_token()
        self.user.refresh_from_db()
        self.assertNotEqual(self.user.telegram_link_token, raw_code)
        self.assertEqual(self.user.telegram_link_token, hash_token(raw_code))

    def test_link_token_is_cryptographically_long(self):
        raw_code = self.user.generate_telegram_link_token()
        # secrets.token_urlsafe(32) -> 43 url-safe chars, matching the reset token.
        self.assertGreaterEqual(len(raw_code), 43)

    def test_regenerating_replaces_the_stored_hash(self):
        first = self.user.generate_telegram_link_token()
        self.user.refresh_from_db()
        first_hash = self.user.telegram_link_token
        second = self.user.generate_telegram_link_token()
        self.user.refresh_from_db()
        self.assertNotEqual(first, second)
        self.assertNotEqual(self.user.telegram_link_token, first_hash)

    def test_expired_link_token_is_invalid(self):
        self.user.generate_telegram_link_token()
        self.assertTrue(self.user.telegram_link_token_is_valid())
        self.user.telegram_link_token_created_at = timezone.now() - timezone.timedelta(minutes=11)
        self.user.save(update_fields=["telegram_link_token_created_at"])
        self.assertFalse(self.user.telegram_link_token_is_valid())

    def test_cleared_link_token_is_invalid(self):
        self.user.generate_telegram_link_token()
        self.user.clear_telegram_link_token()
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_link_token)
        self.assertFalse(self.user.telegram_link_token_is_valid())

    # --- web flow ----------------------------------------------------------
    def test_link_page_requires_login(self):
        self.assertEqual(self.client.get(reverse("telegram_link")).status_code, 302)

    def test_post_mints_a_token_and_shows_the_raw_code_exactly_once(self):
        self.client.login(username="tg_link_user", password="Volunteer2026!")
        response = self.client.post(reverse("telegram_link"))
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.telegram_link_token_is_valid())
        # the emailed/rendered value is the raw code; the DB value (a hash) is not on the page
        self.assertContains(response, "/link ")
        self.assertNotContains(response, self.user.telegram_link_token)
        # a follow-up GET does not re-show a code and does not mint another
        stored = self.user.telegram_link_token
        follow_up = self.client.get(reverse("telegram_link"))
        self.assertNotContains(follow_up, "/link ")
        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_link_token, stored)

    def test_get_does_not_mint_a_token(self):
        self.client.login(username="tg_link_user", password="Volunteer2026!")
        self.client.get(reverse("telegram_link"))
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_link_token)

    def test_unlink_requires_post(self):
        self.client.login(username="tg_link_user", password="Volunteer2026!")
        self.assertEqual(self.client.get(reverse("telegram_unlink")).status_code, 405)

    def test_unlink_clears_the_binding_and_any_pending_code(self):
        self.user.telegram_id = 5000
        self.user.save(update_fields=["telegram_id"])
        self.user.generate_telegram_link_token()
        self.client.login(username="tg_link_user", password="Volunteer2026!")
        response = self.client.post(reverse("telegram_unlink"))
        self.assertRedirects(response, reverse("telegram_link"))
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_id)
        self.assertIsNone(self.user.telegram_link_token)

    def test_one_user_cannot_mint_or_clear_another_users_token(self):
        victim = Users.objects.create_user(
            username="tg_victim", email="tg_victim@example.com", password="Volunteer2026!", is_client=True,
        )
        victim.telegram_id = 4321
        victim.save(update_fields=["telegram_id"])
        self.client.login(username="tg_link_user", password="Volunteer2026!")
        # the views only ever act on request.user; there is no id/username parameter
        self.client.post(reverse("telegram_link"))
        self.client.post(reverse("telegram_unlink"))
        victim.refresh_from_db()
        self.assertEqual(victim.telegram_id, 4321)

    # --- every unverified write path to telegram_id is closed --------------
    def test_telegram_id_is_not_settable_via_edit_profile(self):
        self.client.login(username="tg_link_user", password="Volunteer2026!")
        self.client.post(reverse("edit_profile"), {
            "email": self.user.email, "region": "dushanbe", "telegram_id": "999999",
            "full_name": "X", "bio": "", "availability_status": "available",
        })
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_id)

    def test_telegram_id_is_not_settable_via_update_profile_endpoint(self):
        self.client.login(username="tg_link_user", password="Volunteer2026!")
        self.client.post(reverse("update_profile"), {"telegram_id": "888888"})
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_id)

    def test_registration_ignores_a_submitted_telegram_id(self):
        response = self.client.post(reverse("register"), {
            "username": "tg_newbie", "email": "tg_newbie@example.com", "role": "client",
            "region": "dushanbe", "telegram_id": "777777",
            "password": "Volunteer2026!", "confirm_password": "Volunteer2026!",
        })
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(Users.objects.get(username="tg_newbie").telegram_id)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PasswordResetPolicyTests(TestCase):
    """MEDIUM (extra finding): reset_password only checked length >= 8, so the
    reset link was a way around AUTH_PASSWORD_VALIDATORS (common-password /
    numeric-only / attribute-similarity blocklists that registration enforces)."""

    def setUp(self):
        self.user = Users.objects.create_user(
            username="pw_reset_user", email="pw_reset_user@example.com",
            password="Volunteer2026!", is_client=True,
        )

    def _reset(self, new_password):
        raw_token = self.user.generate_reset_password_token()
        return self.client.post(
            reverse("reset_password", args=[raw_token]),
            {"new_password": new_password, "confirm_password": new_password},
        )

    def test_reset_rejects_common_password(self):
        response = self._reset("password")
        self.assertEqual(response.status_code, 200)  # re-rendered, not redirected
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Volunteer2026!"))  # unchanged

    def test_reset_rejects_numeric_only_password(self):
        response = self._reset("48571903726")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Volunteer2026!"))

    def test_reset_rejects_password_similar_to_username(self):
        response = self._reset("pw_reset_user1")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Volunteer2026!"))

    def test_reset_accepts_a_strong_password_and_consumes_the_token(self):
        response = self._reset("Str0ng-Passphrase-42")
        self.assertRedirects(response, reverse("login"))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Str0ng-Passphrase-42"))
        self.assertIsNone(self.user.reset_password_token)

    def test_rejected_weak_attempt_does_not_consume_the_token(self):
        raw_token = self.user.generate_reset_password_token()
        weak = self.client.post(
            reverse("reset_password", args=[raw_token]),
            {"new_password": "password", "confirm_password": "password"},
        )
        self.assertEqual(weak.status_code, 200)
        # the same link still works for a strong password
        strong = self.client.post(
            reverse("reset_password", args=[raw_token]),
            {"new_password": "Str0ng-Passphrase-42", "confirm_password": "Str0ng-Passphrase-42"},
        )
        self.assertRedirects(strong, reverse("login"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmailVerificationTokenLifecycleTests(TestCase):
    """Regression: an email-verification link must expire (24h) and must be
    single-use, mirroring the password-reset guarantees."""

    def setUp(self):
        self.user = Users.objects.create_user(
            username="verify_user", email="verify_user@example.com",
            password="Volunteer2026!", is_client=True,
        )

    def test_expired_verification_link_is_rejected(self):
        raw_token = self.user.generate_email_verification_token()
        self.user.email_verification_token_created_at = timezone.now() - timezone.timedelta(hours=25)
        self.user.save(update_fields=["email_verification_token_created_at"])
        self.client.get(reverse("confirm_email", args=[raw_token]))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_email_verified)

    def test_verification_link_is_single_use(self):
        raw_token = self.user.generate_email_verification_token()
        self.client.get(reverse("confirm_email", args=[raw_token]))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_email_verified)
        self.assertIsNone(self.user.email_verification_token)
        # a replay of the same link finds no matching account
        self.user.is_email_verified = False
        self.user.save(update_fields=["is_email_verified"])
        self.client.get(reverse("confirm_email", args=[raw_token]))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_email_verified)

    def test_tampered_token_is_rejected(self):
        self.user.generate_email_verification_token()
        self.client.get(reverse("confirm_email", args=["not-a-real-token"]))
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_email_verified)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class UpdateProfileUploadGuardTests(TestCase):
    """The update-profile JSON endpoint writes profile.image directly (no form,
    so no field validators) — the upload-size cap must be enforced there too,
    or it is an unauthenticated-size disk-exhaustion vector for any logged-in
    user."""

    def setUp(self):
        self.user = Users.objects.create_user(
            username="upload_user", email="upload_user@example.com",
            password="Volunteer2026!", is_client=True,
        )
        self.client.login(username="upload_user", password="Volunteer2026!")

    def test_oversized_image_is_rejected_with_400(self):
        oversized = SimpleUploadedFile(
            "big.jpg", b"x" * (MAX_UPLOAD_SIZE_BYTES + 1), content_type="image/jpeg",
        )
        response = self.client.post(reverse("update_profile"), {"image": oversized})
        self.assertEqual(response.status_code, 400)
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.image)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class UpdateProfileInputValidationTests(TestCase):
    """Stage 7: the update-profile JSON endpoint has no ModelForm, so it wrote
    `region` and `age` straight to the DB. An arbitrary region string broke
    region filtering/matching for that user; a non-numeric age 500'd on save."""

    def setUp(self):
        self.user = Users.objects.create_user(
            username="upv_user", email="upv_user@example.com",
            password="Volunteer2026!", is_volunteer=True, region="dushanbe",
        )
        self.client.login(username="upv_user", password="Volunteer2026!")

    def test_invalid_region_is_ignored(self):
        resp = self.client.post(reverse("update_profile"), {"region": "not-a-real-region"})
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.region, "dushanbe")  # unchanged

    def test_valid_region_is_accepted(self):
        self.client.post(reverse("update_profile"), {"region": "sogd"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.region, "sogd")

    def test_non_numeric_age_returns_400_not_500(self):
        resp = self.client.post(reverse("update_profile"), {"age": "twenty"})
        self.assertEqual(resp.status_code, 400)

    def test_out_of_range_age_is_ignored(self):
        self.client.post(reverse("update_profile"), {"age": "500"})
        self.user.profile.refresh_from_db()
        self.assertIsNone(self.user.profile.age)
