"""Smart Volunteer Matching card on task_detail (hotfix 2026-10-19).

On production the card showed only its header: the candidate rows the page
script adds after the JSON fetch carried `.reveal`, which base.html's scroll
observer never sees (it collects its targets once, at load), so they stayed at
opacity 0. These tests lock the endpoint (candidates, the empty-state reason,
notify / assign, who may call it) and the page script itself.
"""

import re
from pathlib import Path

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import HelpRequest
from myapp.services import matching

TASK_LAT, TASK_LNG = 38.5598, 68.7870
PASSWORD = "pass12345"


def _user(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SmartMatchingEndpointTests(TestCase):
    def setUp(self):
        self.admin = Users.objects.create_superuser(username="sm_admin", email="sm_admin@example.com", password=PASSWORD)
        self.curator = _user("sm_curator", is_curator=True)
        self.client_user = _user("sm_client", is_client=True)
        self.near = _user("sm_near", is_volunteer=True)
        self.near.profile.set_location(38.561, 68.788)
        self.far = _user("sm_far", is_volunteer=True)
        self.far.profile.set_location(38.70, 68.95)
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending", latitude=TASK_LAT, longitude=TASK_LNG,
        )

    def _get(self, username):
        self.client.login(username=username, password=PASSWORD)
        return self.client.get(reverse("task_recommendations", args=[self.task.pk]))

    def _reason(self):
        response = self._get("sm_admin")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["recommendations"], [])
        return data["empty_reason"]

    # --- candidates ------------------------------------------------------------

    def test_admin_and_curator_get_ranked_candidates(self):
        for username in ("sm_admin", "sm_curator"):
            with self.subTest(username=username):
                data = self._get(username).json()
                self.assertTrue(data["task_has_location"])
                self.assertIsNone(data["empty_reason"])
                self.assertEqual([r["username"] for r in data["recommendations"]], ["sm_near", "sm_far"])

    def test_each_candidate_carries_translatable_reason_codes(self):
        row = self._get("sm_admin").json()["recommendations"][0]
        self.assertEqual(len(row["reason_codes"]), len(row["reasons"]))
        self.assertIn("close", row["reason_codes"])
        for code, text in zip(row["reason_codes"], row["reasons"]):
            self.assertEqual(matching.REASON_TEXT[code].format(n=row["active_task_count"]), text)

    def test_ai_reason_texts_unchanged(self):
        # The AI assistant still reads the Russian texts.
        self.assertEqual(
            matching._reasons(1.0, Profile.AVAILABILITY_AVAILABLE, 2, True, "fresh", "match"),
            ["Совсем рядом", "Доступен сейчас", "2 активных задач(и)", "Тот же регион, что и запрос", "Подходит по навыкам"],
        )

    # --- empty state: always a reason, never a bare blank -------------------

    def test_empty_reason_task_without_location(self):
        HelpRequest.objects.filter(pk=self.task.pk).update(latitude=None, longitude=None)
        self.assertEqual(self._reason(), matching.EMPTY_NO_TASK_LOCATION)

    def test_empty_reason_no_volunteers(self):
        Users.objects.filter(is_volunteer=True).update(is_active=False)
        self.assertEqual(self._reason(), matching.EMPTY_NO_VOLUNTEERS)

    def test_empty_reason_everyone_offline_or_away(self):
        Profile.objects.filter(user=self.near).update(availability_status=Profile.AVAILABILITY_OFFLINE)
        Profile.objects.filter(user=self.far).update(is_away=True, away_until=timezone.now() + timezone.timedelta(days=2))
        self.assertEqual(self._reason(), matching.EMPTY_ALL_UNAVAILABLE)

    def test_empty_reason_no_saved_location(self):
        Profile.objects.filter(user__is_volunteer=True).update(latitude=None, longitude=None, location_updated_at=None)
        self.assertEqual(self._reason(), matching.EMPTY_NO_VOLUNTEER_LOCATION)

    # --- actions ---------------------------------------------------------------

    def test_curator_notify_sends_and_keeps_task_pending(self):
        self.client.login(username="sm_curator", password=PASSWORD)
        mail.outbox.clear()
        response = self.client.post(reverse("task_notify_volunteer", args=[self.task.pk, self.near.pk]))
        self.assertJSONEqual(response.content, {"success": True})
        self.assertEqual([m.to for m in mail.outbox], [[self.near.email]])
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.volunteer_id), ("pending", None))

    def test_admin_assign_activates_task(self):
        self.client.login(username="sm_admin", password=PASSWORD)
        response = self.client.post(reverse("task_assign_volunteer", args=[self.task.pk, self.near.pk]))
        self.assertJSONEqual(response.content, {"success": True})
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.volunteer_id), ("active", self.near.pk))
        # The assigned volunteer drops out of the next shortlist.
        HelpRequest.objects.filter(pk=self.task.pk).update(status="pending")
        names = [r["username"] for r in self._get("sm_admin").json()["recommendations"]]
        self.assertNotIn("sm_near", names)

    def test_assign_second_time_fails_cleanly(self):
        self.client.login(username="sm_curator", password=PASSWORD)
        self.client.post(reverse("task_assign_volunteer", args=[self.task.pk, self.near.pk]))
        response = self.client.post(reverse("task_assign_volunteer", args=[self.task.pk, self.far.pk]))
        self.assertFalse(response.json()["success"])
        self.task.refresh_from_db()
        self.assertEqual(self.task.volunteer_id, self.near.pk)

    # --- permissions: admin / curator only -------------------------------------

    def test_only_admin_and_curator_reach_the_endpoints(self):
        urls = [
            ("get", reverse("task_recommendations", args=[self.task.pk])),
            ("post", reverse("task_notify_volunteer", args=[self.task.pk, self.near.pk])),
            ("post", reverse("task_assign_volunteer", args=[self.task.pk, self.near.pk])),
        ]
        mail.outbox.clear()
        for username in ("sm_near", "sm_client", None):
            self.client.logout()
            if username:
                self.client.login(username=username, password=PASSWORD)
            for method, url in urls:
                with self.subTest(user=username, url=url):
                    response = getattr(self.client, method)(url)
                    self.assertEqual(response.status_code, 302)
                    self.assertNotEqual(response.get("Content-Type"), "application/json")
        self.task.refresh_from_db()
        self.assertEqual((self.task.status, self.task.volunteer_id), ("pending", None))
        self.assertEqual(mail.outbox, [])

    def test_actions_require_post(self):
        self.client.login(username="sm_admin", password=PASSWORD)
        for name in ("task_notify_volunteer", "task_assign_volunteer"):
            response = self.client.get(reverse(name, args=[self.task.pk, self.near.pk]))
            self.assertEqual(response.status_code, 405)

    def test_card_rendered_for_staff_only(self):
        detail = reverse("task_detail", args=[self.task.pk])
        for username, shown in (("sm_admin", True), ("sm_curator", True), ("sm_client", False)):
            self.client.login(username=username, password=PASSWORD)
            response = self.client.get(detail)
            with self.subTest(username=username):
                self.assertEqual(b'id="recommendCard"' in response.content, shown)


class SmartMatchingPageScriptTests(TestCase):
    """Static checks on the card's script in task_detail.html."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source = (Path(settings.BASE_DIR) / "templates/myapp/task_detail.html").read_text(encoding="utf-8")
        start = source.index("{% if show_recommendations %}\n<script>")
        cls.script = source[start:source.index("</script>", start)]

    def test_dynamic_rows_are_never_reveal(self):
        # The regression itself: base.html's observer never sees rows added later.
        self.assertNotRegex(self.script, r"""className\s*=\s*['"`][^'"`]*\breveal\b""")
        self.assertNotRegex(self.script, r"""el\(\s*['"]\w+['"]\s*,\s*['"][^'"]*\breveal\b""")
        self.assertNotIn("classList.add('reveal')", self.script)

    def test_no_script_anywhere_creates_reveal_elements(self):
        # Project-wide: every static JS file and every inline <script> block.
        # A class string with "reveal" in code means an element JS builds after
        # load — base.html's observer would never show it.
        base = Path(settings.BASE_DIR)
        sources = {p: p.read_text(encoding="utf-8") for p in (base / "static/js").rglob("*.js")
                   if p.name != "i18n.js" and "vendor" not in p.parts}
        for p in (base / "templates").rglob("*.html"):
            blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", p.read_text(encoding="utf-8"), re.S | re.I)
            sources[p] = "\n".join(blocks)
        class_string = re.compile(r"""['"`][^'"`\n]*\breveal\b[^'"`\n]*['"`]""")
        offenders = []
        for path, code in sources.items():
            code = re.sub(r"//[^\n]*|/\*.*?\*/", "", code, flags=re.S)  # comments may say ".reveal"
            for match in class_string.finditer(code):
                if "gc-reveal" not in match.group(0) and ".reveal" not in match.group(0):  # a selector, not a class
                    offenders.append(f"{path.relative_to(base)}: {match.group(0)}")
        self.assertEqual(offenders, [])

    def test_server_text_is_never_html(self):
        # full_name / region / reasons come from users — textContent only.
        self.assertNotRegex(self.script, r"innerHTML\s*=\s*`")
        self.assertNotRegex(self.script, r"innerHTML[^;\n]*item\.")

    def test_every_key_exists_in_all_three_languages(self):
        from server.i18n_prerender import translation_table

        keys = set(re.findall(r"""['"](rec\.[\w.]+)['"]""", self.script))
        keys -= {"rec.why."}
        keys |= {f"rec.why.{code}" for code in matching.REASON_TEXT}
        empty_codes = {
            matching.EMPTY_NO_TASK_LOCATION, matching.EMPTY_NO_VOLUNTEERS,
            matching.EMPTY_ALL_UNAVAILABLE, matching.EMPTY_NO_VOLUNTEER_LOCATION,
        }
        for code in empty_codes:
            self.assertIn(f"{code}:", self.script, f"no empty-state text for {code}")
        self.assertIn("rec.empty.load_failed", keys)
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            missing = sorted(k for k in keys if not table.get(k))
            with self.subTest(lang=lang):
                self.assertEqual(missing, [])
