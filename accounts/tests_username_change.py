"""A user changes their own username on the edit-profile page: registration's
rules (4–30 characters), unique ignoring case, a clear EN/RU/TJ error when the
name is taken — and everything keyed by the account keeps working afterwards
(login, rating, chats, reviews, Telegram binding)."""

import re
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts.models import Users

PASSWORD = "Username-Change-2026!"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username.lower()}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


class UsernameChangeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = _make("old_name", is_volunteer=True, telegram_id=555000111)
        self.other = _make("Taken_Name", is_volunteer=True)
        self.client.force_login(self.user)

    def _post(self, **data):
        payload = {
            "username": "old_name", "email": self.user.email, "region": "dushanbe",
            "full_name": "", "bio": "", "availability_status": "available",
        }
        payload.update(data)
        return self.client.post(reverse("edit_profile"), payload)

    def _error_codes(self, response):
        return [e.code for e in response.context["user_form"].errors.as_data().get("username", [])]

    def test_edit_page_shows_the_field_with_the_hint(self):
        html = self.client.get(reverse("edit_profile")).content.decode()
        self.assertIn('name="username"', html)
        self.assertIn('value="old_name"', html)
        self.assertIn('maxlength="30"', html)
        self.assertIn('data-i18n="edit.username_hint"', html)

    def test_change_to_a_free_name_and_sign_in_with_it(self):
        with self.assertLogs("accounts.views", "INFO") as logs:
            response = self._post(username="  new_name  ")
        self.assertRedirects(response, reverse("profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "new_name")
        self.assertEqual(self.user.telegram_id, 555000111)  # the Telegram binding is kept
        self.assertIn(f"user #{self.user.pk} changed their username", " ".join(logs.output))
        self.assertNotIn("new_name", " ".join(logs.output))  # IDs only
        # Still signed in on this session; a new sign-in takes the new name only.
        self.assertEqual(self.client.get(reverse("profile")).status_code, 200)
        self.client.logout()
        self.assertTrue(self.client.login(username="new_name", password=PASSWORD))
        self.client.logout()
        self.assertFalse(self.client.login(username="old_name", password=PASSWORD))

    def test_rating_and_chat_show_the_new_name(self):
        from myapp.services import chat, rating

        old = {row["user_id"]: row["name"] for row in rating.cached()["rows"]}  # cached with the old name
        self.assertEqual(old[self.user.pk], "old_name")
        self._post(username="new_name")
        self.user.refresh_from_db()
        names = {row["user_id"]: row["name"] for row in rating.cached()["rows"]}
        self.assertEqual(names[self.user.pk], "new_name")
        self.assertEqual(chat.display_name(self.user), "new_name")

    def test_taken_name_is_refused_ignoring_case(self):
        for candidate in ("Taken_Name", "taken_name", "TAKEN_NAME"):
            response = self._post(username=candidate)
            self.assertEqual(response.status_code, 200, candidate)
            self.assertEqual(self._error_codes(response), ["username_taken"], candidate)
            self.assertContains(response, 'data-i18n="err.username_taken"')
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "old_name")

    def test_own_name_in_another_case_is_allowed(self):
        self.assertRedirects(self._post(username="Old_Name"), reverse("profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "Old_Name")

    def test_registration_length_rules(self):
        for candidate, code in (("abc", "username_too_short"), ("x" * 31, "max_length"), ("   ", "required")):
            response = self._post(username=candidate)
            self.assertEqual(response.status_code, 200, candidate)
            self.assertEqual(self._error_codes(response), [code], candidate)
        self.assertRedirects(self._post(username="x" * 30), reverse("profile"))

    def test_not_posting_the_field_keeps_the_name(self):
        response = self.client.post(reverse("edit_profile"), {
            "email": self.user.email, "region": "khatlon", "full_name": "", "bio": "",
            "availability_status": "available",
        })
        self.assertRedirects(response, reverse("profile"))
        self.user.refresh_from_db()
        self.assertEqual((self.user.username, self.user.region), ("old_name", "khatlon"))

    def test_a_legacy_name_outside_the_rules_does_not_block_other_edits(self):
        legacy = _make("abc")  # 3 characters, created before the 4-character rule
        Users.objects.filter(pk=legacy.pk).update(username="y" * 40)
        legacy.refresh_from_db()
        self.client.force_login(legacy)
        response = self.client.post(reverse("edit_profile"), {
            "username": legacy.username, "email": legacy.email, "region": "sogd",
            "full_name": "", "bio": "", "availability_status": "available",
        })
        self.assertRedirects(response, reverse("profile"))
        legacy.refresh_from_db()
        self.assertEqual(legacy.region, "sogd")

    def test_reviews_and_tasks_follow_the_account(self):
        from myapp.models import HelpRequest

        client_user = _make("some_client", is_client=True)
        task = HelpRequest.objects.create(
            client=client_user, volunteer=self.user, status="completed", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )
        self._post(username="new_name")
        task.refresh_from_db()
        self.assertEqual(task.volunteer.username, "new_name")

    def test_no_url_is_built_from_a_username(self):
        """So an old username can't leave a broken link: no route takes one."""
        from django.urls import get_resolver

        def walk(patterns, prefix=""):
            for p in patterns:
                if hasattr(p, "url_patterns"):
                    yield from walk(p.url_patterns, prefix + str(p.pattern))
                else:
                    yield prefix + str(p.pattern)

        routes = list(walk(get_resolver().url_patterns))
        self.assertFalse([r for r in routes if re.search(r"<(\w+:)?\w*user(name)?\w*>", r)], routes)

    def test_hint_is_translated_in_three_languages(self):
        source = (Path(settings.BASE_DIR) / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
        self.assertEqual(source.count('"edit.username_hint":'), 3)
