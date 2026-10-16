"""Sign-up / sign-in calls to action are for visitors only (2026-10-14).

Locks: a signed-in user of any role sees no "Become a volunteer" / "Login"
anywhere on the landing page (hero, volunteers section, the "Two ways"
band, footer, header) — the dashboard instead, plus the role's own action
(client → request help, volunteer → find tasks); the "Two ways" band is
for visitors only — a signed-in user gets no band at all (its "Glad to have
you with us" variant was removed the same day); /login/ and /register/ send
a signed-in user to the dashboard; every new string exists in EN/RU/TJ.
"""

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Users

LOCMEM = "django.core.mail.backends.locmem.EmailBackend"
PASSWORD = "Auth-Cta-Tests-2026!"
NEW_KEYS = ("nav.cabinet",)


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


@override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_BACKEND=LOCMEM)
class AuthCtaBase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.users = {
            "client": _make("cta_client", is_client=True),
            "volunteer": _make("cta_volunteer", is_volunteer=True),
            "curator": _make("cta_curator", is_curator=True),
            "admin": _make("cta_admin", is_superuser=True, is_staff=True),
            "applicant": _make("cta_applicant"),  # registered as volunteer, application not approved yet
        }

    def about(self):
        response = self.client.get(reverse("about"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class AnonymousSeesSignUpTests(AuthCtaBase):
    def test_landing_has_register_and_login(self):
        html = self.client.get(reverse("home")).content.decode()
        # Prefix match: the landing's volunteer CTAs carry ?role=volunteer.
        register = f'href="{reverse("register")}'
        # header, hero, volunteers section, "Two ways" card, footer
        self.assertGreaterEqual(html.count(register), 5)
        self.assertIn(f'href="{reverse("login")}" data-i18n="nav.login"', html)
        self.assertIn('data-i18n="cta.title_1"', html)
        self.assertIn('data-i18n="cta.volunteer_title"', html)
        self.assertIn('data-i18n="cta.client_title"', html)
        self.assertIn('<section class="cta-band">', html)
        self.assertNotIn('data-i18n="nav.cabinet"', html)


class SignedInSeesDashboardTests(AuthCtaBase):
    def test_no_sign_up_or_login_for_any_role(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user)
                html = self.about()
                self.assertNotIn(f'href="{reverse("register")}', html)
                self.assertNotIn('data-i18n="hero.join"', html)
                self.assertNotIn('data-i18n="nav.login"', html)
                self.assertNotIn('data-i18n="cta.volunteer_title"', html)
                self.assertNotIn('data-i18n="cta.title_1"', html)
                self.assertNotIn('class="cta-band"', html)
                self.assertNotIn("Рады, что вы", html)
                # hero, volunteers section, footer
                self.assertGreaterEqual(html.count(f'href="{reverse("dashboard")}" data-i18n="nav.cabinet"'), 3)
                # header logout stays a POST form
                self.assertIn(f'<form method="post" action="{reverse("logout")}" class="nav-logout-form">', html)

    def test_client_keeps_request_help_in_hero(self):
        self.client.force_login(self.users["client"])
        html = self.about()
        self.assertIn(f'href="{reverse("create_request")}" data-i18n="hero.request"', html)

    def test_volunteer_gets_find_tasks_not_request_help(self):
        self.client.force_login(self.users["volunteer"])
        html = self.about()
        self.assertIn(f'href="{reverse("task_list")}" data-i18n="nav.requests"', html)
        self.assertNotIn('data-i18n="hero.request"', html)  # create_request is client-only (403)

    def test_staff_and_applicant_get_no_request_help(self):
        for role in ("curator", "admin", "applicant"):
            with self.subTest(role=role):
                self.client.force_login(self.users[role])
                self.assertNotIn('data-i18n="hero.request"', self.about())

    def test_russian_is_prerendered(self):
        self.client.force_login(self.users["volunteer"])
        html = self.about()
        self.assertIn("Личный кабинет", html)


class SignedInRedirectTests(AuthCtaBase):
    def test_home_login_register_redirect_to_dashboard(self):
        self.client.force_login(self.users["volunteer"])
        for name in ("home", "login", "register"):
            with self.subTest(url=name):
                self.assertRedirects(self.client.get(reverse(name)), reverse("dashboard"), fetch_redirect_response=False)

    def test_anonymous_login_and_register_render(self):
        for name in ("login", "register"):
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class NewKeysTests(TestCase):
    def test_every_new_key_exists_in_every_language(self):
        from server.i18n_prerender import translation_table

        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key in NEW_KEYS:
                self.assertTrue(table.get(key), f"{lang} {key}")
