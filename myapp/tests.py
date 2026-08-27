from unittest import mock

import requests
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Users
from .models import HelpRequest, VolunteerApplication
from .services import analytics
from .services.geo import get_route, haversine_km, is_valid_coordinate
from .services.matching import recommend_volunteers


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class HelpRequestFlowTests(TestCase):
    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="client_one", email="client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.volunteer = Users.objects.create_user(
            username="vol_one", email="vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )

    def test_client_creates_request(self):
        self.client.login(username="client_one", password="pass12345")
        response = self.client.post(
            reverse("create_request"),
            {"help_type": "grocery", "description": "Need groceries", "address": "Some street", "phone": "+992"},
        )
        self.assertEqual(response.status_code, 302)
        request = HelpRequest.objects.get(client=self.client_user)
        self.assertEqual(request.status, "pending")
        self.assertEqual(request.region, "dushanbe")

    def test_volunteer_accept_and_complete(self):
        request = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="Buy food", address="Addr", phone="+992", region="dushanbe"
        )
        self.client.login(username="vol_one", password="pass12345")

        self.client.post(reverse("accept_task", args=[request.pk]))
        request.refresh_from_db()
        self.assertEqual(request.status, "active")
        self.assertEqual(request.volunteer, self.volunteer)

        self.client.post(reverse("complete_task", args=[request.pk]))
        request.refresh_from_db()
        self.assertEqual(request.status, "completed")
        self.volunteer.profile.refresh_from_db()
        self.assertEqual(self.volunteer.profile.rating, 3)

    def test_accept_and_complete_reject_get(self):
        request = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="Buy food", address="Addr", phone="+992", region="dushanbe"
        )
        self.client.login(username="vol_one", password="pass12345")
        self.assertEqual(self.client.get(reverse("accept_task", args=[request.pk])).status_code, 405)
        request.refresh_from_db()
        self.assertEqual(request.status, "pending")

    def test_public_pages_load(self):
        for name in ["about", "rating"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_ai_chat_returns_fallback_without_api_key(self):
        self.client.login(username="vol_one", password="pass12345")
        # Clear both settings and env vars so the view takes the offline fallback
        # path (no external network call during tests).
        with override_settings(GROQ_API_KEY="", GEMINI_API_KEY=""), mock.patch.dict(
            "os.environ", {"GROQ_API_KEY": "", "GEMINI_API_KEY": ""}
        ):
            response = self.client.post(
                reverse("ai_chat"), data='{"message": "привет"}', content_type="application/json"
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("reply", response.json())


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class VolunteerApplicationTests(TestCase):
    def setUp(self):
        self.admin = Users.objects.create_superuser(username="admin_one", email="admin@example.com", password="pass12345")
        self.client_user = Users.objects.create_user(
            username="client_two", email="client2@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

    def _make_pending(self, username, email):
        user = Users.objects.create_user(username=username, email=email, password="pass12345", region="dushanbe")
        application = VolunteerApplication.objects.create(user=user, region="dushanbe")
        return user, application

    def test_register_as_client_sets_is_client(self):
        response = self.client.post(reverse("register"), {
            "username": "newclient", "email": "newclient@example.com", "role": "client",
            "region": "dushanbe", "password": "Passw0rd!23", "confirm_password": "Passw0rd!23",
        })
        self.assertEqual(response.status_code, 302)
        user = Users.objects.get(username="newclient")
        self.assertTrue(user.is_client)
        self.assertFalse(user.is_volunteer)

    def test_register_as_volunteer_does_not_grant_role(self):
        response = self.client.post(reverse("register"), {
            "username": "newvol", "email": "newvol@example.com", "role": "volunteer",
            "region": "dushanbe", "password": "Passw0rd!23", "confirm_password": "Passw0rd!23",
        })
        self.assertEqual(response.status_code, 302)
        user = Users.objects.get(username="newvol")
        self.assertFalse(user.is_volunteer)
        self.assertFalse(user.is_client)

    def test_register_as_volunteer_creates_pending_application(self):
        self.client.post(reverse("register"), {
            "username": "newvol2", "email": "newvol2@example.com", "role": "volunteer",
            "region": "dushanbe", "password": "Passw0rd!23", "confirm_password": "Passw0rd!23",
        })
        user = Users.objects.get(username="newvol2")
        application = VolunteerApplication.objects.get(user=user)
        self.assertEqual(application.status, VolunteerApplication.STATUS_PENDING)

    def test_pending_applicant_cannot_access_task_list(self):
        user, _ = self._make_pending("pendingvol", "p@example.com")
        self.client.login(username="pendingvol", password="pass12345")
        response = self.client.get(reverse("task_list"))
        self.assertRedirects(response, reverse("profile"))

    def test_admin_approve_grants_volunteer(self):
        user, application = self._make_pending("tobeapproved", "a2@example.com")
        application.approve(self.admin)
        user.refresh_from_db()
        application.refresh_from_db()
        self.assertTrue(user.is_volunteer)
        self.assertEqual(application.status, VolunteerApplication.STATUS_APPROVED)
        self.assertEqual(application.reviewed_by, self.admin)
        self.assertIsNotNone(application.reviewed_at)

    def test_approved_volunteer_can_access_task_list(self):
        user, application = self._make_pending("approvedvol", "a3@example.com")
        application.approve(self.admin)
        self.client.login(username="approvedvol", password="pass12345")
        response = self.client.get(reverse("task_list"))
        self.assertEqual(response.status_code, 200)

    def test_admin_reject_denies_volunteer(self):
        user, application = self._make_pending("tobrejected", "a4@example.com")
        application.reject(self.admin)
        user.refresh_from_db()
        application.refresh_from_db()
        self.assertFalse(user.is_volunteer)
        self.assertEqual(application.status, VolunteerApplication.STATUS_REJECTED)

    def test_rejected_user_cannot_accept_task(self):
        user, application = self._make_pending("rejectedvol", "a5@example.com")
        application.reject(self.admin)
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="y", phone="z", region="dushanbe"
        )
        self.client.login(username="rejectedvol", password="pass12345")
        response = self.client.get(reverse("accept_task", args=[task.pk]))
        self.assertRedirects(response, reverse("profile"))
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")

    def test_pending_user_cannot_accept_task(self):
        user, _ = self._make_pending("pendingvol2", "a6@example.com")
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="y", phone="z", region="dushanbe"
        )
        self.client.login(username="pendingvol2", password="pass12345")
        response = self.client.get(reverse("accept_task", args=[task.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_one_active_task_rule_still_enforced(self):
        user, application = self._make_pending("busyvol", "a7@example.com")
        application.approve(self.admin)
        task1 = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="y", phone="z", region="dushanbe"
        )
        task2 = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x2", address="y2", phone="z2", region="dushanbe"
        )
        self.client.login(username="busyvol", password="pass12345")
        self.client.post(reverse("accept_task", args=[task1.pk]))
        self.client.post(reverse("accept_task", args=[task2.pk]))
        task2.refresh_from_db()
        self.assertEqual(task2.status, "pending")
        self.assertIsNone(task2.volunteer)

    def test_duplicate_application_prevented(self):
        user, _ = self._make_pending("dupvol", "a8@example.com")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                VolunteerApplication.objects.create(user=user, region="dushanbe")

    def test_rejected_user_can_reapply(self):
        user, application = self._make_pending("reapplyvol", "a9@example.com")
        application.reject(self.admin)
        self.client.login(username="reapplyvol", password="pass12345")
        response = self.client.post(reverse("volunteer_application"))
        self.assertRedirects(response, reverse("volunteer_application"))
        application.refresh_from_db()
        self.assertEqual(application.status, VolunteerApplication.STATUS_PENDING)
        self.assertIsNone(application.reviewed_by)

    def test_curator_with_change_permission_cannot_approve(self):
        curator = Users.objects.create_user(
            username="curator_one", email="cur@example.com", password="pass12345",
            is_curator=True, is_staff=True, region="dushanbe",
        )
        content_type = ContentType.objects.get_for_model(VolunteerApplication)
        perm = Permission.objects.get(content_type=content_type, codename="change_volunteerapplication")
        curator.user_permissions.add(perm)

        user, application = self._make_pending("curatortest", "a10@example.com")
        self.client.login(username="curator_one", password="pass12345")
        self.client.post(
            reverse("admin:myapp_volunteerapplication_changelist"),
            {"action": "approve_applications", "_selected_action": [application.pk]},
        )
        application.refresh_from_db()
        user.refresh_from_db()
        self.assertEqual(application.status, VolunteerApplication.STATUS_PENDING)
        self.assertFalse(user.is_volunteer)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class VolunteerApplicationsAdminSectionTests(TestCase):
    """Tests for the in-app admin section at /myapp/volunteer-applications/."""

    def setUp(self):
        self.admin = Users.objects.create_user(
            username="section_admin", email="section_admin@example.com", password="pass12345", is_superuser=True, is_staff=True
        )
        self.curator = Users.objects.create_user(
            username="section_curator", email="section_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="section_volunteer", email="section_volunteer@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        self.client_user = Users.objects.create_user(
            username="section_client", email="section_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        self.applicant = Users.objects.create_user(
            username="section_applicant", email="section_applicant@example.com", password="pass12345", region="dushanbe"
        )
        self.application = VolunteerApplication.objects.create(user=self.applicant, region="dushanbe", reason="I want to help.")

    # 1-4: access control on the list page
    def test_admin_can_access_applications_page(self):
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.get(reverse("volunteer_applications"))
        self.assertEqual(response.status_code, 200)

    def test_curator_cannot_access_applications_page(self):
        self.client.login(username="section_curator", password="pass12345")
        response = self.client.get(reverse("volunteer_applications"))
        self.assertRedirects(response, reverse("profile"))

    def test_volunteer_cannot_access_applications_page(self):
        self.client.login(username="section_volunteer", password="pass12345")
        response = self.client.get(reverse("volunteer_applications"))
        self.assertRedirects(response, reverse("profile"))

    def test_client_cannot_access_applications_page(self):
        self.client.login(username="section_client", password="pass12345")
        response = self.client.get(reverse("volunteer_applications"))
        self.assertRedirects(response, reverse("profile"))

    # 5-8: approve / reject behaviour
    def test_admin_can_approve_pending_application(self):
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.post(reverse("volunteer_application_approve", args=[self.application.pk]))
        self.assertRedirects(response, reverse("volunteer_applications"))
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, VolunteerApplication.STATUS_APPROVED)

    def test_approve_sets_is_volunteer_true(self):
        self.client.login(username="section_admin", password="pass12345")
        self.client.post(reverse("volunteer_application_approve", args=[self.application.pk]))
        self.applicant.refresh_from_db()
        self.assertTrue(self.applicant.is_volunteer)

    def test_admin_can_reject_application(self):
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.post(reverse("volunteer_application_reject", args=[self.application.pk]))
        self.assertRedirects(response, reverse("volunteer_applications"))
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, VolunteerApplication.STATUS_REJECTED)

    def test_reject_keeps_is_volunteer_false(self):
        self.client.login(username="section_admin", password="pass12345")
        self.client.post(reverse("volunteer_application_reject", args=[self.application.pk]))
        self.applicant.refresh_from_db()
        self.assertFalse(self.applicant.is_volunteer)

    # 9: GET cannot approve/reject
    def test_get_cannot_approve_or_reject(self):
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.get(reverse("volunteer_application_approve", args=[self.application.pk]))
        self.assertEqual(response.status_code, 405)
        response = self.client.get(reverse("volunteer_application_reject", args=[self.application.pk]))
        self.assertEqual(response.status_code, 405)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, VolunteerApplication.STATUS_PENDING)

    # 10: POST without CSRF token is rejected
    def test_post_without_csrf_is_rejected(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="section_admin", password="pass12345")
        response = csrf_client.post(reverse("volunteer_application_approve", args=[self.application.pk]))
        self.assertEqual(response.status_code, 403)
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, VolunteerApplication.STATUS_PENDING)

    # 11-13: filters
    def test_pending_application_appears_in_pending_list(self):
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.get(reverse("volunteer_applications"), {"status": "pending"})
        self.assertContains(response, "section_applicant")

    def test_approved_application_appears_in_approved_list(self):
        self.application.approve(self.admin)
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.get(reverse("volunteer_applications"), {"status": "approved"})
        self.assertContains(response, "section_applicant")
        response = self.client.get(reverse("volunteer_applications"), {"status": "pending"})
        self.assertNotContains(response, "section_applicant")

    def test_rejected_application_appears_in_rejected_list(self):
        self.application.reject(self.admin)
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.get(reverse("volunteer_applications"), {"status": "rejected"})
        self.assertContains(response, "section_applicant")
        response = self.client.get(reverse("volunteer_applications"), {"status": "pending"})
        self.assertNotContains(response, "section_applicant")

    # Extra: not approving twice / not double-notifying
    def test_approving_already_approved_application_is_a_no_op(self):
        self.application.approve(self.admin)
        reviewed_at = self.application.reviewed_at
        self.client.login(username="section_admin", password="pass12345")
        self.client.post(reverse("volunteer_application_approve", args=[self.application.pk]))
        self.application.refresh_from_db()
        self.assertEqual(self.application.reviewed_at, reviewed_at)

    def test_curator_cannot_approve_via_section_urls(self):
        self.client.login(username="section_curator", password="pass12345")
        response = self.client.post(reverse("volunteer_application_approve", args=[self.application.pk]))
        self.assertRedirects(response, reverse("profile"))
        self.application.refresh_from_db()
        self.assertEqual(self.application.status, VolunteerApplication.STATUS_PENDING)

    def test_detail_page_access_control(self):
        self.client.login(username="section_admin", password="pass12345")
        response = self.client.get(reverse("volunteer_application_detail", args=[self.application.pk]))
        self.assertEqual(response.status_code, 200)

        self.client.logout()
        self.client.login(username="section_curator", password="pass12345")
        response = self.client.get(reverse("volunteer_application_detail", args=[self.application.pk]))
        self.assertRedirects(response, reverse("profile"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class MapAndLocationTests(TestCase):
    def setUp(self):
        self.admin = Users.objects.create_superuser(username="map_admin", email="map_admin@example.com", password="pass12345")
        self.volunteer = Users.objects.create_user(
            username="map_vol", email="map_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="map_client", email="map_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

    def test_map_page_requires_login(self):
        response = self.client.get(reverse("map"))
        self.assertEqual(response.status_code, 302)

    def test_map_page_loads_for_authenticated_user(self):
        self.client.login(username="map_vol", password="pass12345")
        response = self.client.get(reverse("map"))
        self.assertEqual(response.status_code, 200)

    def test_map_data_client_sees_only_own_located_requests(self):
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="mine", address="a", phone="p",
            region="dushanbe", latitude=38.5, longitude=68.7,
        )
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="no location", address="a", phone="p", region="dushanbe",
        )
        other_client = Users.objects.create_user(
            username="other_client", email="other_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        HelpRequest.objects.create(
            client=other_client, help_type="grocery", description="not mine", address="a", phone="p",
            region="dushanbe", latitude=38.6, longitude=68.8,
        )
        self.client.login(username="map_client", password="pass12345")
        response = self.client.get(reverse("map_data"))
        self.assertEqual(response.status_code, 200)
        points = response.json()["points"]
        self.assertEqual(len(points), 1)

    def test_map_data_admin_sees_volunteer_markers(self):
        self.volunteer.profile.set_location(38.55, 68.78)
        self.client.login(username="map_admin", password="pass12345")
        response = self.client.get(reverse("map_data"))
        points = response.json()["points"]
        self.assertTrue(any(p["title"] == "map_vol" for p in points))

    def test_update_location_requires_post(self):
        self.client.login(username="map_vol", password="pass12345")
        response = self.client.get(reverse("update_location"))
        self.assertEqual(response.status_code, 405)

    def test_update_location_sets_profile_fields(self):
        self.client.login(username="map_vol", password="pass12345")
        response = self.client.post(
            reverse("update_location"), data='{"latitude": 38.55, "longitude": 68.78}', content_type="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.volunteer.profile.refresh_from_db()
        self.assertAlmostEqual(float(self.volunteer.profile.latitude), 38.55)
        self.assertIsNotNone(self.volunteer.profile.location_updated_at)

    def test_update_location_rejects_invalid_payload(self):
        self.client.login(username="map_vol", password="pass12345")
        response = self.client.post(reverse("update_location"), data="{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)

    def test_task_form_without_location_fields_still_renders(self):
        # create_event/broadcast reuse task_form.html, whose map picker block is
        # guarded by `{% if form.latitude %}` — EventForm/BroadcastForm have no
        # such field, so this must render cleanly without it.
        self.client.login(username="map_admin", password="pass12345")
        self.assertEqual(self.client.get(reverse("create_event")).status_code, 200)
        self.assertEqual(self.client.get(reverse("broadcast")).status_code, 200)


class GeoServiceTests(TestCase):
    """Pure-function tests for myapp.services.geo — no DB, no network."""

    def test_haversine_zero_distance(self):
        self.assertAlmostEqual(haversine_km(38.55, 68.78, 38.55, 68.78), 0.0, places=3)

    def test_haversine_known_distance(self):
        # Two points roughly 1km apart in Dushanbe — sanity range, not exact.
        km = haversine_km(38.5598, 68.7870, 38.5688, 68.7870)
        self.assertGreater(km, 0.5)
        self.assertLess(km, 1.5)

    def test_is_valid_coordinate_accepts_valid_values(self):
        self.assertTrue(is_valid_coordinate(38.55, 68.78))
        self.assertTrue(is_valid_coordinate("38.55", "68.78"))
        self.assertTrue(is_valid_coordinate(-90, -180))
        self.assertTrue(is_valid_coordinate(90, 180))

    def test_is_valid_coordinate_rejects_out_of_range_and_garbage(self):
        self.assertFalse(is_valid_coordinate(91, 68.78))
        self.assertFalse(is_valid_coordinate(38.55, 181))
        self.assertFalse(is_valid_coordinate(-91, 0))
        self.assertFalse(is_valid_coordinate("abc", 68.78))
        self.assertFalse(is_valid_coordinate(None, None))

    def test_get_route_success(self):
        fake_response = mock.Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {
            "code": "Ok",
            "routes": [{
                "distance": 5000,
                "duration": 600,
                "geometry": {"coordinates": [[68.78, 38.55], [68.79, 38.56]]},
            }],
        }
        with mock.patch("myapp.services.geo.requests.get", return_value=fake_response):
            result = get_route((38.55, 68.78), (38.56, 68.79))
        self.assertTrue(result["success"])
        self.assertEqual(result["distance_km"], 5.0)
        self.assertEqual(result["duration_min"], 10.0)
        # GeoJSON [lng, lat] must be flipped to Leaflet's [lat, lng].
        self.assertEqual(result["geometry"][0], [38.55, 68.78])

    def test_get_route_handles_request_exception_without_raising(self):
        with mock.patch("myapp.services.geo.requests.get", side_effect=requests.RequestException("boom")):
            result = get_route((38.55, 68.78), (38.56, 68.79))
        self.assertFalse(result["success"])
        self.assertIsNone(result["duration_min"])
        self.assertEqual(result["geometry"], [])
        self.assertGreater(result["distance_km"], 0)

    def test_get_route_handles_timeout_without_raising(self):
        with mock.patch("myapp.services.geo.requests.get", side_effect=requests.Timeout("slow")):
            result = get_route((38.55, 68.78), (38.56, 68.79))
        self.assertFalse(result["success"])

    def test_get_route_handles_no_route_found(self):
        fake_response = mock.Mock()
        fake_response.raise_for_status.return_value = None
        fake_response.json.return_value = {"code": "NoRoute", "routes": []}
        with mock.patch("myapp.services.geo.requests.get", return_value=fake_response):
            result = get_route((38.55, 68.78), (38.56, 68.79))
        self.assertFalse(result["success"])

    def test_get_route_without_osrm_base_url_falls_back(self):
        with override_settings(OSRM_BASE_URL=""):
            result = get_route((38.55, 68.78), (38.56, 68.79))
        self.assertFalse(result["success"])
        self.assertGreater(result["distance_km"], 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TaskRouteViewTests(TestCase):
    def setUp(self):
        self.admin = Users.objects.create_superuser(username="route_admin", email="route_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="route_curator", email="route_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="route_vol", email="route_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.other_volunteer = Users.objects.create_user(
            username="route_vol2", email="route_vol2@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="route_client", email="route_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.other_client = Users.objects.create_user(
            username="route_client2", email="route_client2@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x",
            address="addr", phone="p", region="dushanbe", status="active",
            latitude=38.56, longitude=68.79,
        )
        self.volunteer.profile.set_location(38.55, 68.78)

    def _mock_route(self):
        return mock.patch("myapp.views.get_route", return_value={
            "success": True, "distance_km": 1.5, "duration_min": 4.0,
            "geometry": [[38.55, 68.78], [38.56, 68.79]],
        })

    # 1: authentication
    def test_route_requires_authentication(self):
        response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 302)

    # 2: assigned volunteer
    def test_assigned_volunteer_can_route(self):
        self.client.login(username="route_vol", password="pass12345")
        with self._mock_route():
            response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["distance_km"], 1.5)
        self.assertEqual(data["origin"]["source"], "saved")

    # 3: unrelated volunteer forbidden
    def test_unrelated_volunteer_cannot_route(self):
        self.client.login(username="route_vol2", password="pass12345")
        response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 403)

    # task owner client allowed, other client forbidden
    def test_task_owner_client_can_route(self):
        self.client.login(username="route_client", password="pass12345")
        with self._mock_route():
            response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)

    # 4: unrelated client forbidden
    def test_other_client_cannot_route(self):
        self.client.login(username="route_client2", password="pass12345")
        response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 403)

    # 5: admin/curator access
    def test_admin_and_curator_can_route(self):
        with self._mock_route():
            self.client.login(username="route_admin", password="pass12345")
            self.assertEqual(self.client.get(reverse("task_route", args=[self.task.pk])).status_code, 200)
            self.client.logout()
            self.client.login(username="route_curator", password="pass12345")
            self.assertEqual(self.client.get(reverse("task_route", args=[self.task.pk])).status_code, 200)

    # 6: invalid coordinates
    def test_invalid_live_coordinates_rejected(self):
        self.client.login(username="route_vol", password="pass12345")
        response = self.client.get(reverse("task_route", args=[self.task.pk]), {"lat": "999", "lng": "68.78"})
        self.assertEqual(response.status_code, 400)

    def test_live_coordinates_used_as_origin_for_assigned_volunteer(self):
        self.client.login(username="route_vol", password="pass12345")
        with self._mock_route() as mocked:
            response = self.client.get(reverse("task_route", args=[self.task.pk]), {"lat": "38.50", "lng": "68.70"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["origin"]["source"], "live")
        self.assertEqual(data["origin"]["lat"], 38.5)
        mocked.assert_called_once_with((38.5, 68.7), (38.56, 68.79))

    def test_client_cannot_override_origin_with_live_coordinates(self):
        # Only the assigned volunteer's browser location is trusted as origin;
        # a client passing lat/lng must be ignored, not used as their own origin.
        self.client.login(username="route_client", password="pass12345")
        with self._mock_route() as mocked:
            response = self.client.get(reverse("task_route", args=[self.task.pk]), {"lat": "1.0", "lng": "2.0"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["origin"]["source"], "saved")
        mocked.assert_called_once_with((38.55, 68.78), (38.56, 68.79))

    # 7: missing task/location handled gracefully
    def test_task_without_location_is_graceful(self):
        self.task.latitude = None
        self.task.longitude = None
        self.task.save(update_fields=["latitude", "longitude"])
        self.client.login(username="route_vol", password="pass12345")
        response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "no_task_location")

    def test_unassigned_task_is_graceful_for_client(self):
        self.task.volunteer = None
        self.task.status = "pending"
        self.task.save(update_fields=["volunteer", "status"])
        self.client.login(username="route_client", password="pass12345")
        response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "no_volunteer_assigned")

    def test_volunteer_without_saved_location_is_graceful(self):
        self.volunteer.profile.latitude = None
        self.volunteer.profile.longitude = None
        self.volunteer.profile.save()
        # Viewed by the client, so no live-GPS override is possible for them.
        self.client.login(username="route_client", password="pass12345")
        response = self.client.get(reverse("task_route", args=[self.task.pk]))
        data = response.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["reason"], "no_volunteer_location")

    # 8 & 9: OSRM failure and success paths through the view
    def test_osrm_failure_returns_graceful_fallback(self):
        self.client.login(username="route_vol", password="pass12345")
        with mock.patch("myapp.views.get_route", return_value={
            "success": False, "distance_km": 1.62, "duration_min": None, "geometry": [],
        }):
            response = self.client.get(reverse("task_route", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["distance_km"], 1.62)
        self.assertIsNone(data["duration_min"])

    def test_osrm_success_returns_distance_and_duration(self):
        self.client.login(username="route_vol", password="pass12345")
        with self._mock_route():
            response = self.client.get(reverse("task_route", args=[self.task.pk]))
        data = response.json()
        self.assertEqual(data["distance_km"], 1.5)
        self.assertEqual(data["duration_min"], 4.0)
        self.assertEqual(len(data["geometry"]), 2)

    # Task detail page: route card only for the active, assigned, permitted case
    def test_task_detail_shows_route_card_only_for_active_assigned_task(self):
        self.client.login(username="route_vol", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertContains(response, 'id="routeCard"')

        self.task.status = "completed"
        self.task.save(update_fields=["status"])
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertNotContains(response, 'id="routeCard"')


TASK_LAT, TASK_LNG = 38.5598, 68.7870
KM_PER_DEGREE_LAT = 111.195  # exact for a pure-latitude offset (great-circle arc along a meridian)


class MatchingAlgorithmTests(TestCase):
    """Pure algorithm tests for myapp.services.matching — no views, no network."""

    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="match_client", email="match_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending", latitude=TASK_LAT, longitude=TASK_LNG,
        )
        self._counter = 0

    def _make_volunteer(self, distance_km=5, region="dushanbe", availability="available",
                         active_tasks=0, location_age=None, has_location=True):
        self._counter += 1
        volunteer = Users.objects.create_user(
            username=f"vol_{self._counter}", email=f"vol_{self._counter}@example.com",
            password="pass12345", is_volunteer=True, region=region,
        )
        profile = volunteer.profile
        profile.availability_status = availability
        if has_location:
            profile.latitude = TASK_LAT + distance_km / KM_PER_DEGREE_LAT
            profile.longitude = TASK_LNG
            profile.location_updated_at = timezone.now() - (location_age or timezone.timedelta(minutes=1))
        profile.save()
        for _ in range(active_tasks):
            HelpRequest.objects.create(
                client=self.client_user, volunteer=volunteer, help_type="grocery", description="x",
                address="a", phone="p", region=region, status="active",
            )
        return volunteer

    # 7 & 14: offline excluded / no suitable volunteers -> empty
    def test_offline_volunteers_excluded(self):
        self._make_volunteer(distance_km=1, availability="offline")
        self.assertEqual(recommend_volunteers(self.task), [])

    def test_no_volunteers_at_all_returns_empty(self):
        self.assertEqual(recommend_volunteers(self.task), [])

    # 6: no location -> handled (excluded, not crashed)
    def test_volunteers_without_location_are_excluded_not_crashed(self):
        self._make_volunteer(has_location=False)
        self.assertEqual(recommend_volunteers(self.task), [])

    # 8: busy ranks below available, all else equal
    def test_busy_ranks_below_available_all_else_equal(self):
        available = self._make_volunteer(distance_km=5, availability="available")
        busy = self._make_volunteer(distance_km=5, availability="busy")
        ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertLess(ids.index(available.id), ids.index(busy.id))

    # 9: fewer active tasks ranks higher when distance is comparable
    def test_fewer_active_tasks_ranks_higher_when_distance_comparable(self):
        low_workload = self._make_volunteer(distance_km=5, active_tasks=0)
        high_workload = self._make_volunteer(distance_km=5, active_tasks=2)
        ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertLess(ids.index(low_workload.id), ids.index(high_workload.id))

    # 10: same-region volunteers get appropriate preference (not exclusive)
    def test_same_region_preferred_over_other_region(self):
        same_region = self._make_volunteer(distance_km=5, region="dushanbe")
        other_region = self._make_volunteer(distance_km=5, region="khatlon")
        results = recommend_volunteers(self.task)
        ids = [item["volunteer"].id for item in results]
        self.assertIn(other_region.id, ids)  # not excluded, just ranked behind
        self.assertLess(ids.index(same_region.id), ids.index(other_region.id))

    # 11: closer volunteers rank higher
    def test_closer_volunteer_ranks_higher(self):
        close = self._make_volunteer(distance_km=2)
        far = self._make_volunteer(distance_km=25)
        ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertLess(ids.index(close.id), ids.index(far.id))

    # 12: urgent tasks prioritize proximity over a merely-available-but-far volunteer
    def test_urgent_task_prioritizes_closest_over_available_but_far(self):
        far_available = self._make_volunteer(distance_km=20, availability="available")
        close_busy = self._make_volunteer(distance_km=1, availability="busy")

        normal_ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertLess(normal_ids.index(far_available.id), normal_ids.index(close_busy.id))

        self.task.is_urgent = True
        self.task.save(update_fields=["is_urgent"])
        urgent_ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertLess(urgent_ids.index(close_busy.id), urgent_ids.index(far_available.id))

    # 13: old/stale locations handled correctly (flagged + deprioritized, not treated as live)
    def test_stale_location_flagged_and_deprioritized(self):
        stale = self._make_volunteer(distance_km=5, location_age=timezone.timedelta(days=30))
        fresh = self._make_volunteer(distance_km=5, location_age=timezone.timedelta(minutes=1))
        results = recommend_volunteers(self.task)
        by_id = {item["volunteer"].id: item for item in results}
        self.assertEqual(by_id[stale.id]["location_freshness"], "stale")
        self.assertEqual(by_id[fresh.id]["location_freshness"], "fresh")
        ids = [item["volunteer"].id for item in results]
        self.assertLess(ids.index(fresh.id), ids.index(stale.id))

    def test_task_without_location_returns_empty(self):
        self.task.latitude = None
        self.task.longitude = None
        self.task.save(update_fields=["latitude", "longitude"])
        self._make_volunteer(distance_km=1)
        self.assertEqual(recommend_volunteers(self.task), [])

    def test_already_assigned_volunteer_excluded_from_recommendations(self):
        assigned = self._make_volunteer(distance_km=1)
        self.task.volunteer = assigned
        self.task.status = "active"
        self.task.save(update_fields=["volunteer", "status"])
        ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertNotIn(assigned.id, ids)

    def test_limit_is_respected(self):
        for i in range(8):
            self._make_volunteer(distance_km=i + 1)
        self.assertEqual(len(recommend_volunteers(self.task, limit=5)), 5)

    def test_recommendation_never_assigns_the_task(self):
        self._make_volunteer(distance_km=1)
        recommend_volunteers(self.task)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
        self.assertIsNone(self.task.volunteer)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TaskRecommendationsViewTests(TestCase):
    def setUp(self):
        self.admin = Users.objects.create_superuser(username="rec_admin", email="rec_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="rec_curator", email="rec_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="rec_vol", email="rec_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="rec_client", email="rec_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending", latitude=TASK_LAT, longitude=TASK_LNG,
        )
        self.nearby_volunteer = Users.objects.create_user(
            username="rec_nearby", email="rec_nearby@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.nearby_volunteer.profile.set_location(38.561, 68.787)

    # 1 & 2: admin/curator access
    def test_admin_can_get_recommendations(self):
        self.client.login(username="rec_admin", password="pass12345")
        response = self.client.get(reverse("task_recommendations", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["task_has_location"])
        self.assertEqual(len(data["recommendations"]), 1)
        self.assertEqual(data["recommendations"][0]["username"], "rec_nearby")

    def test_curator_can_get_recommendations(self):
        self.client.login(username="rec_curator", password="pass12345")
        response = self.client.get(reverse("task_recommendations", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)

    # 3, 4, 5: client / volunteer / anonymous cannot query the volunteer directory
    def test_client_cannot_get_recommendations(self):
        self.client.login(username="rec_client", password="pass12345")
        response = self.client.get(reverse("task_recommendations", args=[self.task.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_volunteer_cannot_get_recommendations(self):
        self.client.login(username="rec_vol", password="pass12345")
        response = self.client.get(reverse("task_recommendations", args=[self.task.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_anonymous_cannot_get_recommendations(self):
        response = self.client.get(reverse("task_recommendations", args=[self.task.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    # Notify action: recommends/notifies, never assigns
    def test_admin_can_notify_recommended_volunteer(self):
        self.client.login(username="rec_admin", password="pass12345")
        mail.outbox.clear()  # setUp's create_user() calls send welcome emails; isolate this action's send.
        response = self.client.post(reverse("task_notify_volunteer", args=[self.task.pk, self.nearby_volunteer.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(str(self.task.id), mail.outbox[0].body)

    def test_notify_does_not_assign_task(self):
        self.client.login(username="rec_admin", password="pass12345")
        self.client.post(reverse("task_notify_volunteer", args=[self.task.pk, self.nearby_volunteer.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")
        self.assertIsNone(self.task.volunteer)

    def test_notify_requires_post(self):
        self.client.login(username="rec_admin", password="pass12345")
        response = self.client.get(reverse("task_notify_volunteer", args=[self.task.pk, self.nearby_volunteer.pk]))
        self.assertEqual(response.status_code, 405)

    def test_client_cannot_notify(self):
        self.client.login(username="rec_client", password="pass12345")
        response = self.client.post(reverse("task_notify_volunteer", args=[self.task.pk, self.nearby_volunteer.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_notify_on_non_pending_task_is_graceful(self):
        self.task.status = "cancelled"
        self.task.save(update_fields=["status"])
        self.client.login(username="rec_admin", password="pass12345")
        response = self.client.post(reverse("task_notify_volunteer", args=[self.task.pk, self.nearby_volunteer.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["success"])

    # 15: task lifecycle still intact (accept/complete untouched by this phase)
    def test_task_lifecycle_still_works_after_matching_feature(self):
        self.client.login(username="rec_vol", password="pass12345")
        self.client.post(reverse("accept_task", args=[self.task.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "active")
        self.assertEqual(self.task.volunteer, self.volunteer)

    def test_recommend_section_only_visible_to_admin_curator_on_detail_page(self):
        self.client.login(username="rec_admin", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertContains(response, 'id="recommendCard"')

        self.client.logout()
        self.client.login(username="rec_vol", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertNotContains(response, 'id="recommendCard"')


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AnalyticsServiceTests(TestCase):
    """Pure tests for myapp.services.analytics — no views involved."""

    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="an_client", email="an_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )

    def _make_volunteer(self, username, availability="available"):
        volunteer = Users.objects.create_user(
            username=username, email=f"{username}@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        volunteer.profile.availability_status = availability
        volunteer.profile.save()
        return volunteer

    def test_volunteer_availability_breakdown(self):
        self._make_volunteer("an_v1", "available")
        self._make_volunteer("an_v2", "available")
        self._make_volunteer("an_v3", "busy")
        self._make_volunteer("an_v4", "offline")
        breakdown = analytics.volunteer_availability_breakdown()
        self.assertEqual(breakdown["available"], 2)
        self.assertEqual(breakdown["busy"], 1)
        self.assertEqual(breakdown["offline"], 1)

    def test_task_status_breakdown_counts(self):
        volunteer = self._make_volunteer("an_v5")
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending",
        )
        HelpRequest.objects.create(
            client=self.client_user, volunteer=volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active", is_urgent=True,
        )
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="completed",
        )
        counts = analytics.task_status_breakdown()
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["active"], 1)
        self.assertEqual(counts["completed"], 1)
        self.assertEqual(counts["urgent"], 1)

    def test_overdue_counted_correctly(self):
        volunteer = self._make_volunteer("an_v6")
        HelpRequest.objects.create(
            client=self.client_user, volunteer=volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active", accepted_at=timezone.now() - timezone.timedelta(hours=4),
        )
        HelpRequest.objects.create(
            client=self.client_user, volunteer=volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active", accepted_at=timezone.now() - timezone.timedelta(minutes=30),
        )
        counts = analytics.task_status_breakdown()
        self.assertEqual(counts["overdue"], 1)

    def test_dashboard_stats_has_every_expected_key(self):
        stats = analytics.dashboard_stats()
        for key in [
            "volunteers_total", "volunteers_available", "volunteers_busy", "volunteers_offline",
            "clients_total", "tasks_pending", "tasks_active", "tasks_completed", "tasks_overdue",
            "tasks_urgent", "tasks_completed_today", "pending_applications",
        ]:
            self.assertIn(key, stats)

    def test_tasks_completed_today_excludes_older_completions(self):
        volunteer = self._make_volunteer("an_v7")
        HelpRequest.objects.create(
            client=self.client_user, volunteer=volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="completed", completed_at=timezone.now(),
        )
        HelpRequest.objects.create(
            client=self.client_user, volunteer=volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="completed", completed_at=timezone.now() - timezone.timedelta(days=3),
        )
        self.assertEqual(analytics.dashboard_stats()["tasks_completed_today"], 1)

    def test_recent_activity_includes_task_creation(self):
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p", region="dushanbe"
        )
        events = analytics.recent_activity()
        self.assertTrue(any(event["type"] == "task_created" for event in events))

    def test_recent_activity_respects_limit(self):
        for i in range(15):
            HelpRequest.objects.create(
                client=self.client_user, help_type="grocery", description=f"x{i}", address="a", phone="p", region="dushanbe"
            )
        self.assertLessEqual(len(analytics.recent_activity(limit=5)), 5)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CrmTasksViewTests(TestCase):
    def setUp(self):
        self.admin = Users.objects.create_superuser(username="ct_admin", email="ct_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="ct_curator", email="ct_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="ct_vol", email="ct_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="ct_client", email="ct_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

        self.pending_task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="Need bread", address="a", phone="p",
            region="dushanbe", status="pending",
        )
        self.urgent_task = HelpRequest.objects.create(
            client=self.client_user, help_type="medical", description="Urgent care", address="a", phone="p",
            region="khatlon", status="pending", is_urgent=True,
        )
        self.active_task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active", accepted_at=timezone.now(),
        )
        self.overdue_task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="transport", description="x", address="a",
            phone="p", region="dushanbe", status="active", accepted_at=timezone.now() - timezone.timedelta(hours=5),
        )

    # Permissions
    def test_admin_can_access(self):
        self.client.login(username="ct_admin", password="pass12345")
        self.assertEqual(self.client.get(reverse("crm_tasks")).status_code, 200)

    def test_curator_can_access(self):
        self.client.login(username="ct_curator", password="pass12345")
        self.assertEqual(self.client.get(reverse("crm_tasks")).status_code, 200)

    def test_volunteer_denied(self):
        self.client.login(username="ct_vol", password="pass12345")
        self.assertRedirects(self.client.get(reverse("crm_tasks")), reverse("profile"))

    def test_client_denied(self):
        self.client.login(username="ct_client", password="pass12345")
        self.assertRedirects(self.client.get(reverse("crm_tasks")), reverse("profile"))

    def test_anonymous_denied(self):
        self.assertEqual(self.client.get(reverse("crm_tasks")).status_code, 302)

    # Filters
    def test_filter_by_status(self):
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"status": "pending"})
        ids = [task.id for task in response.context["page_obj"]]
        self.assertIn(self.pending_task.id, ids)
        self.assertIn(self.urgent_task.id, ids)
        self.assertNotIn(self.active_task.id, ids)

    def test_filter_by_region(self):
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"region": "khatlon"})
        ids = [task.id for task in response.context["page_obj"]]
        self.assertEqual(ids, [self.urgent_task.id])

    def test_filter_by_urgent(self):
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"is_urgent": "on"})
        ids = [task.id for task in response.context["page_obj"]]
        self.assertEqual(ids, [self.urgent_task.id])

    def test_filter_by_volunteer(self):
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"volunteer": "ct_vol"})
        ids = {task.id for task in response.context["page_obj"]}
        self.assertEqual(ids, {self.active_task.id, self.overdue_task.id})

    def test_search_by_description(self):
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"q": "bread"})
        ids = [task.id for task in response.context["page_obj"]]
        self.assertEqual(ids, [self.pending_task.id])

    def test_overdue_only_filter(self):
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"overdue": "1"})
        ids = [task.id for task in response.context["page_obj"]]
        self.assertEqual(ids, [self.overdue_task.id])
        self.assertTrue(response.context["overdue_only"])

    def test_date_from_filter_excludes_earlier_tasks(self):
        self.client.login(username="ct_admin", password="pass12345")
        tomorrow = (timezone.localdate() + timezone.timedelta(days=1)).isoformat()
        response = self.client.get(reverse("crm_tasks"), {"date_from": tomorrow})
        self.assertEqual(len(response.context["page_obj"].object_list), 0)

    # Pagination
    def test_pagination_limits_page_size(self):
        for i in range(25):
            HelpRequest.objects.create(
                client=self.client_user, help_type="grocery", description=f"bulk{i}", address="a", phone="p",
                region="dushanbe",
            )
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"))
        self.assertEqual(len(response.context["page_obj"].object_list), 20)
        self.assertTrue(response.context["page_obj"].has_next())
        self.assertEqual(self.client.get(reverse("crm_tasks"), {"page": 2}).status_code, 200)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CrmVolunteerDetailViewTests(TestCase):
    def setUp(self):
        self.admin = Users.objects.create_superuser(username="cvd_admin", email="cvd_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="cvd_curator", email="cvd_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="cvd_vol", email="cvd_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.viewer_volunteer = Users.objects.create_user(
            username="cvd_vol2", email="cvd_vol2@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="cvd_client", email="cvd_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="completed",
        )
        HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active",
        )

    def test_admin_can_view_with_correct_stats(self):
        self.client.login(username="cvd_admin", password="pass12345")
        response = self.client.get(reverse("crm_volunteer_detail", args=[self.volunteer.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["completed_count"], 1)
        self.assertEqual(response.context["active_count"], 1)

    def test_curator_can_view(self):
        self.client.login(username="cvd_curator", password="pass12345")
        self.assertEqual(
            self.client.get(reverse("crm_volunteer_detail", args=[self.volunteer.pk])).status_code, 200
        )

    def test_volunteer_denied(self):
        self.client.login(username="cvd_vol2", password="pass12345")
        response = self.client.get(reverse("crm_volunteer_detail", args=[self.volunteer.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_client_denied(self):
        self.client.login(username="cvd_client", password="pass12345")
        response = self.client.get(reverse("crm_volunteer_detail", args=[self.volunteer.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_freshness_none_when_no_location_set(self):
        self.client.login(username="cvd_admin", password="pass12345")
        response = self.client.get(reverse("crm_volunteer_detail", args=[self.volunteer.pk]))
        self.assertIsNone(response.context["location_freshness"])

    def test_freshness_fresh_right_after_set_location(self):
        self.volunteer.profile.set_location(38.55, 68.78)
        self.client.login(username="cvd_admin", password="pass12345")
        response = self.client.get(reverse("crm_volunteer_detail", args=[self.volunteer.pk]))
        self.assertEqual(response.context["location_freshness"], "fresh")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PeopleListCrmTests(TestCase):
    """Phase 5 additions to the existing people directory: availability
    filter + per-row stat annotations. (No prior tests existed for this
    view; these are additive, not a replacement of anything.)"""

    def setUp(self):
        self.admin = Users.objects.create_superuser(username="pl_admin", email="pl_admin@example.com", password="pass12345")
        self.available_vol = Users.objects.create_user(
            username="pl_avail", email="pl_avail@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.busy_vol = Users.objects.create_user(
            username="pl_busy", email="pl_busy@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.busy_vol.profile.availability_status = "busy"
        self.busy_vol.profile.save()
        self.client_user = Users.objects.create_user(
            username="pl_client", email="pl_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

    def test_admin_can_access_people_list(self):
        self.client.login(username="pl_admin", password="pass12345")
        self.assertEqual(self.client.get(reverse("people_list", args=["volunteer"])).status_code, 200)

    def test_volunteer_cannot_access_people_list(self):
        self.client.login(username="pl_avail", password="pass12345")
        response = self.client.get(reverse("people_list", args=["volunteer"]))
        self.assertRedirects(response, reverse("profile"))

    def test_client_cannot_access_people_list(self):
        self.client.login(username="pl_client", password="pass12345")
        response = self.client.get(reverse("people_list", args=["volunteer"]))
        self.assertRedirects(response, reverse("profile"))

    def test_availability_filter_narrows_results(self):
        self.client.login(username="pl_admin", password="pass12345")
        response = self.client.get(reverse("people_list", args=["volunteer"]), {"availability": "busy"})
        usernames = [person.username for person in response.context["people"]]
        self.assertEqual(usernames, ["pl_busy"])

    def test_client_list_ignores_availability_param(self):
        self.client.login(username="pl_admin", password="pass12345")
        response = self.client.get(reverse("people_list", args=["client"]), {"availability": "busy"})
        self.assertEqual(response.status_code, 200)
        usernames = [person.username for person in response.context["people"]]
        self.assertIn("pl_client", usernames)

    def test_volunteer_row_task_count_annotation(self):
        HelpRequest.objects.create(
            client=self.client_user, volunteer=self.available_vol, help_type="grocery", description="x",
            address="a", phone="p", region="dushanbe", status="completed",
        )
        self.client.login(username="pl_admin", password="pass12345")
        response = self.client.get(reverse("people_list", args=["volunteer"]))
        person = next(p for p in response.context["people"] if p.username == "pl_avail")
        self.assertEqual(person.completed_task_count, 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AdminPanelDashboardTests(TestCase):
    """admin_panel_view had no direct end-to-end render coverage before
    Phase 5 despite being the CRM dashboard home — this closes that gap and
    checks the new KPI/activity additions land in the response."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(username="dash_admin", email="dash_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="dash_curator", email="dash_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="dash_vol", email="dash_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="dash_client", email="dash_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

    def test_admin_dashboard_renders_with_stats_and_activity(self):
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p", region="dushanbe"
        )
        self.client.login(username="dash_admin", password="pass12345")
        response = self.client.get(reverse("admin_panel"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("stats", response.context)
        self.assertIn("recent_activity", response.context)
        self.assertContains(response, 'href="/myapp/crm/tasks/"')

    def test_curator_dashboard_renders(self):
        self.client.login(username="dash_curator", password="pass12345")
        self.assertEqual(self.client.get(reverse("admin_panel")).status_code, 200)

    def test_volunteer_denied_dashboard(self):
        self.client.login(username="dash_vol", password="pass12345")
        self.assertRedirects(self.client.get(reverse("admin_panel")), reverse("profile"))

    def test_client_denied_dashboard(self):
        self.client.login(username="dash_client", password="pass12345")
        self.assertRedirects(self.client.get(reverse("admin_panel")), reverse("profile"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TaskDetailCrmIntegrationTests(TestCase):
    """Phase 5's task-detail additions: the location-only mini-map (shown
    only when the route card isn't already showing a map) and the
    admin/curator-only task-history panel."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(username="tdi_admin", email="tdi_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="tdi_curator", email="tdi_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="tdi_vol", email="tdi_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="tdi_client", email="tdi_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

    def test_location_map_shown_for_pending_task_with_coordinates(self):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending", latitude=TASK_LAT, longitude=TASK_LNG,
        )
        self.client.login(username="tdi_admin", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertTrue(response.context["show_location_map"])
        self.assertContains(response, 'id="locationMapCard"')

    def test_location_map_hidden_when_task_has_no_coordinates(self):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending",
        )
        self.client.login(username="tdi_admin", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertFalse(response.context["show_location_map"])
        self.assertNotContains(response, 'id="locationMapCard"')

    def test_location_map_yields_to_route_card_when_route_is_shown(self):
        task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active", latitude=TASK_LAT, longitude=TASK_LNG,
        )
        self.client.login(username="tdi_vol", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertTrue(response.context["show_route"])
        self.assertFalse(response.context["show_location_map"])
        self.assertContains(response, 'id="routeCard"')
        self.assertNotContains(response, 'id="locationMapCard"')
        # Leaflet must load exactly once even though both features can use it.
        self.assertEqual(response.content.decode().count("leaflet@1.9.4/dist/leaflet.js"), 1)

    def test_history_visible_to_admin_and_curator(self):
        task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="completed", accepted_at=timezone.now(), completed_at=timezone.now(),
        )
        self.client.login(username="tdi_admin", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertTrue(response.context["show_history"])
        self.assertContains(response, 'data-i18n="crm.task_history"')

        self.client.logout()
        self.client.login(username="tdi_curator", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertTrue(response.context["show_history"])
        self.assertContains(response, 'data-i18n="crm.task_history"')

    def test_history_hidden_from_volunteer_and_client(self):
        task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active",
        )
        self.client.login(username="tdi_vol", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertFalse(response.context["show_history"])
        self.assertNotContains(response, 'data-i18n="crm.task_history"')

        self.client.logout()
        self.client.login(username="tdi_client", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertFalse(response.context["show_history"])
        self.assertNotContains(response, 'data-i18n="crm.task_history"')

    def test_history_includes_overdue_entry_when_applicable(self):
        task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active", accepted_at=timezone.now() - timezone.timedelta(hours=4),
        )
        self.client.login(username="tdi_admin", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertContains(response, 'data-i18n="crm.history_overdue"')

    def test_history_omits_overdue_entry_when_not_applicable(self):
        task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="active", accepted_at=timezone.now(),
        )
        self.client.login(username="tdi_admin", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[task.pk]))
        self.assertNotContains(response, 'data-i18n="crm.history_overdue"')
