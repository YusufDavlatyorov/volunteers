"""Continue with Google (accounts/google_oauth.py + the google_* views).

Google's token endpoint is mocked (``accounts.google_oauth.requests.post``);
no test reaches the network.
"""

import base64
import json
import time
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import google_oauth
from accounts.models import Profile, Users
from myapp.models import VolunteerApplication

CLIENT_ID = "test-client.apps.googleusercontent.com"
GOOGLE = override_settings(GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET="test-secret")


def _jwt(claims):
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def _claims(nonce, **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com", "aud": CLIENT_ID, "sub": "1234567890",
        "email": "Aziz@Gmail.com", "email_verified": True, "name": "Aziz Karimov",
        "iat": now, "exp": now + 3600, "nonce": nonce,
    }
    claims.update(overrides)
    return claims


class _TokenResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@GOOGLE
class GoogleFlowTests(TestCase):
    def setUp(self):
        cache.clear()

    def _start(self, **params):
        response = self.client.get(reverse("google_login"), params)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response["Location"]).query)
        return query["state"][0], query["nonce"][0], query

    def _callback(self, next_url=None, **claim_overrides):
        state, nonce, _ = self._start(**({"next": next_url} if next_url else {}))
        claims = _claims(nonce)
        claims.update(claim_overrides)
        with mock.patch("accounts.google_oauth.requests.post",
                        return_value=_TokenResponse({"id_token": _jwt(claims)})) as post:
            response = self.client.get(reverse("google_callback"), {"state": state, "code": "abc"})
        return response, post

    def test_button_only_when_configured(self):
        self.assertContains(self.client.get(reverse("login")), reverse("google_login"))
        self.assertContains(self.client.get(reverse("register")), reverse("google_login"))
        with override_settings(GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET=""):
            self.assertNotContains(self.client.get(reverse("login")), "google-btn")
            self.assertEqual(self.client.get(reverse("google_login")).status_code, 404)
            self.assertEqual(self.client.get(reverse("google_callback")).status_code, 404)

    def test_authorization_redirect_has_pkce_state_nonce(self):
        _, _, query = self._start()
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["scope"], ["openid email profile"])
        self.assertTrue(query["redirect_uri"][0].endswith("/auth/google/callback/"))

    def test_new_user_goes_to_signup_then_client_account(self):
        response, post = self._callback()
        self.assertRedirects(response, reverse("google_signup"))
        # The PKCE verifier and our redirect URI went to Google's token endpoint.
        sent = post.call_args.kwargs["data"]
        self.assertTrue(sent["code_verifier"])
        self.assertEqual(sent["code"], "abc")
        self.assertFalse(Users.objects.exists())

        page = self.client.get(reverse("google_signup"))
        self.assertContains(page, "aziz@gmail.com")
        self.assertContains(page, 'value="aziz"')  # suggested username

        response = self.client.post(reverse("google_signup"),
                                    {"role": "client", "username": "aziz_k", "region": "sogd"})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        user = Users.objects.get(username="aziz_k")
        self.assertEqual(user.email, "aziz@gmail.com")
        self.assertEqual(user.google_sub, "1234567890")
        self.assertTrue(user.is_client)
        self.assertTrue(user.is_email_verified)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(Profile.objects.get(user=user).full_name, "Aziz Karimov")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)
        self.assertNotIn(google_oauth.SESSION_PENDING_KEY, self.client.session)

    def test_volunteer_signup_files_an_application_not_the_role(self):
        self._callback()
        self.client.post(reverse("google_signup"), {
            "role": "volunteer", "username": "aziz_v", "region": "dushanbe", "motivation": "Хочу помогать"})
        user = Users.objects.get(username="aziz_v")
        self.assertFalse(user.is_volunteer)
        self.assertEqual(VolunteerApplication.objects.get(user=user).reason, "Хочу помогать")

    def test_signup_validates_username(self):
        Users.objects.create_user("taken_name", "other@example.com", "x-Strong-pass-1")
        self._callback()
        response = self.client.post(reverse("google_signup"),
                                    {"role": "client", "username": "taken_name", "region": "sogd"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Users.objects.count(), 1)

    def test_returning_google_user_logs_straight_in(self):
        user = Users.objects.create_user("aziz", "aziz@gmail.com", None, google_sub="1234567890", is_client=True)
        response, _ = self._callback(email="new-address@gmail.com")
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_existing_password_account_is_linked_by_verified_email(self):
        user = Users.objects.create_user("aziz", "aziz@gmail.com", "x-Strong-pass-1", is_client=True)
        response, _ = self._callback()
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)
        user.refresh_from_db()
        self.assertEqual(user.google_sub, "1234567890")
        self.assertTrue(user.is_email_verified)
        self.assertTrue(user.has_usable_password())  # the password still works

    def test_unverified_google_email_never_links_or_signs_up(self):
        Users.objects.create_user("aziz", "aziz@gmail.com", "x-Strong-pass-1")
        response, _ = self._callback(email_verified=False)
        self.assertRedirects(response, reverse("register"))
        self.assertIsNone(Users.objects.get().google_sub)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_email_tied_to_another_google_account_is_refused(self):
        Users.objects.create_user("aziz", "aziz@gmail.com", "x", google_sub="someone-else")
        response, _ = self._callback()
        self.assertRedirects(response, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_account_is_refused(self):
        Users.objects.create_user("aziz", "aziz@gmail.com", None, google_sub="1234567890", is_active=False)
        response, _ = self._callback()
        self.assertRedirects(response, reverse("login"))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_state_is_rejected_without_calling_google(self):
        self._start()
        with mock.patch("accounts.google_oauth.requests.post") as post:
            response = self.client.get(reverse("google_callback"), {"state": "forged", "code": "abc"})
        self.assertRedirects(response, reverse("login"))
        post.assert_not_called()

    def test_state_is_single_use(self):
        state, nonce, _ = self._start()
        token = _jwt(_claims(nonce))
        with mock.patch("accounts.google_oauth.requests.post", return_value=_TokenResponse({"id_token": token})):
            self.client.get(reverse("google_callback"), {"state": state, "code": "abc"})
            self.client.session.flush()
            replay = self.client.get(reverse("google_callback"), {"state": state, "code": "abc"})
        self.assertRedirects(replay, reverse("login"))

    def test_bad_token_claims_are_rejected(self):
        for overrides in ({"aud": "someone-else"}, {"iss": "https://evil.example"},
                          {"exp": int(time.time()) - 3600}, {"nonce": "other"}):
            with self.subTest(overrides=overrides):
                self.client.session.flush()
                response, _ = self._callback(**overrides)
                self.assertRedirects(response, reverse("login"))
                self.assertNotIn(google_oauth.SESSION_PENDING_KEY, self.client.session)

    def test_google_error_and_cancel(self):
        self._start()
        response = self.client.get(reverse("google_callback"), {"error": "access_denied"})
        self.assertRedirects(response, reverse("login"))
        state, _, _ = self._start()
        with mock.patch("accounts.google_oauth.requests.post",
                        return_value=_TokenResponse({"error": "invalid_grant"}, status=400)):
            response = self.client.get(reverse("google_callback"), {"state": state, "code": "abc"})
        self.assertRedirects(response, reverse("login"))

    def test_next_survives_the_round_trip(self):
        """Like the password login: "Request help" → login?next=… → Google → the form."""
        target = reverse("create_request")
        login_page = self.client.get(reverse("login"), {"next": target})
        self.assertContains(login_page, f'{reverse("google_login")}?next={target}')
        Users.objects.create_user("aziz", "aziz@gmail.com", None, google_sub="1234567890", is_client=True)
        response, _ = self._callback(next_url=target)
        self.assertRedirects(response, target, fetch_redirect_response=False)

    def test_new_account_lands_on_next_unless_it_cannot_use_it(self):
        target = reverse("create_request")
        self._callback(next_url=target)
        response = self.client.post(reverse("google_signup"),
                                    {"role": "client", "username": "aziz_k", "region": "sogd"})
        self.assertRedirects(response, target, fetch_redirect_response=False)
        self.client.logout()
        cache.clear()
        self._callback(next_url=target, sub="999", email="vol@gmail.com")
        response = self.client.post(reverse("google_signup"),
                                    {"role": "volunteer", "username": "vol_v", "region": "sogd"})
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_offsite_next_is_dropped(self):
        Users.objects.create_user("aziz", "aziz@gmail.com", None, google_sub="1234567890", is_client=True)
        response, _ = self._callback(next_url="https://evil.example/")
        self.assertRedirects(response, reverse("dashboard"), fetch_redirect_response=False)

    def test_signup_without_pending_identity_redirects(self):
        self.assertRedirects(self.client.get(reverse("google_signup")), reverse("login"))


class SuggestUsernameTests(TestCase):
    def test_short_or_symbolic_local_parts(self):
        ident = google_oauth.GoogleIdentity(sub="1", email="a.b@x.com", email_verified=True)
        name = google_oauth.suggest_username(ident, lambda n: False)
        self.assertGreaterEqual(len(name), 4)
        ident = google_oauth.GoogleIdentity(sub="1", email="++@x.com", email_verified=True)
        self.assertEqual(google_oauth.suggest_username(ident, lambda n: False), "user")

    def test_taken_name_gets_a_suffix(self):
        ident = google_oauth.GoogleIdentity(sub="1", email="aziz@x.com", email_verified=True)
        name = google_oauth.suggest_username(ident, lambda n: n == "aziz")
        self.assertTrue(name.startswith("aziz") and name != "aziz")
        self.assertLessEqual(len(name), Users.USERNAME_MAX_LENGTH)
