"""Dashboard (/dashboard/, "what do I do now") and profile (/profile/, "who I
am") are two separate pages since 2026-10-15. These tests lock the split: both
login-only, both render for every role, each carries only its own blocks, and
login lands on the dashboard."""

from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users

PASSWORD = "Split-Tests-2026!"

ROLE_PARTIALS = {
    "volunteer": "myapp/dashboard/_volunteer.html",
    "client": "myapp/dashboard/_client.html",
    "curator": "myapp/dashboard/_curator.html",
    "admin": "myapp/dashboard/_admin.html",
}


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


class DashboardProfileSplitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.users = {
            "volunteer": _make("split_vol", is_volunteer=True),
            "client": _make("split_cli", is_client=True),
            "curator": _make("split_cur", is_curator=True),
            "admin": _make("split_adm", is_superuser=True, is_staff=True),
        }

    def test_urls(self):
        self.assertEqual(reverse("dashboard"), "/dashboard/")
        self.assertEqual(reverse("profile"), "/profile/")

    def test_both_pages_require_login(self):
        for name in ("dashboard", "profile"):
            with self.subTest(page=name):
                url = reverse(name)
                self.assertRedirects(self.client.get(url), f"{reverse('login')}?next={url}",
                                     fetch_redirect_response=False)

    def test_old_dashboard_url_redirects_to_the_new_one(self):
        self.client.force_login(self.users["client"])
        self.assertRedirects(self.client.get("/myapp/dashboard/"), reverse("dashboard"))

    def test_login_lands_on_the_dashboard(self):
        response = self.client.post(reverse("login"), {"username": "split_cli", "password": PASSWORD})
        self.assertRedirects(response, reverse("dashboard"))
        # An already signed-in visitor of /login/ and / goes there too.
        self.assertRedirects(self.client.get(reverse("login")), reverse("dashboard"))
        self.assertRedirects(self.client.get("/"), reverse("dashboard"))

    def test_dashboard_renders_its_role_block_for_every_role(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user)
                response = self.client.get(reverse("dashboard"))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "myapp/dashboard.html")
                self.assertTemplateUsed(response, ROLE_PARTIALS[role])
                self.assertEqual(response.context["dash_role"], role)
                self.assertContains(response, 'class="card dash-hero')
                self.assertContains(response, 'data-i18n="profile.quick"')
                # No profile card on the dashboard.
                self.assertNotContains(response, 'data-i18n="profile.edit"')
                self.assertNotContains(response, 'data-i18n="tg.title"')
                self.assertNotContains(response, 'id="profileLocationCard"')
                self.assertNotContains(response, 'class="badge-grid"')
                self.assertNotContains(response, "act-card")

    def test_profile_renders_for_every_role_without_dashboard_blocks(self):
        for role, user in self.users.items():
            with self.subTest(role=role):
                self.client.force_login(user)
                response = self.client.get(reverse("profile"))
                self.assertEqual(response.status_code, 200)
                self.assertTemplateUsed(response, "accounts/profile.html")
                for partial in ROLE_PARTIALS.values():
                    self.assertTemplateNotUsed(response, partial)
                self.assertNotIn("dash_role", response.context)
                self.assertContains(response, 'data-i18n="profile.edit"')
                self.assertContains(response, f'href="{reverse("edit_profile")}"')
                self.assertContains(response, f'href="{reverse("telegram_link")}"')
                self.assertNotContains(response, 'class="card dash-hero')
                self.assertNotContains(response, 'data-i18n="profile.quick"')
                self.assertNotContains(response, "css/dashboard.css")

    def test_profile_never_builds_the_role_dashboard(self):
        with mock.patch("myapp.services.dashboard.for_user", side_effect=AssertionError("not on the profile")):
            for role, user in self.users.items():
                with self.subTest(role=role):
                    self.client.force_login(user)
                    self.assertEqual(self.client.get(reverse("profile")).status_code, 200)

    def test_volunteer_profile_blocks(self):
        self.client.force_login(self.users["volunteer"])
        response = self.client.get(reverse("profile"))
        self.assertContains(response, 'id="profileLocationCard"')
        self.assertContains(response, 'class="badge-grid"')
        self.assertContains(response, "act-card")
        self.assertContains(response, 'data-i18n="profile.reviews_title"')
        self.assertContains(response, f'href="{reverse("my_reviews")}"')
        self.assertContains(response, "js/profile-location.js")
        self.assertContains(response, "js/achievements.js")

    def test_client_profile_has_no_volunteer_blocks(self):
        self.client.force_login(self.users["client"])
        response = self.client.get(reverse("profile"))
        self.assertContains(response, "act-card")
        for marker in ('id="profileLocationCard"', 'class="badge-grid"', 'data-i18n="profile.reviews_title"'):
            self.assertNotContains(response, marker)

    def test_stale_location_is_a_banner_on_the_dashboard(self):
        volunteer = self.users["volunteer"]
        self.client.force_login(volunteer)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, 'data-i18n="dash.loc_stale"')
        self.assertContains(response, f'href="{reverse("profile")}#profileLocationCard"')
        self.assertNotContains(response, 'id="profileLocationCard"')

        Profile.objects.filter(user=volunteer).update(
            latitude=38.5, longitude=68.7, location_updated_at=timezone.now() - timedelta(hours=1)
        )
        self.assertNotContains(self.client.get(reverse("dashboard")), 'data-i18n="dash.loc_stale"')

    def test_no_location_banner_for_other_roles(self):
        for role in ("client", "curator", "admin"):
            with self.subTest(role=role):
                self.client.force_login(self.users[role])
                self.assertNotContains(self.client.get(reverse("dashboard")), 'data-i18n="dash.loc_stale"')

    def test_nav_dashboard_link_and_avatar(self):
        self.client.force_login(self.users["client"])
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(f'href="{reverse("dashboard")}" data-i18n="nav.dashboard" aria-current="page"', html)
        self.assertIn(f'class="nav-avatar" href="{reverse("profile")}"', html)
        html = self.client.get(reverse("profile")).content.decode()
        self.assertNotIn('data-i18n="nav.dashboard" aria-current="page"', html)

    def test_dashboard_is_not_indexed(self):
        robots = self.client.get("/robots.txt").content.decode()
        self.assertIn("Disallow: /dashboard/", robots)
        self.assertIn("Disallow: /profile/", robots)
        self.client.force_login(self.users["client"])
        self.assertContains(self.client.get(reverse("dashboard")), "noindex, nofollow")

    def test_new_strings_exist_in_all_three_languages(self):
        from pathlib import Path

        from django.conf import settings

        source = (Path(settings.BASE_DIR) / "static/js/i18n.js").read_text(encoding="utf-8")
        for key in ("dash.loc_stale", "dash.loc_stale_cta", "profile.lede", "profile.reviews_title",
                    "va.back_dashboard"):
            with self.subTest(key=key):
                self.assertEqual(source.count(f'"{key}"'), 3)
