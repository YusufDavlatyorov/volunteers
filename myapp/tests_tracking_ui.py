"""Feature Pack v2, milestone 2 — frontend wiring for live tracking (feature 7)
and route navigation (feature 2): who gets the consent dialog, the tracking
indicator and the live panel on the task page; the staff-only ``live`` list in
the map JSON; the privacy note on the About page; i18n coverage of every new
key in EN / RU / TJ."""

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, Users
from myapp.models import HelpRequest
from myapp.services import tracking
from server.i18n_prerender import translation_table

PASSWORD = "Track-Ui-2026!"
STATIC_JS = Path(settings.BASE_DIR) / "static" / "js"
TEMPLATES = Path(settings.BASE_DIR) / "templates"

CONSENT = 'id="trackConsentDialog"'
BANNER = 'id="trackBanner"'
WATCH = 'id="trackWatch"'
ROUTE_CARD = 'id="routeCard"'
SCRIPT = "js/task-route.js"


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


class TaskDetailTrackingUiTests(TestCase):
    def setUp(self):
        self.admin = _make("tu_admin", is_superuser=True, is_staff=True)
        self.curator = _make("tu_curator", is_curator=True)
        self.volunteer = _make("tu_vol", is_volunteer=True)
        self.other_vol = _make("tu_vol2", is_volunteer=True)
        self.client_user = _make("tu_client", is_client=True)
        self.other_client = _make("tu_client2", is_client=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="d",
            address="a", phone="+992901234567", region="dushanbe", status="active",
            latitude=38.5770, longitude=68.7795, accepted_at=timezone.now(),
        )

    def _html(self, user, task=None):
        self.client.force_login(user)
        response = self.client.get(reverse("task_detail", args=[(task or self.task).pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    # --- assigned volunteer ---
    def test_consent_dialog_for_assigned_volunteer_without_consent(self):
        html = self._html(self.volunteer)
        self.assertIn(CONSENT, html)
        self.assertIn(BANNER, html)
        self.assertIn('data-consent="0"', html)
        self.assertIn(reverse("tracking_consent"), html)
        self.assertIn(reverse("tracking_stop", args=[self.task.pk]), html)
        self.assertIn(SCRIPT, html)
        # the consent text explains who, when, auto-stop and retention
        for key in ("track.consent_who", "track.consent_when", "track.consent_keep", "track.consent_control",
                    "track.consent_agree", "track.consent_decline"):
            self.assertIn(f'data-i18n="{key}"', html)
        self.assertNotIn(WATCH, html)

    def test_no_dialog_once_consent_given_but_indicator_stays(self):
        tracking.give_consent(self.volunteer)
        html = self._html(self.volunteer)
        self.assertNotIn(CONSENT, html)
        self.assertIn(BANNER, html)
        self.assertIn('data-consent="1"', html)
        self.assertIn('data-stopped="0"', html)

    def test_stopped_flag_rendered_after_volunteer_stopped(self):
        tracking.give_consent(self.volunteer)
        self.task.tracking_stopped_at = timezone.now()
        self.task.save(update_fields=["tracking_stopped_at"])
        self.assertIn('data-stopped="1"', self._html(self.volunteer))

    def test_volunteer_gets_navigation_and_deep_links(self):
        html = self._html(self.volunteer)
        self.assertIn('id="routeSteps"', html)
        self.assertIn('id="navNext"', html)
        for app in ("google", "yandex_maps", "yandex_navi", "dgis"):
            self.assertIn(f'data-app="{app}"', html)
        self.assertIn("2gis.ru/routeSearch/rsType/car/to/", html)
        self.assertIn('rel="noopener"', html)

    # --- client / staff ---
    def test_client_and_staff_get_live_panel_without_tracker_controls(self):
        for user in (self.client_user, self.curator, self.admin):
            html = self._html(user)
            self.assertIn(ROUTE_CARD, html, user.username)
            self.assertIn(WATCH, html, user.username)
            self.assertIn(reverse("task_location", args=[self.task.pk]), html)
            self.assertNotIn(CONSENT, html, user.username)
            self.assertNotIn(BANNER, html, user.username)
            self.assertNotIn('id="routeSteps"', html, user.username)
            self.assertNotIn('data-app="google"', html, user.username)
            self.assertNotIn(reverse("tracking_consent"), html, user.username)

    def test_unrelated_users_get_nothing_location_related(self):
        # A volunteer who isn't assigned can't open an active task at all.
        self.client.force_login(self.other_vol)
        self.assertEqual(self.client.get(reverse("task_detail", args=[self.task.pk])).status_code, 302)
        self.client.force_login(self.other_client)
        self.assertEqual(self.client.get(reverse("task_detail", args=[self.task.pk])).status_code, 302)

    def test_no_location_panel_on_pending_or_finished_tasks(self):
        pending = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="d", address="a",
            phone="+992901234567", region="dushanbe", status="pending", latitude=38.57, longitude=68.78,
        )
        for user in (self.admin, self.client_user, self.volunteer):
            html = self._html(user, pending)
            for marker in (ROUTE_CARD, CONSENT, BANNER, WATCH, SCRIPT):
                self.assertNotIn(marker, html, f"{user.username} pending {marker}")
        for status in ("completed", "cancelled"):
            self.task.status = status
            self.task.save(update_fields=["status"])
            for user in (self.admin, self.client_user, self.volunteer):
                html = self._html(user)
                for marker in (ROUTE_CARD, CONSENT, BANNER, WATCH, SCRIPT):
                    self.assertNotIn(marker, html, f"{user.username} {status} {marker}")


class MapDataLiveTests(TestCase):
    def setUp(self):
        self.admin = _make("ml_admin", is_superuser=True, is_staff=True)
        self.curator = _make("ml_curator", is_curator=True)
        self.volunteer = _make("ml_vol", is_volunteer=True)
        self.client_user = _make("ml_client", is_client=True)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="d",
            address="a", phone="+992901234567", region="dushanbe", status="active",
            latitude=38.5770, longitude=68.7795, accepted_at=timezone.now(),
            volunteer_last_lat=38.5603, volunteer_last_lng=68.7870, volunteer_last_at=timezone.now(),
        )

    def _data(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("map_data")).json()

    def test_live_markers_only_in_staff_branch(self):
        for user in (self.admin, self.curator):
            data = self._data(user)
            self.assertEqual([m["task_id"] for m in data["live"]], [self.task.pk])
            marker = data["live"][0]
            self.assertEqual(marker["kind"], "volunteer_live")
            self.assertEqual(set(marker), {"kind", "task_id", "lat", "lng", "ts"})
        for user in (self.volunteer, self.client_user):
            self.assertNotIn("live", self._data(user), user.username)

    def test_live_list_skips_tasks_without_a_position(self):
        self.task.volunteer_last_lat = self.task.volunteer_last_lng = self.task.volunteer_last_at = None
        self.task.save()
        self.assertEqual(self._data(self.admin)["live"], [])

    def test_ops_map_page_has_live_list_for_staff_only(self):
        self.client.force_login(self.curator)
        self.assertContains(self.client.get(reverse("map")), 'id="opsLive"')
        self.client.force_login(self.volunteer)
        self.assertNotContains(self.client.get(reverse("map")), 'id="opsLive"')


class AboutPrivacyNoteTests(TestCase):
    def test_privacy_section_prerendered_in_russian(self):
        html = self.client.get(reverse("about")).content.decode()
        self.assertIn('id="privacy"', html)
        ru = translation_table("ru")
        for key in ("about.privacy_title", "about.privacy_when", "about.privacy_who",
                    "about.privacy_stop", "about.privacy_keep"):
            self.assertIn(f'data-i18n="{key}"', html)
            self.assertIn(ru[key], html)
        self.assertIn("24", ru["about.privacy_keep"])


class TrackingUiI18nTests(TestCase):
    """Every key the milestone-2 templates and scripts use exists in EN, RU and TJ."""

    FILES = ["myapp/task_detail.html", "myapp/map.html", "myapp/about.html"]
    SCRIPTS = ["task-route.js", "map.js"]

    def _keys(self):
        keys = set()
        for name in self.FILES:
            text = (TEMPLATES / name).read_text(encoding="utf-8")
            keys |= set(re.findall(r'data-i18n="([^"{]+)"', text))
            keys |= set(re.findall(r"""(?:GCMap\.t|I|tr)\(\s*'([a-z_]+\.[a-z0-9_.]+)'""", text))
        for name in self.SCRIPTS:
            text = (STATIC_JS / name).read_text(encoding="utf-8")
            keys |= set(re.findall(r"""['"`]((?:nav|route|track|map)\.[a-z0-9_]+)['"`]""", text))
        # dynamic nav.* keys built by GCMap.stepKey (`nav.turn_${mod}`, `nav.keep_${side}`)
        keys |= {f"nav.turn_{m}" for m in ("left", "right", "slight_left", "slight_right",
                                            "sharp_left", "sharp_right", "uturn")}
        keys |= {"nav.keep_left", "nav.keep_right"}
        return keys - {"map.marker"}   # a WebSocket event type, not a string

    def test_keys_exist_in_all_languages(self):
        keys = self._keys()
        self.assertGreater(len(keys), 80)
        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            missing = sorted(k for k in keys if k not in table)
            self.assertEqual(missing, [], f"{lang} is missing {missing}")

    def test_notification_keys_exist_in_all_languages(self):
        from myapp.services.notify_texts import all_i18n_keys

        for lang in ("en", "ru", "tj"):
            table = translation_table(lang)
            for key, text in all_i18n_keys(lang).items():
                self.assertEqual(table.get(key), text, f"{lang} {key}")


class TrackingConsentSettingTests(TestCase):
    """Edit profile lets a volunteer withdraw tracking consent (privacy control)."""

    def setUp(self):
        self.volunteer = Users.objects.create_user(
            username="consent_vol", email="consent_vol@example.com", password="Consent-2026!", is_volunteer=True,
        )
        self.client.force_login(self.volunteer)

    def test_revoke_form_shown_only_with_consent_and_works(self):
        url = reverse("edit_profile")
        self.assertNotContains(self.client.get(url), 'name="action" value="revoke"')
        Profile.objects.filter(user=self.volunteer).update(tracking_consent_at=timezone.now())
        self.assertContains(self.client.get(url), 'name="action" value="revoke"')
        response = self.client.post(reverse("tracking_consent"), {"action": "revoke", "next": url})
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.assertIsNone(Profile.objects.get(user=self.volunteer).tracking_consent_at)

    def test_clients_do_not_see_the_setting(self):
        client_user = Users.objects.create_user(
            username="consent_cli", email="consent_cli@example.com", password="Consent-2026!", is_client=True,
        )
        self.client.force_login(client_user)
        self.assertNotContains(self.client.get(reverse("edit_profile")), "trackingConsentSetting")
