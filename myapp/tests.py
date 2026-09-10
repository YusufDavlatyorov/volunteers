import io
import threading
from decimal import Decimal
from unittest import mock

import requests
from PIL import Image
from django.conf import settings
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, OperationalError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Users, hash_token
from .forms import HelpRequestForm, PhotoReportForm
from .models import (
    Donation,
    EmergencyReport,
    HelpRequest,
    PetReport,
    PhotoReport,
    Product,
    STALE_PENDING_THRESHOLD,
    VolunteerApplication,
)
from .notifications import notify_users
from .services import analytics, dashboard, donations, emergency, maps, overdue, pets, stale
from .services.geo import get_route, haversine_km, is_valid_coordinate
from .services.matching import TASK_REC_CANDIDATE_CAP, recommend_tasks, recommend_volunteers
from .services.telegram_link import LINK_ATTEMPT_LIMIT, redeem_link_code


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

    def test_map_data_payload_carries_ops_fields(self):
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="mine", address="a", phone="p",
            region="dushanbe", latitude=38.5, longitude=68.7, priority="emergency",
        )
        self.client.login(username="map_client", password="pass12345")
        point = next(p for p in self.client.get(reverse("map_data")).json()["points"] if p["kind"] == "task")
        for key in ("id", "kind", "status", "priority", "help_type", "region", "is_overdue", "url"):
            self.assertIn(key, point)
        self.assertEqual(point["priority"], "emergency")
        self.assertFalse(point["is_overdue"])

    def test_map_data_marks_overdue_active_task(self):
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="late", address="a", phone="p",
            region="dushanbe", latitude=38.5, longitude=68.7, status="active", volunteer=self.volunteer,
            accepted_at=timezone.now() - timezone.timedelta(hours=4),
        )
        self.client.login(username="map_client", password="pass12345")
        point = next(p for p in self.client.get(reverse("map_data")).json()["points"] if p["kind"] == "task")
        self.assertTrue(point["is_overdue"])

    def test_located_client_gets_a_me_marker_but_still_only_own_tasks(self):
        self.client_user.profile.set_location(38.52, 68.75)
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="mine", address="a", phone="p",
            region="dushanbe", latitude=38.5, longitude=68.7,
        )
        other = Users.objects.create_user(
            username="c_other", email="c_other@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        HelpRequest.objects.create(
            client=other, help_type="grocery", description="theirs", address="a", phone="p",
            region="dushanbe", latitude=38.6, longitude=68.8,
        )
        self.client.login(username="map_client", password="pass12345")
        points = self.client.get(reverse("map_data")).json()["points"]
        self.assertEqual(sum(1 for p in points if p["kind"] == "me"), 1)
        self.assertEqual(sum(1 for p in points if p["kind"] == "task"), 1)

    def test_ops_map_page_renders_component_and_filter_context(self):
        self.client.login(username="map_admin", password="pass12345")
        response = self.client.get(reverse("map"))
        self.assertContains(response, 'id="opsMap"')
        self.assertContains(response, "ops-map__panel")
        self.assertContains(response, 'id="opsFilterPriority"')
        self.assertIn("priority_choices", response.context)


class PriorityFieldTests(TestCase):
    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="pr_client", email="pr_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

    def test_priority_defaults_to_normal(self):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p", region="dushanbe",
        )
        self.assertEqual(task.priority, "normal")
        self.assertFalse(task.is_urgent)

    def test_legacy_is_urgent_create_still_works(self):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", is_urgent=True,
        )
        self.assertTrue(task.is_urgent)

    def test_form_priority_syncs_is_urgent(self):
        self.client.login(username="pr_client", password="pass12345")
        self.client.post(reverse("create_request"), {
            "help_type": "medical", "description": "urgent", "address": "a", "phone": "+992", "priority": "emergency",
        })
        task = HelpRequest.objects.get(client=self.client_user)
        self.assertEqual(task.priority, "emergency")
        self.assertTrue(task.is_urgent)

    def test_form_normal_priority_leaves_is_urgent_false(self):
        self.client.login(username="pr_client", password="pass12345")
        self.client.post(reverse("create_request"), {
            "help_type": "grocery", "description": "calm", "address": "a", "phone": "+992", "priority": "normal",
        })
        task = HelpRequest.objects.get(client=self.client_user)
        self.assertEqual(task.priority, "normal")
        self.assertFalse(task.is_urgent)

    def test_crm_filter_by_priority(self):
        admin = Users.objects.create_superuser(username="pr_admin", email="pr_admin@example.com", password="pass12345")
        HelpRequest.objects.create(
            client=self.client_user, help_type="medical", description="emergency one", address="a", phone="p",
            region="dushanbe", priority="emergency",
        )
        HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="calm one", address="a", phone="p",
            region="dushanbe", priority="normal",
        )
        self.client.login(username="pr_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"priority": "emergency"})
        rows = list(response.context["page_obj"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].priority, "emergency")


class StartRouteTests(TestCase):
    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="sr_client", email="sr_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.volunteer = Users.objects.create_user(
            username="sr_vol", email="sr_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.other_vol = Users.objects.create_user(
            username="sr_vol2", email="sr_vol2@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="active", volunteer=self.volunteer, latitude=38.5, longitude=68.7,
        )

    def _get(self, username):
        self.client.login(username=username, password="pass12345")
        return self.client.get(reverse("task_detail", args=[self.task.pk]))

    def test_assigned_volunteer_sees_start_route_deep_link(self):
        response = self._get("sr_vol")
        self.assertContains(response, "google.com/maps/dir/")
        self.assertContains(response, "destination=38.5")

    def test_client_and_other_volunteer_do_not_see_start_route(self):
        self.assertNotContains(self._get("sr_client"), "google.com/maps/dir/")
        # other volunteer can't even view this active task
        self.assertEqual(self._get("sr_vol2").status_code, 302)

    def test_no_start_route_without_task_location(self):
        self.task.latitude = self.task.longitude = None
        self.task.save(update_fields=["latitude", "longitude"])
        self.assertNotContains(self._get("sr_vol"), "google.com/maps/dir/")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class WorkStageTests(TestCase):
    """HelpRequest.work_stage — the volunteer's on-the-ground progress while active."""

    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="ws_client", email="ws_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.volunteer = Users.objects.create_user(
            username="ws_vol", email="ws_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.curator = Users.objects.create_user(
            username="ws_curator", email="ws_curator@example.com", password="pass12345", is_curator=True
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending",
        )

    def _advance(self, username, stage):
        self.client.login(username=username, password="pass12345")
        return self.client.post(reverse("task_advance_stage", args=[self.task.pk]), {"stage": stage})

    def test_accept_sets_work_stage_assigned(self):
        self.client.login(username="ws_vol", password="pass12345")
        self.client.post(reverse("accept_task", args=[self.task.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "active")
        self.assertEqual(self.task.work_stage, "assigned")

    def test_volunteer_advances_through_the_stages(self):
        self.task.accept(self.volunteer)
        for stage in ("en_route", "arrived", "in_progress"):
            self._advance("ws_vol", stage)
            self.task.refresh_from_db()
            self.assertEqual(self.task.work_stage, stage)

    def test_cannot_move_backward(self):
        self.task.accept(self.volunteer)
        self.task.advance_work_stage("in_progress")
        self._advance("ws_vol", "assigned")
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_stage, "in_progress")

    def test_cannot_advance_a_pending_or_completed_task(self):
        self._advance("ws_vol", "en_route")  # still pending
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_stage, "assigned")
        self.task.accept(self.volunteer)
        self.task.complete()
        self._advance("ws_vol", "en_route")
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_stage, "assigned")

    def test_unrelated_volunteer_and_client_denied(self):
        self.task.accept(self.volunteer)
        other = Users.objects.create_user(
            username="ws_vol2", email="ws_vol2@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.assertEqual(self._advance("ws_vol2", "en_route").status_code, 302)
        self.assertEqual(self._advance("ws_client", "en_route").status_code, 302)
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_stage, "assigned")

    def test_staff_can_correct_the_stage(self):
        self.task.accept(self.volunteer)
        self._advance("ws_curator", "arrived")
        self.task.refresh_from_db()
        self.assertEqual(self.task.work_stage, "arrived")

    def test_client_notified_on_en_route_and_arrived_only(self):
        self.task.accept(self.volunteer)
        mail.outbox.clear()
        self._advance("ws_vol", "en_route")
        self.assertEqual(len(mail.outbox), 1)
        mail.outbox.clear()
        self._advance("ws_vol", "arrived")
        self.assertEqual(len(mail.outbox), 1)
        mail.outbox.clear()
        self._advance("ws_vol", "in_progress")
        self.assertEqual(len(mail.outbox), 0)

    def test_complete_still_works_from_any_stage_and_awards_points(self):
        self.task.accept(self.volunteer)
        self.task.advance_work_stage("in_progress")
        self.client.login(username="ws_vol", password="pass12345")
        self.client.post(reverse("complete_task", args=[self.task.pk]))
        self.task.refresh_from_db()
        self.volunteer.profile.refresh_from_db()
        self.assertEqual(self.task.status, "completed")
        self.assertEqual(self.volunteer.profile.rating, 3)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DirectAssignTests(TestCase):
    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="da_client", email="da_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.volunteer = Users.objects.create_user(
            username="da_vol", email="da_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.curator = Users.objects.create_user(
            username="da_curator", email="da_curator@example.com", password="pass12345", is_curator=True
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending",
        )

    def _assign(self, username, volunteer_id=None):
        self.client.login(username=username, password="pass12345")
        return self.client.post(reverse("task_assign_volunteer", args=[self.task.pk, volunteer_id or self.volunteer.pk]))

    def test_curator_assigns_pending_task(self):
        mail.outbox.clear()
        response = self._assign("da_curator")
        self.assertJSONEqual(response.content, {"success": True})
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "active")
        self.assertEqual(self.task.volunteer, self.volunteer)
        self.assertEqual(self.task.work_stage, "assigned")
        self.assertEqual(len(mail.outbox), 1)  # one send to [volunteer, client]

    def test_cannot_assign_non_pending_task(self):
        self.task.accept(self.volunteer)
        response = self._assign("da_curator")
        self.assertEqual(response.json()["success"], False)

    def test_volunteer_and_client_and_anon_cannot_assign(self):
        self.assertIn(self._assign("da_vol").status_code, (302, 403))
        self.assertIn(self._assign("da_client").status_code, (302, 403))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "pending")


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


def _nominatim_response(payload):
    r = mock.Mock()
    r.raise_for_status.return_value = None
    r.json.return_value = payload
    return r


class MapsServiceTests(TestCase):
    """myapp.services.maps — the provider-agnostic layer. Network mocked at
    myapp.services.maps.requests.get."""

    def test_tile_layer_osm_shape(self):
        cfg = maps.tile_layer()
        self.assertIn("openstreetmap.org", cfg["url"])
        self.assertIn("attribution", cfg)
        self.assertEqual(cfg["max_zoom"], 19)

    @override_settings(MAPS_PROVIDER="totally-unknown")
    def test_unknown_provider_falls_back_to_osm(self):
        self.assertIn("openstreetmap.org", maps.tile_layer()["url"])

    @override_settings(MAPS_PROVIDER="mapbox", MAPS_API_KEY="")
    def test_mapbox_without_key_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            maps.tile_layer()

    @override_settings(MAPS_PROVIDER="mapbox", MAPS_API_KEY="pk.test123")
    def test_mapbox_with_key_builds_url(self):
        self.assertIn("pk.test123", maps.tile_layer()["url"])

    def test_geocode_success_returns_tuple(self):
        with mock.patch(
            "myapp.services.maps.requests.get",
            return_value=_nominatim_response([{"lat": "38.5598", "lon": "68.7870"}]),
        ) as m:
            coords = maps.geocode("ул. Рудаки 12", region="dushanbe")
        self.assertEqual(coords, (38.5598, 68.787))
        # Nominatim policy: a User-Agent must be sent, and the region label appended.
        self.assertEqual(m.call_args.kwargs["headers"]["User-Agent"], settings.NOMINATIM_USER_AGENT)
        self.assertIn("Душанбе", m.call_args.kwargs["params"]["q"])

    def test_geocode_empty_query_short_circuits(self):
        with mock.patch("myapp.services.maps.requests.get") as m:
            self.assertIsNone(maps.geocode("   "))
        m.assert_not_called()

    def test_geocode_network_failure_returns_none(self):
        with mock.patch("myapp.services.maps.requests.get", side_effect=requests.RequestException("boom")):
            self.assertIsNone(maps.geocode("somewhere"))

    def test_geocode_no_results_returns_none(self):
        with mock.patch("myapp.services.maps.requests.get", return_value=_nominatim_response([])):
            self.assertIsNone(maps.geocode("nowhere at all"))

    def test_geocode_out_of_range_result_rejected(self):
        with mock.patch(
            "myapp.services.maps.requests.get",
            return_value=_nominatim_response([{"lat": "999", "lon": "68.78"}]),
        ):
            self.assertIsNone(maps.geocode("bad"))

    def test_route_delegates_to_geo_get_route(self):
        sentinel = {"success": True, "distance_km": 1.0, "duration_min": 2.0, "geometry": []}
        with mock.patch("myapp.services.maps._osm_route", return_value=sentinel) as m:
            result = maps.route((38.5, 68.7), (38.6, 68.8))
        self.assertIs(result, sentinel)
        m.assert_called_once_with((38.5, 68.7), (38.6, 68.8))


class CreateRequestGeocodingTests(TestCase):
    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="geo_client", email="geo_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        self.client.login(username="geo_client", password="pass12345")

    def _post(self, **extra):
        data = {"help_type": "grocery", "description": "x", "address": "ул. Рудаки 12", "phone": "+992"}
        data.update(extra)
        return self.client.post(reverse("create_request"), data)

    def test_address_is_geocoded_when_no_pin(self):
        with mock.patch("myapp.views.maps.geocode", return_value=(38.5598, 68.787)) as m:
            response = self._post()
        self.assertEqual(response.status_code, 302)
        task = HelpRequest.objects.get(client=self.client_user)
        self.assertTrue(task.has_location)
        self.assertAlmostEqual(float(task.latitude), 38.5598)
        m.assert_called_once()

    def test_unresolvable_address_still_saves_without_coordinates(self):
        with mock.patch("myapp.views.maps.geocode", return_value=None):
            response = self._post()
        self.assertEqual(response.status_code, 302)
        task = HelpRequest.objects.get(client=self.client_user)
        self.assertFalse(task.has_location)

    def test_map_pin_wins_and_geocode_not_called(self):
        with mock.patch("myapp.views.maps.geocode") as m:
            response = self._post(latitude="38.60", longitude="68.80")
        self.assertEqual(response.status_code, 302)
        task = HelpRequest.objects.get(client=self.client_user)
        self.assertAlmostEqual(float(task.latitude), 38.60)
        m.assert_not_called()


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
                         active_tasks=0, location_age=None, has_location=True, skills=None):
        self._counter += 1
        volunteer = Users.objects.create_user(
            username=f"vol_{self._counter}", email=f"vol_{self._counter}@example.com",
            password="pass12345", is_volunteer=True, region=region,
        )
        profile = volunteer.profile
        profile.availability_status = availability
        if skills is not None:
            profile.skills = skills
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

    # Skills (task.help_type is "grocery" in setUp)
    def test_skill_match_ranks_higher_all_else_equal(self):
        matches = self._make_volunteer(distance_km=5, skills=["grocery", "transport"])
        differs = self._make_volunteer(distance_km=5, skills=["medical"])
        ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertLess(ids.index(matches.id), ids.index(differs.id))

    def test_empty_skills_is_neutral_not_a_penalty(self):
        no_skills = self._make_volunteer(distance_km=5, skills=[])
        wrong_skills = self._make_volunteer(distance_km=5, skills=["medical"])
        ids = [item["volunteer"].id for item in recommend_volunteers(self.task)]
        self.assertLess(ids.index(no_skills.id), ids.index(wrong_skills.id))

    def test_skill_match_label_in_payload(self):
        self._make_volunteer(distance_km=5, skills=["grocery"])
        self._make_volunteer(distance_km=6, skills=["medical"])
        self._make_volunteer(distance_km=7)
        labels = {item["skill_match"] for item in recommend_volunteers(self.task)}
        self.assertEqual(labels, {"match", "mismatch", "none"})


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

    def test_stale_pending_counted_correctly(self):
        stuck = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending",
        )
        HelpRequest.objects.filter(pk=stuck.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=60)
        )
        HelpRequest.objects.create(  # fresh pending — not stale
            client=self.client_user, help_type="grocery", description="y", address="a", phone="p",
            region="dushanbe", status="pending",
        )
        counts = analytics.task_status_breakdown()
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(analytics.dashboard_stats()["tasks_stale"], 1)

    def test_dashboard_stats_has_every_expected_key(self):
        stats = analytics.dashboard_stats()
        for key in [
            "volunteers_total", "volunteers_available", "volunteers_busy", "volunteers_offline",
            "clients_total", "tasks_pending", "tasks_active", "tasks_completed", "tasks_overdue",
            "tasks_stale", "tasks_urgent", "tasks_completed_today", "pending_applications",
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

    def test_stale_only_filter(self):
        HelpRequest.objects.filter(pk=self.pending_task.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=60)
        )
        self.client.login(username="ct_admin", password="pass12345")
        response = self.client.get(reverse("crm_tasks"), {"stale": "1"})
        ids = [task.id for task in response.context["page_obj"]]
        self.assertEqual(ids, [self.pending_task.id])  # urgent_task is fresh, not stale
        self.assertTrue(response.context["stale_only"])

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


class TaskDetailIdorTests(TestCase):
    """CRITICAL: task_detail_view used to let any authenticated user open any
    pending HelpRequest (address/phone/description leaked) just by knowing
    its <pk>, because `task.status == "pending"` was one of the
    OR-conditions in the old permission check."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(username="idor_admin", email="idor_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="idor_curator", email="idor_curator@example.com", password="pass12345", is_curator=True
        )
        self.client_a = Users.objects.create_user(
            username="idor_client_a", email="idor_client_a@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.client_b = Users.objects.create_user(
            username="idor_client_b", email="idor_client_b@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.vol_same_region = Users.objects.create_user(
            username="idor_vol_same", email="idor_vol_same@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.vol_other_region = Users.objects.create_user(
            username="idor_vol_other", email="idor_vol_other@example.com", password="pass12345", is_volunteer=True, region="khatlon"
        )
        self.vol_no_region = Users.objects.create_user(
            username="idor_vol_none", email="idor_vol_none@example.com", password="pass12345", is_volunteer=True, region=""
        )
        self.task = HelpRequest.objects.create(
            client=self.client_a, help_type="grocery", description="secret groceries", address="12 Secret St",
            phone="+992900000000", region="dushanbe", status="pending",
        )

    def test_other_client_cannot_view_someone_elses_pending_task(self):
        self.client.login(username="idor_client_b", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_owning_client_can_view_own_task(self):
        self.client.login(username="idor_client_a", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "12 Secret St")

    def test_volunteer_in_matching_region_can_view_pending_task(self):
        self.client.login(username="idor_vol_same", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)

    def test_volunteer_in_other_region_cannot_view_pending_task(self):
        self.client.login(username="idor_vol_other", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_volunteer_with_no_region_can_view_any_pending_task(self):
        # Matches task_list_view: a volunteer with no region set sees pending
        # tasks from every region, so the detail page must allow it too.
        self.client.login(username="idor_vol_none", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertEqual(response.status_code, 200)

    def test_volunteer_cannot_view_another_volunteers_active_task(self):
        self.task.status = "active"
        self.task.volunteer = self.vol_same_region
        self.task.accepted_at = timezone.now()
        self.task.save()
        bystander = Users.objects.create_user(
            username="idor_vol_bystander", email="idor_vol_bystander@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        self.client.login(username="idor_vol_bystander", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertRedirects(response, reverse("profile"))

    def test_curator_and_admin_have_full_access(self):
        for username in ("idor_curator", "idor_admin"):
            self.client.login(username=username, password="pass12345")
            response = self.client.get(reverse("task_detail", args=[self.task.pk]))
            self.assertEqual(response.status_code, 200)
            self.client.logout()

    def test_incrementing_pk_does_not_leak_another_clients_task(self):
        # The exact IDOR shape described in the report: probe an adjacent id.
        other_task = HelpRequest.objects.create(
            client=self.client_b, help_type="medical", description="other client's private issue", address="99 Other Ave",
            phone="+992911111111", region="dushanbe", status="pending",
        )
        self.assertEqual(other_task.pk, self.task.pk + 1)  # sanity: adjacent ids, as a real IDOR probe would try
        self.client.login(username="idor_client_a", password="pass12345")
        response = self.client.get(reverse("task_detail", args=[other_task.pk]))
        self.assertRedirects(response, reverse("profile"))


class AcceptTaskRaceConditionTests(TestCase):
    """HIGH: accept_task_view raced. These are deterministic simulations of
    the exact interleavings the fix must survive; AcceptTaskConcurrencyTests
    below exercises the same two races with real concurrent threads."""

    def setUp(self):
        cache.clear()
        self.client_user = Users.objects.create_user(
            username="race_det_client", email="race_det_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.vol_a = Users.objects.create_user(
            username="race_det_vol_a", email="race_det_vol_a@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p", region="dushanbe",
        )

    def tearDown(self):
        cache.clear()

    def test_task_taken_between_page_load_and_click_is_rejected_not_overwritten(self):
        # Simulates another request winning the race a moment earlier: by the
        # time our conditional UPDATE runs, status is no longer "pending".
        other_vol = Users.objects.create_user(
            username="race_det_vol_b", email="race_det_vol_b@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        HelpRequest.objects.filter(pk=self.task.pk).update(volunteer=other_vol, status="active", accepted_at=timezone.now())

        self.client.login(username="race_det_vol_a", password="pass12345")
        response = self.client.post(reverse("accept_task", args=[self.task.pk]))
        self.assertRedirects(response, reverse("task_list"))

        self.task.refresh_from_db()
        self.assertEqual(self.task.volunteer_id, other_vol.pk)  # not overwritten by the loser
        self.assertEqual(self.task.status, "active")

    def test_in_flight_accept_lock_blocks_a_second_concurrent_attempt(self):
        # Simulates the same volunteer's second concurrent request arriving
        # while the first request is still mid-flight (lock held).
        second_task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="y", address="a", phone="p", region="dushanbe",
        )
        cache.set(f"accept_task_lock:{self.vol_a.pk}", "1", 10)

        self.client.login(username="race_det_vol_a", password="pass12345")
        response = self.client.post(reverse("accept_task", args=[second_task.pk]))
        self.assertRedirects(response, reverse("task_list"))

        second_task.refresh_from_db()
        self.assertEqual(second_task.status, "pending")
        self.assertIsNone(second_task.volunteer)


class AcceptTaskConcurrencyTests(TransactionTestCase):
    """The two races named in the report, exercised against real concurrent
    requests on separate DB connections/threads (TransactionTestCase, not
    TestCase, so the threads can actually see each other's committed rows)."""

    def setUp(self):
        cache.clear()
        self.client_user = Users.objects.create_user(
            username="race_live_client", email="race_live_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.vol_a = Users.objects.create_user(
            username="race_live_vol_a", email="race_live_vol_a@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.vol_b = Users.objects.create_user(
            username="race_live_vol_b", email="race_live_vol_b@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )

    def tearDown(self):
        cache.clear()

    def _accept_in_thread(self, username, task_pk, barrier):
        def run():
            barrier.wait()
            try:
                thread_client = Client()
                thread_client.login(username=username, password="pass12345")
                thread_client.post(reverse("accept_task", args=[task_pk]))
            except OperationalError:
                # SQLite's own busy-timeout occasionally trips under real
                # thread contention on a loaded test machine (even the
                # session-write in .login() can hit it); the assertions below
                # only look at final committed state, so a request that
                # simply lost the race to even acquire the write lock is a
                # legitimate (if noisy) outcome here, not a test bug.
                pass
            finally:
                connection.close()

        thread = threading.Thread(target=run)
        thread.start()
        return thread

    def test_two_volunteers_racing_the_same_task_only_one_wins(self):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p", region="dushanbe",
        )
        barrier = threading.Barrier(2)
        t1 = self._accept_in_thread("race_live_vol_a", task.pk, barrier)
        t2 = self._accept_in_thread("race_live_vol_b", task.pk, barrier)
        t1.join(timeout=10)
        t2.join(timeout=10)

        task.refresh_from_db()
        self.assertEqual(task.status, "active")
        self.assertIn(task.volunteer_id, [self.vol_a.pk, self.vol_b.pk])
        self.assertEqual(HelpRequest.objects.filter(status="active").count(), 1)

    def test_one_volunteer_racing_two_tasks_ends_up_with_at_most_one_active(self):
        task1 = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p", region="dushanbe",
        )
        task2 = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="y", address="a", phone="p", region="dushanbe",
        )
        barrier = threading.Barrier(2)
        t1 = self._accept_in_thread("race_live_vol_a", task1.pk, barrier)
        t2 = self._accept_in_thread("race_live_vol_a", task2.pk, barrier)
        t1.join(timeout=10)
        t2.join(timeout=10)

        active_count = HelpRequest.objects.filter(volunteer=self.vol_a, status="active").count()
        self.assertLessEqual(active_count, 1)

    def test_curator_assign_racing_a_self_accept_only_one_wins(self):
        curator = Users.objects.create_user(
            username="race_live_curator", email="race_live_curator@example.com", password="pass12345", is_curator=True
        )
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="z", address="a", phone="p", region="dushanbe",
        )

        def assign_run(barrier):
            barrier.wait()
            try:
                c = Client(); c.login(username="race_live_curator", password="pass12345")
                c.post(reverse("task_assign_volunteer", args=[task.pk, self.vol_b.pk]))
            except OperationalError:
                pass
            finally:
                connection.close()

        barrier = threading.Barrier(2)
        t1 = self._accept_in_thread("race_live_vol_a", task.pk, barrier)
        t2 = threading.Thread(target=assign_run, args=(barrier,)); t2.start()
        t1.join(timeout=10); t2.join(timeout=10)

        task.refresh_from_db()
        self.assertEqual(task.status, "active")
        self.assertIn(task.volunteer_id, [self.vol_a.pk, self.vol_b.pk])
        self.assertEqual(HelpRequest.objects.filter(pk=task.pk, status="active").count(), 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CheckOverdueMethodTests(TestCase):
    """MEDIUM: GET /myapp/archive/check-overdue/ performed a state change
    (sending curator alerts, flipping alarm_sent) from a plain link, with no
    CSRF protection for that action."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(username="co_admin", email="co_admin@example.com", password="pass12345")

    def test_get_check_overdue_is_rejected(self):
        self.client.login(username="co_admin", password="pass12345")
        response = self.client.get(reverse("check_overdue"))
        self.assertEqual(response.status_code, 405)

    def test_post_check_overdue_works(self):
        self.client.login(username="co_admin", password="pass12345")
        response = self.client.post(reverse("check_overdue"))
        self.assertRedirects(response, reverse("admin_panel"))

    def test_post_check_overdue_alerts_once_then_is_idempotent(self):
        """The POST view routes through services.overdue: an overdue task is
        alerted on exactly once, and a second POST is a no-op."""
        self.client.login(username="co_admin", password="pass12345")
        volunteer = Users.objects.create_user(
            username="co_vol", email="co_vol@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        client_user = Users.objects.create_user(
            username="co_client", email="co_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        task = HelpRequest.objects.create(
            client=client_user, volunteer=volunteer, help_type="grocery", description="x",
            address="a", phone="p", region="dushanbe", status="active",
            accepted_at=timezone.now() - timezone.timedelta(hours=4),
        )
        mail.outbox.clear()

        self.client.post(reverse("check_overdue"))
        task.refresh_from_db()
        self.assertTrue(task.alarm_sent)
        self.assertEqual(len(mail.outbox), 1)

        mail.outbox.clear()
        self.client.post(reverse("check_overdue"))
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class OverdueSweepServiceTests(TestCase):
    """myapp.services.overdue — the single overdue-detection + alerting path
    shared by check_overdue_view and the check_overdue_tasks command."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(
            username="ov_admin", email="ov_admin@example.com", password="pass12345"
        )
        self.curator = Users.objects.create_user(
            username="ov_curator", email="ov_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="ov_vol", email="ov_vol@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        self.client_user = Users.objects.create_user(
            username="ov_client", email="ov_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        mail.outbox.clear()

    def _task(self, *, status="active", hours_ago=4, alarm_sent=False, accepted=True):
        return HelpRequest.objects.create(
            client=self.client_user,
            volunteer=self.volunteer if accepted else None,
            help_type="grocery", description="x", address="a", phone="p", region="dushanbe",
            status=status, alarm_sent=alarm_sent,
            accepted_at=timezone.now() - timezone.timedelta(hours=hours_ago) if accepted else None,
        )

    def test_detects_active_task_past_threshold(self):
        task = self._task(hours_ago=4)
        self.assertEqual(list(overdue.find_overdue_tasks()), [task])

    def test_sweep_alerts_staff_once_and_sets_alarm_sent(self):
        task = self._task(hours_ago=4)
        alerted = overdue.sweep_overdue_tasks()
        self.assertEqual(alerted, [task])
        task.refresh_from_db()
        self.assertTrue(task.alarm_sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, "Просроченный запрос")
        self.assertIn(str(task.id), mail.outbox[0].body)
        self.assertCountEqual(mail.outbox[0].to, [self.admin.email, self.curator.email])

    def test_second_sweep_is_a_noop(self):
        self._task(hours_ago=4)
        overdue.sweep_overdue_tasks()
        mail.outbox.clear()
        self.assertEqual(overdue.sweep_overdue_tasks(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_task_within_threshold_is_ignored(self):
        self._task(hours_ago=2)
        self.assertEqual(overdue.sweep_overdue_tasks(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_completed_and_cancelled_tasks_are_ignored(self):
        self._task(status="completed", hours_ago=9)
        self._task(status="cancelled", hours_ago=9)
        self.assertEqual(list(overdue.find_overdue_tasks()), [])
        self.assertEqual(overdue.sweep_overdue_tasks(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_already_alarmed_task_is_ignored(self):
        self._task(hours_ago=4, alarm_sent=True)
        self.assertEqual(overdue.sweep_overdue_tasks(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_pending_task_is_never_overdue(self):
        self._task(status="pending", accepted=False)
        self.assertEqual(overdue.sweep_overdue_tasks(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_zero_recipients_does_not_permanently_mark_alerted(self):
        """Stage 6 audit MEDIUM finding: alarm_sent used to be committed before
        notify_users() ran, so a delivery failure (here: no active curator/admin)
        permanently and silently lost the alert. The fix must leave alarm_sent
        False so a later run retries once a recipient exists again."""
        Users.objects.filter(pk__in=[self.admin.pk, self.curator.pk]).update(is_active=False)
        task = self._task(hours_ago=4)

        with self.assertLogs("myapp.services.overdue", level="ERROR"):
            alerted = overdue.sweep_overdue_tasks()
        self.assertEqual(alerted, [])
        task.refresh_from_db()
        self.assertFalse(task.alarm_sent)
        self.assertEqual(len(mail.outbox), 0)

        # Idempotency preserved: still a no-op while nobody can be notified.
        self.assertEqual(overdue.sweep_overdue_tasks(), [])
        task.refresh_from_db()
        self.assertFalse(task.alarm_sent)

        # Once a recipient is active again, the next run successfully retries.
        Users.objects.filter(pk=self.curator.pk).update(is_active=True)
        retried = overdue.sweep_overdue_tasks()
        self.assertEqual(retried, [task])
        task.refresh_from_db()
        self.assertTrue(task.alarm_sent)
        self.assertEqual(len(mail.outbox), 1)

        # And it's idempotent again from here on.
        mail.outbox.clear()
        self.assertEqual(overdue.sweep_overdue_tasks(), [])
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CheckOverdueTasksCommandTests(TestCase):
    """The check_overdue_tasks management command is a thin wrapper over
    services.overdue and must be safe to run repeatedly (cron)."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(
            username="cmd_admin", email="cmd_admin@example.com", password="pass12345"
        )
        self.volunteer = Users.objects.create_user(
            username="cmd_vol", email="cmd_vol@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        self.client_user = Users.objects.create_user(
            username="cmd_client", email="cmd_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery",
            description="x", address="a", phone="p", region="dushanbe", status="active",
            accepted_at=timezone.now() - timezone.timedelta(hours=4),
        )
        mail.outbox.clear()

    def _run(self, *args):
        out = io.StringIO()
        call_command("check_overdue_tasks", *args, stdout=out)
        return out.getvalue()

    def test_command_alerts_overdue_task(self):
        output = self._run()
        self.task.refresh_from_db()
        self.assertTrue(self.task.alarm_sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"#{self.task.id}", output)
        self.assertIn("1 task(s) newly alerted", output)

    def test_repeated_run_does_not_duplicate_notifications(self):
        self._run()
        mail.outbox.clear()
        output = self._run()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("0 task(s) newly alerted", output)

    def test_dry_run_reports_without_side_effects(self):
        output = self._run("--dry-run")
        self.task.refresh_from_db()
        self.assertFalse(self.task.alarm_sent)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("dry-run", output)
        self.assertIn(f"#{self.task.id}", output)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class StalePendingSweepServiceTests(TestCase):
    """myapp.services.stale — the pending-side SLA sweep, the mirror of
    services.overdue. Alerts staff once per stuck request, idempotently."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(
            username="st_admin", email="st_admin@example.com", password="pass12345"
        )
        self.curator = Users.objects.create_user(
            username="st_curator", email="st_curator@example.com", password="pass12345", is_curator=True
        )
        self.client_user = Users.objects.create_user(
            username="st_client", email="st_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        mail.outbox.clear()

    def _request(self, *, status="pending", age_hours=49, stale_alert_sent=False):
        task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status=status, stale_alert_sent=stale_alert_sent,
        )
        HelpRequest.objects.filter(pk=task.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=age_hours)
        )
        task.refresh_from_db()
        return task

    def test_detects_pending_request_past_threshold(self):
        task = self._request(age_hours=49)
        self.assertEqual(list(stale.find_stale_pending()), [task])

    def test_sweep_alerts_staff_once_and_sets_flag(self):
        task = self._request(age_hours=60)
        alerted = stale.sweep_stale_pending()
        self.assertEqual(alerted, [task])
        task.refresh_from_db()
        self.assertTrue(task.stale_alert_sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, stale.STALE_SUBJECT)
        self.assertIn(str(task.id), mail.outbox[0].body)
        self.assertCountEqual(mail.outbox[0].to, [self.admin.email, self.curator.email])

    def test_second_sweep_is_a_noop(self):
        self._request(age_hours=60)
        stale.sweep_stale_pending()
        mail.outbox.clear()
        self.assertEqual(stale.sweep_stale_pending(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_request_within_threshold_is_ignored(self):
        self._request(age_hours=10)
        self.assertEqual(stale.sweep_stale_pending(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_active_and_completed_requests_are_never_stale_pending(self):
        self._request(status="active", age_hours=200)
        self._request(status="completed", age_hours=200)
        self.assertEqual(list(stale.find_stale_pending()), [])
        self.assertEqual(stale.sweep_stale_pending(), [])
        self.assertEqual(len(mail.outbox), 0)

    def test_already_alerted_request_is_ignored_by_sweep_but_still_currently_stale(self):
        task = self._request(age_hours=60, stale_alert_sent=True)
        self.assertEqual(stale.sweep_stale_pending(), [])
        self.assertEqual(len(mail.outbox), 0)
        # currently_stale_pending is the "stuck right now" set — flag-independent.
        self.assertIn(task.id, {t.id for t in stale.currently_stale_pending()})

    def test_accepting_the_request_removes_it_from_the_stale_set(self):
        volunteer = Users.objects.create_user(
            username="st_vol", email="st_vol@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        task = self._request(age_hours=60)
        task.accept(volunteer)
        self.assertEqual(list(stale.find_stale_pending()), [])
        self.assertEqual(list(stale.currently_stale_pending()), [])

    def test_zero_recipients_does_not_permanently_mark_alerted(self):
        """Mirror of the overdue-sweep regression: stale_alert_sent used to be
        committed before notify_users() ran, so a delivery failure (here: no
        active curator/admin) permanently and silently lost the alert."""
        Users.objects.filter(pk__in=[self.admin.pk, self.curator.pk]).update(is_active=False)
        task = self._request(age_hours=60)

        with self.assertLogs("myapp.services.stale", level="ERROR"):
            alerted = stale.sweep_stale_pending()
        self.assertEqual(alerted, [])
        task.refresh_from_db()
        self.assertFalse(task.stale_alert_sent)
        self.assertEqual(len(mail.outbox), 0)

        # Idempotency preserved: still a no-op while nobody can be notified.
        self.assertEqual(stale.sweep_stale_pending(), [])
        task.refresh_from_db()
        self.assertFalse(task.stale_alert_sent)

        # Once a recipient is active again, the next run successfully retries.
        Users.objects.filter(pk=self.curator.pk).update(is_active=True)
        retried = stale.sweep_stale_pending()
        self.assertEqual(retried, [task])
        task.refresh_from_db()
        self.assertTrue(task.stale_alert_sent)
        self.assertEqual(len(mail.outbox), 1)

        # And it's idempotent again from here on.
        mail.outbox.clear()
        self.assertEqual(stale.sweep_stale_pending(), [])
        self.assertEqual(len(mail.outbox), 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CheckStaleRequestsCommandTests(TestCase):
    """check_stale_requests is a thin wrapper over services.stale and must be
    safe to run repeatedly (cron)."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(
            username="scmd_admin", email="scmd_admin@example.com", password="pass12345"
        )
        self.client_user = Users.objects.create_user(
            username="scmd_client", email="scmd_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending",
        )
        HelpRequest.objects.filter(pk=self.task.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=60)
        )
        mail.outbox.clear()

    def _run(self, *args):
        out = io.StringIO()
        call_command("check_stale_requests", *args, stdout=out)
        return out.getvalue()

    def test_command_alerts_stale_request(self):
        output = self._run()
        self.task.refresh_from_db()
        self.assertTrue(self.task.stale_alert_sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(f"#{self.task.id}", output)
        self.assertIn("1 request(s) newly alerted", output)

    def test_repeated_run_does_not_duplicate_notifications(self):
        self._run()
        mail.outbox.clear()
        output = self._run()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("0 request(s) newly alerted", output)

    def test_dry_run_reports_without_side_effects(self):
        output = self._run("--dry-run")
        self.task.refresh_from_db()
        self.assertFalse(self.task.stale_alert_sent)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("dry-run", output)
        self.assertIn(f"#{self.task.id}", output)


class HelpRequestFormCoordinateValidationTests(TestCase):
    """LOW: latitude/longitude accepted any DecimalField(max_digits=9) value
    (up to ~1000), not just real WGS84 coordinates."""

    def _base_data(self, **overrides):
        data = {"help_type": "grocery", "description": "x", "address": "a", "phone": "p"}
        data.update(overrides)
        return data

    def test_valid_coordinates_accepted(self):
        form = HelpRequestForm(data=self._base_data(latitude="38.56", longitude="68.78"))
        self.assertTrue(form.is_valid(), form.errors)

    def test_out_of_range_latitude_rejected(self):
        form = HelpRequestForm(data=self._base_data(latitude="999.5", longitude="68.78"))
        self.assertFalse(form.is_valid())

    def test_missing_longitude_with_latitude_present_is_rejected(self):
        form = HelpRequestForm(data=self._base_data(latitude="38.56"))
        self.assertFalse(form.is_valid())

    def test_no_coordinates_at_all_is_still_valid(self):
        form = HelpRequestForm(data=self._base_data())
        self.assertTrue(form.is_valid(), form.errors)


def _small_image_file(name="photo.png"):
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), color="blue").save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def _oversized_image_file(name="big.png"):
    # A real, Pillow-parseable PNG with junk appended past the IEND chunk:
    # Image.open().verify() only reads up to IEND, so this is cheap to build
    # but still reports a `.size` comfortably over the 5MB cap.
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), color="blue").save(buffer, format="PNG")
    payload = buffer.getvalue() + b"0" * (6 * 1024 * 1024)
    return SimpleUploadedFile(name, payload, content_type="image/png")


class PhotoReportSecurityTests(TestCase):
    """LOW: any authenticated user (including clients) could publish a photo
    report, and image uploads (avatars and photo reports) had no size cap."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(username="pr_admin", email="pr_admin@example.com", password="pass12345")
        self.curator = Users.objects.create_user(
            username="pr_curator", email="pr_curator@example.com", password="pass12345", is_curator=True
        )
        self.volunteer = Users.objects.create_user(
            username="pr_vol", email="pr_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.client_user = Users.objects.create_user(
            username="pr_client", email="pr_client@example.com", password="pass12345", is_client=True, region="dushanbe"
        )

    def test_client_cannot_create_photo_report(self):
        self.client.login(username="pr_client", password="pass12345")
        response = self.client.post(reverse("photo_reports"), {"title": "T", "description": "d", "image": _small_image_file()})
        self.assertRedirects(response, reverse("photo_reports"))
        self.assertEqual(PhotoReport.objects.count(), 0)

    def test_client_does_not_see_create_form(self):
        self.client.login(username="pr_client", password="pass12345")
        response = self.client.get(reverse("photo_reports"))
        self.assertNotContains(response, 'enctype="multipart/form-data"')

    def test_volunteer_can_create_photo_report(self):
        self.client.login(username="pr_vol", password="pass12345")
        response = self.client.post(reverse("photo_reports"), {"title": "T", "description": "d", "image": _small_image_file()})
        self.assertRedirects(response, reverse("photo_reports"))
        self.assertEqual(PhotoReport.objects.count(), 1)

    def test_curator_and_admin_can_create_photo_report(self):
        for username in ("pr_curator", "pr_admin"):
            self.client.login(username=username, password="pass12345")
            response = self.client.post(
                reverse("photo_reports"), {"title": f"T-{username}", "description": "d", "image": _small_image_file(f"{username}.png")}
            )
            self.assertRedirects(response, reverse("photo_reports"))
            self.client.logout()
        self.assertEqual(PhotoReport.objects.count(), 2)

    def test_oversized_image_is_rejected_by_form_validation(self):
        form = PhotoReportForm(data={"title": "T", "description": "d"}, files={"image": _oversized_image_file()})
        self.assertFalse(form.is_valid())
        self.assertIn("image", form.errors)

    def test_oversized_avatar_is_rejected_by_form_validation(self):
        from accounts.forms import ProfileForm

        form = ProfileForm(data={"availability_status": "available"}, files={"image": _oversized_image_file("avatar.png")})
        self.assertFalse(form.is_valid())
        self.assertIn("image", form.errors)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TelegramLinkRedemptionTests(TestCase):
    """HIGH #1: the old bot `/link <username>` bound the sender's Telegram chat
    to ANY account given only a (publicly guessable) username — no auth, no
    confirmation. Linking now requires a one-time, hashed, short-lived,
    per-chat-rate-limited code that proves control of BOTH the app account
    (minted while logged in) and the chat (redeemed from it)."""

    def setUp(self):
        cache.clear()
        self.owner = Users.objects.create_user(
            username="tg_owner", email="tg_owner@example.com", password="pass12345", is_client=True,
        )
        self.attacker = Users.objects.create_user(
            username="tg_attacker", email="tg_attacker@example.com", password="pass12345", is_client=True,
        )

    def tearDown(self):
        cache.clear()

    def test_valid_owner_code_links_their_own_chat(self):
        code = self.owner.generate_telegram_link_token()
        outcome = redeem_link_code(555001, code)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.code, "linked")
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.telegram_id, 555001)
        # token consumed
        self.assertIsNone(self.owner.telegram_link_token)

    def test_code_is_hashed_at_rest(self):
        code = self.owner.generate_telegram_link_token()
        self.owner.refresh_from_db()
        self.assertNotEqual(self.owner.telegram_link_token, code)
        self.assertEqual(self.owner.telegram_link_token, hash_token(code))

    def test_attacker_cannot_link_a_victim_account_without_its_code(self):
        # Victim has a live linking code; the attacker never sees it and guesses.
        self.owner.generate_telegram_link_token()
        outcome = redeem_link_code(999666, "definitely-not-the-real-code")
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "invalid")
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.telegram_id)

    def test_there_is_no_username_binding_path_at_all(self):
        # The username itself is worthless to the redemption function now.
        outcome = redeem_link_code(999667, self.owner.username)
        self.assertFalse(outcome.ok)
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.telegram_id)

    def test_expired_code_is_rejected(self):
        code = self.owner.generate_telegram_link_token()
        self.owner.telegram_link_token_created_at = timezone.now() - timezone.timedelta(minutes=11)
        self.owner.save(update_fields=["telegram_link_token_created_at"])
        outcome = redeem_link_code(555002, code)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "invalid")
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.telegram_id)

    def test_code_is_single_use(self):
        code = self.owner.generate_telegram_link_token()
        self.assertTrue(redeem_link_code(555003, code).ok)
        # replay from the same chat
        self.assertFalse(redeem_link_code(555003, code).ok)
        # replay from a different chat must not move the binding either
        self.assertFalse(redeem_link_code(777003, code).ok)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.telegram_id, 555003)

    def test_regenerating_invalidates_the_previous_code(self):
        old_code = self.owner.generate_telegram_link_token()
        new_code = self.owner.generate_telegram_link_token()
        self.assertFalse(redeem_link_code(555004, old_code).ok)
        self.assertTrue(redeem_link_code(555004, new_code).ok)

    def test_brute_force_is_throttled_per_chat(self):
        for i in range(LINK_ATTEMPT_LIMIT):
            self.assertFalse(redeem_link_code(444555, f"wrong-{i}").ok)
        # Even the correct code is now refused until the window clears — proving
        # the limiter runs before the token lookup.
        code = self.owner.generate_telegram_link_token()
        outcome = redeem_link_code(444555, code)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "throttled")
        self.owner.refresh_from_db()
        self.assertIsNone(self.owner.telegram_id)

    def test_throttle_is_scoped_per_chat_not_global(self):
        for i in range(LINK_ATTEMPT_LIMIT):
            redeem_link_code(11111, f"wrong-{i}")
        code = self.owner.generate_telegram_link_token()
        self.assertTrue(redeem_link_code(22222, code).ok)

    def test_successful_link_clears_the_failed_attempt_counter(self):
        redeem_link_code(333444, "wrong-once")
        code = self.owner.generate_telegram_link_token()
        self.assertTrue(redeem_link_code(333444, code).ok)
        # counter reset: a fresh burst is needed to trip the limit again
        self.assertEqual(cache.get("telegram_link_attempts:chat:333444"), None)

    def test_existing_telegram_binding_cannot_be_hijacked(self):
        # owner is already linked to chat 6000
        self.owner.telegram_id = 6000
        self.owner.save(update_fields=["telegram_id"])
        # attacker, logged in as themselves, mints a code and sends it from 6000
        code = self.attacker.generate_telegram_link_token()
        outcome = redeem_link_code(6000, code)
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.code, "chat_in_use")
        self.owner.refresh_from_db()
        self.attacker.refresh_from_db()
        self.assertEqual(self.owner.telegram_id, 6000)
        self.assertIsNone(self.attacker.telegram_id)

    def test_relinking_same_account_to_a_new_chat_reports_the_old_chat(self):
        self.owner.telegram_id = 100
        self.owner.save(update_fields=["telegram_id"])
        code = self.owner.generate_telegram_link_token()
        outcome = redeem_link_code(200, code)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.previous_chat_id, 100)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.telegram_id, 200)

    @override_settings(TELEGRAM_BOT_TOKEN="test-bot-token")
    def test_notifications_reach_the_linked_chat_after_linking(self):
        code = self.owner.generate_telegram_link_token()
        redeem_link_code(314159, code)
        self.owner.refresh_from_db()
        with mock.patch("myapp.notifications.requests.post") as posted:
            posted.return_value.raise_for_status.return_value = None
            notify_users([self.owner], "Subject", "Body")
        posted.assert_called_once()
        self.assertEqual(posted.call_args.kwargs["json"]["chat_id"], 314159)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TelegramBotCommandTests(TestCase):
    """The bot command dispatch itself: `/link <username>` is gone; `/link
    <code>` drives the verified redemption; other commands are unchanged."""

    def setUp(self):
        cache.clear()
        from myapp.management.commands.run_telegram_bot import Command

        self.user = Users.objects.create_user(
            username="bot_user", email="bot_user@example.com", password="pass12345", is_client=True,
        )
        self.cmd = Command()
        self.sent = []
        self.cmd._send = lambda chat_id, text: self.sent.append((chat_id, text))

    def tearDown(self):
        cache.clear()

    def test_link_by_username_no_longer_binds_anything(self):
        self.cmd._handle_message(4242, "/link bot_user")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_id)

    def test_link_with_a_valid_code_binds_the_sender_chat(self):
        code = self.user.generate_telegram_link_token()
        self.cmd._handle_message(4242, f"/link {code}")
        self.user.refresh_from_db()
        self.assertEqual(self.user.telegram_id, 4242)

    def test_link_without_an_argument_shows_usage(self):
        self.cmd._handle_message(4242, "/link")
        self.assertIn("Использование", self.sent[-1][1])
        self.user.refresh_from_db()
        self.assertIsNone(self.user.telegram_id)

    def test_id_command_still_returns_the_chat_id(self):
        self.cmd._handle_message(4242, "/id")
        self.assertIn("4242", self.sent[-1][1])


# ============================================================================
# Emergency / SOS (Stage 4)
# ============================================================================

def _emergency_users():
    admin = Users.objects.create_superuser(username="e_admin", email="e_admin@example.com", password="pass12345")
    curator = Users.objects.create_user(username="e_curator", email="e_curator@example.com", password="pass12345", is_curator=True)
    volunteer = Users.objects.create_user(username="e_vol", email="e_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe")
    other_vol = Users.objects.create_user(username="e_vol2", email="e_vol2@example.com", password="pass12345", is_volunteer=True, region="dushanbe")
    client_user = Users.objects.create_user(username="e_client", email="e_client@example.com", password="pass12345", is_client=True, region="dushanbe")
    return admin, curator, volunteer, other_vol, client_user


def _active_task(client_user, volunteer, **extra):
    defaults = dict(
        client=client_user, volunteer=volunteer, help_type="grocery", description="x",
        address="ул. Рудаки 1", phone="+992900000000", region="dushanbe", status="active",
        accepted_at=timezone.now() - timezone.timedelta(minutes=20),
    )
    defaults.update(extra)
    return HelpRequest.objects.create(**defaults)


class EmergencyModelTests(TestCase):
    def setUp(self):
        _, _, self.volunteer, _, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)
        self.report = EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)

    def test_defaults(self):
        self.assertEqual(self.report.status, "open")
        self.assertTrue(self.report.is_open)
        self.assertFalse(self.report.has_location)
        self.assertEqual(self.report.client, self.client_user)

    def test_valid_transition_chain(self):
        self.report.acknowledge(self.volunteer)  # actor identity is enforced in the view, not the model
        self.assertEqual(self.report.status, "acknowledged")
        self.assertIsNotNone(self.report.acknowledged_at)
        self.report.resolve(self.volunteer, note="done")
        self.assertEqual(self.report.status, "resolved")
        self.assertEqual(self.report.resolution_note, "done")
        self.assertFalse(self.report.is_open)

    def test_open_can_go_straight_to_resolved_or_cancelled(self):
        self.assertTrue(self.report.can_transition_to("resolved"))
        self.assertTrue(self.report.can_transition_to("cancelled"))

    def test_invalid_transitions_raise(self):
        self.report.resolve(self.volunteer)
        with self.assertRaises(ValueError):
            self.report.acknowledge(self.volunteer)
        with self.assertRaises(ValueError):
            self.report.cancel(self.volunteer)

    def test_cannot_reopen_or_go_backwards(self):
        self.report.acknowledge(self.volunteer)
        self.assertFalse(self.report.can_transition_to("open"))


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmergencyServiceTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, _, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer, latitude="38.560000", longitude="68.780000")
        mail.outbox.clear()

    def test_report_creates_and_links(self):
        report, created = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task, reason="  help  ")
        self.assertTrue(created)
        self.assertEqual(report.help_request, self.task)
        self.assertEqual(report.volunteer, self.volunteer)
        self.assertEqual(report.reason, "help")
        self.assertEqual(report.region, "dushanbe")

    def test_dedup_returns_existing_open_report(self):
        first, c1 = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        second, c2 = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task, reason="again")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(EmergencyReport.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)  # only the first press alerted staff

    def test_location_explicit_coords(self):
        report, _ = emergency.report_emergency(
            volunteer=self.volunteer, help_request=self.task, latitude="39.0", longitude="69.0"
        )
        self.assertEqual((float(report.latitude), float(report.longitude)), (39.0, 69.0))

    def test_location_falls_back_to_task(self):
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        self.assertEqual((float(report.latitude), float(report.longitude)), (38.56, 68.78))

    def test_location_falls_back_to_profile_then_none(self):
        task = _active_task(self.client_user, self.volunteer)  # no coords
        self.volunteer.profile.latitude = "40.10"
        self.volunteer.profile.longitude = "70.20"
        self.volunteer.profile.save()
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=task)
        self.assertEqual((float(report.latitude), float(report.longitude)), (40.10, 70.20))

    def test_location_invalid_coords_ignored(self):
        task = _active_task(self.client_user, self.volunteer)
        report, _ = emergency.report_emergency(
            volunteer=self.volunteer, help_request=task, latitude="999", longitude="1"
        )
        self.assertIsNone(report.latitude)

    def test_notify_staff_is_idempotent(self):
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        self.assertEqual(len(mail.outbox), 1)
        self.assertFalse(emergency.notify_staff(report))  # already claimed
        self.assertEqual(len(mail.outbox), 1)

    def test_staff_notification_recipients_and_body(self):
        emergency.report_emergency(volunteer=self.volunteer, help_request=self.task, reason="fell")
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertCountEqual(msg.to, [self.admin.email, self.curator.email])
        self.assertIn(self.volunteer.username, msg.body)
        self.assertIn(str(self.task.id), msg.body)
        self.assertIn(self.client_user.username, msg.body)
        self.assertIn("fell", msg.body)

    def test_transitions_notify_reporter(self):
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        mail.outbox.clear()
        emergency.acknowledge(report, actor=self.curator)
        emergency.resolve(report, actor=self.curator, note="ok")
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[0].to, [self.volunteer.email])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmergencyReportViewTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.other_vol, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)
        mail.outbox.clear()
        cache.clear()

    def _url(self, task=None):
        return reverse("emergency_report", args=[(task or self.task).pk])

    def test_volunteer_creates_emergency(self):
        self.client.login(username="e_vol", password="pass12345")
        resp = self.client.post(self._url(), {"reason": "unsafe"})
        report = EmergencyReport.objects.get()
        self.assertRedirects(resp, reverse("emergency_detail", args=[report.pk]))
        self.assertEqual(report.volunteer, self.volunteer)
        self.assertEqual(report.help_request, self.task)
        self.assertEqual(report.reason, "unsafe")
        self.assertEqual(report.status, "open")
        self.assertEqual(len(mail.outbox), 1)

    def test_get_is_rejected(self):
        self.client.login(username="e_vol", password="pass12345")
        self.assertEqual(self.client.get(self._url()).status_code, 405)
        self.assertEqual(EmergencyReport.objects.count(), 0)

    def test_csrf_required(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="e_vol", password="pass12345")
        resp = csrf_client.post(self._url(), {"reason": "x"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(EmergencyReport.objects.count(), 0)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_client_cannot_create(self):
        self.client.login(username="e_client", password="pass12345")
        resp = self.client.post(self._url())
        self.assertEqual(EmergencyReport.objects.count(), 0)
        self.assertRedirects(resp, reverse("profile"))

    def test_volunteer_cannot_report_on_another_volunteers_task(self):
        self.client.login(username="e_vol2", password="pass12345")
        resp = self.client.post(self._url())
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(EmergencyReport.objects.count(), 0)

    def test_cannot_report_on_non_active_task(self):
        pending = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a",
            phone="p", region="dushanbe", status="pending",
        )
        self.client.login(username="e_vol", password="pass12345")
        self.assertEqual(self.client.post(reverse("emergency_report", args=[pending.pk])).status_code, 404)

    def test_duplicate_press_does_not_spam(self):
        self.client.login(username="e_vol", password="pass12345")
        self.client.post(self._url(), {"reason": "one"})
        cache.clear()  # bypass the short cooldown lock to hit the DB-level dedup
        self.client.post(self._url(), {"reason": "two"})
        self.assertEqual(EmergencyReport.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_rapid_double_submit_bounces_to_existing_report(self):
        self.client.login(username="e_vol", password="pass12345")
        self.client.post(self._url())
        report = EmergencyReport.objects.get()
        resp = self.client.post(self._url())  # cooldown still held
        self.assertRedirects(resp, reverse("emergency_detail", args=[report.pk]))
        self.assertEqual(EmergencyReport.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmergencyUpdateViewTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.other_vol, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)
        self.report = EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        mail.outbox.clear()

    def _post(self, username, action, note=""):
        self.client.login(username=username, password="pass12345")
        return self.client.post(reverse("emergency_update", args=[self.report.pk]), {"action": action, "note": note})

    def test_curator_can_acknowledge(self):
        resp = self._post("e_curator", "acknowledge")
        self.report.refresh_from_db()
        self.assertRedirects(resp, reverse("emergency_detail", args=[self.report.pk]))
        self.assertEqual(self.report.status, "acknowledged")
        self.assertEqual(self.report.acknowledged_by, self.curator)

    def test_curator_can_resolve(self):
        self._post("e_curator", "resolve", note="handled")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, "resolved")
        self.assertEqual(self.report.resolution_note, "handled")

    def test_admin_can_cancel(self):
        self._post("e_admin", "cancel")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, "cancelled")
        self.assertEqual(self.report.cancelled_by, self.admin)

    def test_volunteer_cannot_acknowledge_or_resolve(self):
        for actor in ("e_vol", "e_vol2"):
            resp = self._post(actor, "acknowledge")
            self.report.refresh_from_db()
            self.assertEqual(self.report.status, "open")
            self.assertRedirects(resp, reverse("profile"))

    def test_client_cannot_update(self):
        self._post("e_client", "resolve")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, "open")

    def test_get_cannot_mutate(self):
        self.client.login(username="e_curator", password="pass12345")
        resp = self.client.get(reverse("emergency_update", args=[self.report.pk]))
        self.assertEqual(resp.status_code, 405)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, "open")

    def test_invalid_transition_is_handled_gracefully(self):
        self.report.resolve(self.admin)
        resp = self._post("e_curator", "acknowledge")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, "resolved")
        self.assertEqual(resp.status_code, 302)  # redirect with a warning message, not a 500

    def test_unknown_action_is_rejected(self):
        resp = self._post("e_curator", "explode")
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, "open")
        self.assertEqual(resp.status_code, 302)


class EmergencyDetailAccessTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.other_vol, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)
        self.report = EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)

    def _get(self, username):
        self.client.login(username=username, password="pass12345")
        return self.client.get(reverse("emergency_detail", args=[self.report.pk]))

    def test_reporter_sees_own_without_action_controls(self):
        resp = self._get("e_vol")
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse("emergency_update", args=[self.report.pk]))

    def test_other_volunteer_denied(self):
        resp = self._get("e_vol2")
        self.assertRedirects(resp, reverse("profile"))

    def test_client_denied(self):
        self.assertRedirects(self._get("e_client"), reverse("profile"))

    def test_staff_see_action_controls(self):
        resp = self._get("e_curator")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("emergency_update", args=[self.report.pk]))

    def test_anonymous_redirected(self):
        resp = self.client.get(reverse("emergency_detail", args=[self.report.pk]))
        self.assertEqual(resp.status_code, 302)


class EmergencyCrmListTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.other_vol, self.client_user = _emergency_users()
        self.t1 = _active_task(self.client_user, self.volunteer, region="dushanbe")
        self.t2 = _active_task(self.client_user, self.other_vol, region="sogd")
        self.open_report = EmergencyReport.objects.create(help_request=self.t1, volunteer=self.volunteer, region="dushanbe", reason="door locked")
        self.resolved_report = EmergencyReport.objects.create(
            help_request=self.t2, volunteer=self.other_vol, region="sogd", status="resolved"
        )

    def test_requires_staff(self):
        self.client.login(username="e_vol", password="pass12345")
        self.assertRedirects(self.client.get(reverse("emergency_list")), reverse("profile"))
        self.client.login(username="e_client", password="pass12345")
        self.assertRedirects(self.client.get(reverse("emergency_list")), reverse("profile"))

    def test_open_report_appears_for_staff(self):
        self.client.login(username="e_curator", password="pass12345")
        resp = self.client.get(reverse("emergency_list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "#%d" % self.open_report.id)
        self.assertEqual(resp.context["counts"]["open"], 1)
        self.assertEqual(resp.context["counts"]["resolved"], 1)
        self.assertEqual(resp.context["counts"]["total"], 2)

    def test_status_tab_filter(self):
        self.client.login(username="e_admin", password="pass12345")
        ids = [r.id for r in self.client.get(reverse("emergency_list"), {"status": "open"}).context["page_obj"]]
        self.assertEqual(ids, [self.open_report.id])

    def test_region_filter(self):
        self.client.login(username="e_admin", password="pass12345")
        ids = [r.id for r in self.client.get(reverse("emergency_list"), {"region": "sogd"}).context["page_obj"]]
        self.assertEqual(ids, [self.resolved_report.id])

    def test_search_by_volunteer(self):
        self.client.login(username="e_admin", password="pass12345")
        ids = [r.id for r in self.client.get(reverse("emergency_list"), {"q": "e_vol2"}).context["page_obj"]]
        self.assertEqual(ids, [self.resolved_report.id])

    def test_pagination(self):
        for _ in range(25):
            EmergencyReport.objects.create(help_request=self.t1, volunteer=self.volunteer, status="cancelled")
        self.client.login(username="e_admin", password="pass12345")
        page1 = self.client.get(reverse("emergency_list"))
        self.assertEqual(page1.context["page_obj"].paginator.num_pages, 2)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmergencyDashboardTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.other_vol, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)

    def test_dashboard_stats_counts(self):
        # Distinct (volunteer, task) pairs — the partial unique constraint allows
        # only one active report per pair.
        self.assertEqual(analytics.dashboard_stats()["emergencies_open"], 0)
        task2 = _active_task(self.client_user, self.other_vol)
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        EmergencyReport.objects.create(help_request=task2, volunteer=self.other_vol, status="acknowledged")
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer, status="resolved")
        stats = analytics.dashboard_stats()
        self.assertEqual(stats["emergencies_open"], 1)
        self.assertEqual(stats["emergencies_acknowledged"], 1)
        self.assertEqual(stats["emergencies_active"], 2)

    def test_admin_panel_shows_open_emergencies(self):
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer, reason="urgent")
        self.client.login(username="e_curator", password="pass12345")
        resp = self.client.get(reverse("admin_panel"))
        self.assertEqual(len(resp.context["open_emergencies"]), 1)
        self.assertContains(resp, reverse("emergency_list"))

    def test_volunteer_dashboard_has_sos_button(self):
        self.client.login(username="e_vol", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertContains(resp, reverse("emergency_report", args=[self.task.pk]))

    def test_volunteer_dashboard_shows_notice_when_report_open(self):
        report = EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        self.client.login(username="e_vol", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertNotContains(resp, reverse("emergency_report", args=[self.task.pk]))
        self.assertContains(resp, reverse("emergency_detail", args=[report.pk]))

    def test_client_dashboard_has_no_sos_button(self):
        self.client.login(username="e_client", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertNotContains(resp, "emergency/report/")

    def test_staff_dashboard_shows_open_count(self):
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        self.client.login(username="e_curator", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertEqual(resp.context["open_emergency_count"], 1)

    def test_task_detail_sos_button_for_assigned_volunteer_only(self):
        self.client.login(username="e_vol", password="pass12345")
        resp = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertContains(resp, reverse("emergency_report", args=[self.task.pk]))
        self.client.login(username="e_curator", password="pass12345")
        resp = self.client.get(reverse("task_detail", args=[self.task.pk]))
        self.assertNotContains(resp, reverse("emergency_report", args=[self.task.pk]))


class EmergencyMapDataTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.other_vol, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer, latitude="38.56", longitude="68.78")
        self.open_located = EmergencyReport.objects.create(
            help_request=self.task, volunteer=self.volunteer, latitude="38.57", longitude="68.79",
        )
        self.open_no_loc = EmergencyReport.objects.create(help_request=self.task, volunteer=self.other_vol)
        self.resolved = EmergencyReport.objects.create(
            help_request=self.task, volunteer=self.volunteer, status="resolved",
            latitude="38.58", longitude="68.80",
        )

    def _points(self, username):
        self.client.login(username=username, password="pass12345")
        return self.client.get(reverse("map_data")).json()["points"]

    def test_staff_map_data_includes_open_located_emergency(self):
        emg = [p for p in self._points("e_curator") if p["kind"] == "emergency"]
        self.assertEqual(len(emg), 1)
        self.assertEqual(emg[0]["id"], self.open_located.id)
        self.assertEqual(emg[0]["status"], "open")
        self.assertEqual(emg[0]["url"], reverse("emergency_detail", args=[self.open_located.id]))

    def test_resolved_and_unlocated_excluded(self):
        emg_ids = [p["id"] for p in self._points("e_admin") if p["kind"] == "emergency"]
        self.assertNotIn(self.resolved.id, emg_ids)
        self.assertNotIn(self.open_no_loc.id, emg_ids)

    def test_volunteer_map_data_has_no_emergency_points(self):
        self.assertFalse(any(p["kind"] == "emergency" for p in self._points("e_vol")))

    def test_client_map_data_has_no_emergency_points(self):
        self.assertFalse(any(p["kind"] == "emergency" for p in self._points("e_client")))


class EmergencyWithoutCoordinatesTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, _, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)  # no coords, no profile location

    def test_report_works_without_any_location(self):
        report, created = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        self.assertTrue(created)
        self.assertIsNone(report.latitude)
        self.assertIsNone(report.longitude)
        self.assertFalse(report.has_location)

    def test_detail_page_renders_without_location(self):
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        self.client.login(username="e_curator", password="pass12345")
        resp = self.client.get(reverse("emergency_detail", args=[report.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'id="locationMap"')


# --- Stage 4 hardening: DB-level dedup, transition rejection, notification failure ---

@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmergencyDedupHardeningTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.other_vol, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)
        mail.outbox.clear()

    def test_db_constraint_blocks_second_active_report_same_pair(self):
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)

    def test_db_constraint_covers_acknowledged_state(self):
        r = EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        r.acknowledge(self.curator)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)

    def test_constraint_allows_new_report_after_previous_closed(self):
        r = EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        r.resolve(self.curator)
        # same pair, previous one terminal -> allowed
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        self.assertEqual(EmergencyReport.objects.filter(help_request=self.task, volunteer=self.volunteer).count(), 2)

    def test_service_survives_the_race_and_returns_existing(self):
        """Simulate the check-then-create window losing to a concurrent worker:
        the fast pre-check finds nothing, the INSERT hits the constraint, and the
        service recovers by returning the row that won — one row, one alert."""
        EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer)
        mail.outbox.clear()
        with mock.patch("myapp.services.emergency._existing_active_report", side_effect=[None, EmergencyReport.objects.get()]):
            report, created = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        self.assertFalse(created)
        self.assertEqual(EmergencyReport.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_concurrent_view_posts_create_one_row_one_alert(self):
        self.client.login(username="e_vol", password="pass12345")
        url = reverse("emergency_report", args=[self.task.pk])
        self.client.post(url, {"reason": "a"})
        cache.clear()
        self.client.post(url, {"reason": "b"})
        cache.clear()
        self.client.post(url, {"reason": "c"})
        self.assertEqual(EmergencyReport.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)


class EmergencyInvalidTransitionRejectionTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, _, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)

    def _report(self, status):
        return EmergencyReport.objects.create(help_request=self.task, volunteer=self.volunteer, status=status)

    def test_resolved_cannot_go_to_acknowledged_or_open(self):
        r = self._report("resolved")
        for target in ("acknowledged", "open"):
            self.assertFalse(r.can_transition_to(target))
        with self.assertRaises(ValueError):
            r.acknowledge(self.curator)

    def test_cancelled_cannot_go_to_acknowledged_or_resolved(self):
        r = self._report("cancelled")
        for target in ("acknowledged", "resolved", "open"):
            self.assertFalse(r.can_transition_to(target))
        with self.assertRaises(ValueError):
            r.resolve(self.curator)

    def test_update_view_rejects_illegal_transition_without_500(self):
        r = self._report("resolved")
        self.client.login(username="e_curator", password="pass12345")
        resp = self.client.post(reverse("emergency_update", args=[r.pk]), {"action": "acknowledge"})
        self.assertEqual(resp.status_code, 302)
        r.refresh_from_db()
        self.assertEqual(r.status, "resolved")

    def test_update_view_get_is_405_for_every_action(self):
        r = self._report("open")
        self.client.login(username="e_admin", password="pass12345")
        self.assertEqual(self.client.get(reverse("emergency_update", args=[r.pk])).status_code, 405)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class EmergencyNotificationFailureTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, _, self.client_user = _emergency_users()
        self.task = _active_task(self.client_user, self.volunteer)
        mail.outbox.clear()

    def test_zero_delivery_is_logged_and_report_stays_visible(self):
        with mock.patch("myapp.services.emergency.notify_users", return_value=0) as nu:
            with self.assertLogs("myapp.services.emergency", level="ERROR") as logs:
                report, created = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        nu.assert_called_once()
        self.assertTrue(created)
        report.refresh_from_db()
        self.assertIsNotNone(report.notified_at)          # claimed, so no duplicate spam later
        self.assertEqual(report.status, "open")           # still open
        self.assertIn(report, list(emergency.open_reports()))  # still in the CRM queue
        self.assertTrue(any("0 of" in m for m in logs.output))

    def test_realert_resends_to_staff(self):
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        self.assertEqual(len(mail.outbox), 1)
        self.client.login(username="e_curator", password="pass12345")
        self.client.post(reverse("emergency_update", args=[report.pk]), {"action": "realert"})
        self.assertEqual(len(mail.outbox), 2)

    def test_realert_is_staff_only(self):
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        mail.outbox.clear()
        self.client.login(username="e_vol", password="pass12345")
        resp = self.client.post(reverse("emergency_update", args=[report.pk]), {"action": "realert"})
        self.assertRedirects(resp, reverse("profile"))
        self.assertEqual(len(mail.outbox), 0)

    def test_realert_noop_on_closed_report(self):
        report, _ = emergency.report_emergency(volunteer=self.volunteer, help_request=self.task)
        report.resolve(self.curator)
        mail.outbox.clear()
        self.client.login(username="e_admin", password="pass12345")
        self.client.post(reverse("emergency_update", args=[report.pk]), {"action": "realert"})
        self.assertEqual(len(mail.outbox), 0)


class EmergencyCoordinateValidationTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, _, self.client_user = _emergency_users()

    def test_out_of_range_and_non_numeric_coords_are_rejected(self):
        task = _active_task(self.client_user, self.volunteer, latitude="38.50", longitude="68.70")
        for bad_lat, bad_lng in [("800", "10"), ("10", "500"), ("abc", "xyz"), ("", "68.7")]:
            report, _ = emergency.report_emergency(
                volunteer=self.volunteer, help_request=_active_task(self.client_user, self.volunteer, latitude="38.50", longitude="68.70"),
                latitude=bad_lat, longitude=bad_lng,
            )
            # never stores the bad value — falls back to the task's own coords
            self.assertEqual((float(report.latitude), float(report.longitude)), (38.50, 68.70))

    def test_valid_coords_from_post_are_stored(self):
        task = _active_task(self.client_user, self.volunteer)
        report, _ = emergency.report_emergency(
            volunteer=self.volunteer, help_request=task, latitude="38.53", longitude="68.77"
        )
        self.assertEqual((float(report.latitude), float(report.longitude)), (38.53, 68.77))


# ============================================================================
# Role-based dashboards (Stage 5)
# ============================================================================

def _dash_users():
    admin = Users.objects.create_superuser(username="d5_admin", email="d5_admin@example.com", password="pass12345")
    curator = Users.objects.create_user(username="d5_curator", email="d5_curator@example.com", password="pass12345", is_curator=True)
    vol_a = Users.objects.create_user(username="d5_vol_a", email="d5_vol_a@example.com", password="pass12345", is_volunteer=True, region="dushanbe")
    vol_b = Users.objects.create_user(username="d5_vol_b", email="d5_vol_b@example.com", password="pass12345", is_volunteer=True, region="dushanbe")
    cli_a = Users.objects.create_user(username="d5_cli_a", email="d5_cli_a@example.com", password="pass12345", is_client=True, region="dushanbe")
    cli_b = Users.objects.create_user(username="d5_cli_b", email="d5_cli_b@example.com", password="pass12345", is_client=True, region="dushanbe")
    return admin, curator, vol_a, vol_b, cli_a, cli_b


def _hr(client_user, **kw):
    data = dict(help_type="grocery", description="desc", address="a", phone="p", region="dushanbe", status="pending")
    data.update(kw)
    return HelpRequest.objects.create(client=client_user, **data)


class RecommendTasksAdapterTests(TestCase):
    """matching.recommend_tasks — the volunteer->task adapter. Reuses the
    task->volunteer scoring primitives, never assigns."""

    def setUp(self):
        _, _, self.vol, _, self.client_user, _ = _dash_users()
        self.vol.profile.latitude = "38.5600"
        self.vol.profile.longitude = "68.7800"
        self.vol.profile.skills = ["grocery"]
        self.vol.profile.save()

    def test_returns_empty_without_volunteer_location(self):
        self.vol.profile.latitude = None
        self.vol.profile.longitude = None
        self.vol.profile.save()
        _hr(self.client_user, latitude="38.56", longitude="68.78")
        self.assertEqual(recommend_tasks(self.vol), [])

    def test_only_pending_tasks_with_coordinates_in_region(self):
        near = _hr(self.client_user, latitude="38.561", longitude="68.781")
        _hr(self.client_user, status="active", latitude="38.56", longitude="68.78")       # not pending
        _hr(self.client_user, latitude=None, longitude=None)                              # no coords
        _hr(self.client_user, region="sogd", latitude="40.0", longitude="69.0")           # other region
        recs = recommend_tasks(self.vol, limit=5)
        self.assertEqual([r["task"].id for r in recs], [near.id])
        self.assertLessEqual(recs[0]["score"], 100)
        self.assertGreaterEqual(recs[0]["score"], 0)
        self.assertIn("reasons", recs[0])

    def test_deterministic_and_capped(self):
        for i in range(6):
            _hr(self.client_user, latitude=f"38.{560+i}", longitude="68.78")
        a = [r["task"].id for r in recommend_tasks(self.vol, limit=3)]
        b = [r["task"].id for r in recommend_tasks(self.vol, limit=3)]
        self.assertEqual(a, b)
        self.assertEqual(len(a), 3)

    def test_skill_match_flag_and_closer_ranks_higher(self):
        close = _hr(self.client_user, help_type="grocery", latitude="38.5601", longitude="68.7801")
        far = _hr(self.client_user, help_type="grocery", latitude="38.90", longitude="69.20")
        recs = recommend_tasks(self.vol, limit=2)
        self.assertEqual(recs[0]["task"].id, close.id)
        self.assertEqual(recs[0]["skill_match"], "match")
        self.assertLess(recs[1]["score"], recs[0]["score"])

    def test_nearby_task_not_hidden_by_backlog_of_far_urgent_tasks(self):
        """Regression: the candidate cap keeps the *nearest* pending tasks. A
        backlog of far-away urgent requests (more than TASK_REC_CANDIDATE_CAP of
        them) must not push a close, acceptable task out of the running."""
        for i in range(TASK_REC_CANDIDATE_CAP + 5):
            _hr(
                self.client_user,
                priority="emergency",
                is_urgent=True,
                latitude=f"39.{600 + i}",   # ~115 km north of the volunteer
                longitude="68.780000",
            )
        near = _hr(self.client_user, latitude="38.560500", longitude="68.780500")  # ~70 m away

        recs = recommend_tasks(self.vol, limit=3)
        ids = [r["task"].id for r in recs]
        self.assertIn(near.id, ids)
        self.assertEqual(ids[0], near.id)

    def test_no_recommendations_while_volunteer_has_active_task(self):
        _hr(self.client_user, latitude="38.560500", longitude="68.780500")  # would otherwise match
        _hr(self.client_user, status="active", volunteer=self.vol,
            latitude="38.560", longitude="68.780", accepted_at=timezone.now())
        self.assertEqual(recommend_tasks(self.vol), [])

    def test_recommendations_returned_without_active_task(self):
        _hr(self.client_user, latitude="38.560500", longitude="68.780500")
        self.assertTrue(recommend_tasks(self.vol))


class CurrentlyOverdueHelperTests(TestCase):
    def setUp(self):
        _, _, self.vol, _, self.client_user, _ = _dash_users()

    def test_includes_overdue_regardless_of_alarm_sent(self):
        alarmed = _hr(self.client_user, status="active", volunteer=self.vol, alarm_sent=True,
                      accepted_at=timezone.now() - timezone.timedelta(hours=5))
        not_alarmed = _hr(self.client_user, status="active", volunteer=self.vol, alarm_sent=False,
                          accepted_at=timezone.now() - timezone.timedelta(hours=4))
        _hr(self.client_user, status="active", volunteer=self.vol,
            accepted_at=timezone.now() - timezone.timedelta(minutes=30))  # not overdue
        ids = {t.id for t in overdue.currently_overdue_tasks()}
        self.assertEqual(ids, {alarmed.id, not_alarmed.id})


class VolunteerDashboardTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()

    def _get(self, user="d5_vol_a"):
        self.client.login(username=user, password="pass12345")
        return self.client.get(reverse("profile"))

    def test_volunteer_gets_volunteer_dashboard(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["dash_role"], "volunteer")

    def test_only_own_tasks_and_stats(self):
        mine = _hr(self.cli_a, status="completed", volunteer=self.vol_a, completed_at=timezone.now())
        theirs = _hr(self.cli_a, status="completed", volunteer=self.vol_b, completed_at=timezone.now())
        resp = self._get()
        self.assertEqual(resp.context["vol_completed"], 1)
        ids = {t.id for t in resp.context["recent_completed"]}
        self.assertIn(mine.id, ids)
        self.assertNotIn(theirs.id, ids)
        self.assertNotContains(resp, "d5_vol_b")

    def test_another_volunteers_active_task_not_exposed(self):
        others = _hr(self.cli_a, status="active", volunteer=self.vol_b,
                     accepted_at=timezone.now(), latitude="38.56", longitude="68.78")
        resp = self._get()
        self.assertIsNone(resp.context["active_task"])
        self.assertNotContains(resp, reverse("task_detail", args=[others.id]))

    def test_own_emergency_scoped(self):
        task = _hr(self.cli_a, status="active", volunteer=self.vol_a, accepted_at=timezone.now())
        other_task = _hr(self.cli_b, status="active", volunteer=self.vol_b, accepted_at=timezone.now())
        mine = EmergencyReport.objects.create(help_request=task, volunteer=self.vol_a)
        EmergencyReport.objects.create(help_request=other_task, volunteer=self.vol_b)
        resp = self._get()
        ids = {r.id for r in resp.context["my_emergencies"]}
        self.assertEqual(ids, {mine.id})

    def test_recommendations_only_when_no_active_task_and_scoped(self):
        self.vol_a.profile.latitude = "38.56"
        self.vol_a.profile.longitude = "68.78"
        self.vol_a.profile.save()
        _hr(self.cli_a, latitude="38.561", longitude="68.781")
        resp = self._get()
        self.assertTrue(len(resp.context["recommended_tasks"]) >= 1)
        # once they have an active task, no recommendations are computed
        _hr(self.cli_a, status="active", volunteer=self.vol_a, accepted_at=timezone.now())
        resp = self._get()
        self.assertEqual(resp.context["recommended_tasks"], [])

    def test_no_crm_aggregates_in_context(self):
        resp = self._get()
        for key in ("stats", "open_emergencies", "overdue_tasks", "unassigned_tasks", "region_breakdown"):
            self.assertNotIn(key, resp.context)


class ClientDashboardTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()

    def _get(self, user="d5_cli_a"):
        self.client.login(username=user, password="pass12345")
        return self.client.get(reverse("profile"))

    def test_client_gets_client_dashboard(self):
        resp = self._get()
        self.assertEqual(resp.context["dash_role"], "client")

    def test_only_own_requests(self):
        mine = _hr(self.cli_a, status="active", volunteer=self.vol_a, accepted_at=timezone.now())
        theirs = _hr(self.cli_b, status="active", volunteer=self.vol_b, accepted_at=timezone.now())
        resp = self._get()
        self.assertEqual(resp.context["client_current"].id, mine.id)
        self.assertNotContains(resp, reverse("task_detail", args=[theirs.id]))
        self.assertEqual(resp.context["client_total"], 1)

    def test_assigned_volunteer_shows_name_not_contact_details(self):
        self.vol_a.profile.full_name = "Ahmad Karimov"
        self.vol_a.profile.save()
        _hr(self.cli_a, status="active", volunteer=self.vol_a, accepted_at=timezone.now())
        resp = self._get()
        self.assertEqual(resp.context["client_current_volunteer"]["name"], "Ahmad Karimov")
        self.assertContains(resp, "Ahmad Karimov")
        self.assertNotContains(resp, self.vol_a.email)
        self.assertNotIn("email", resp.context["client_current_volunteer"])

    def test_no_crm_or_emergency_context(self):
        resp = self._get()
        for key in ("stats", "open_emergency_count", "open_emergencies", "overdue_tasks", "recent_activity"):
            self.assertNotIn(key, resp.context)


class CuratorDashboardTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()

    def _get(self, user="d5_curator"):
        self.client.login(username=user, password="pass12345")
        return self.client.get(reverse("profile"))

    def test_curator_gets_curator_dashboard(self):
        resp = self._get()
        self.assertEqual(resp.context["dash_role"], "curator")
        self.assertIn("stats", resp.context)

    def test_operational_counts_correct(self):
        _hr(self.cli_a)  # unassigned pending
        overdue_task = _hr(self.cli_a, status="active", volunteer=self.vol_a,
                           accepted_at=timezone.now() - timezone.timedelta(hours=4))
        emg_task = _hr(self.cli_b, status="active", volunteer=self.vol_b, accepted_at=timezone.now())
        EmergencyReport.objects.create(help_request=emg_task, volunteer=self.vol_b)
        VolunteerApplication.objects.create(user=self.cli_a, region="dushanbe", status="pending")
        resp = self._get()
        self.assertEqual(resp.context["open_emergency_count"], 1)
        self.assertEqual(resp.context["stats"]["tasks_overdue"], 1)
        self.assertIn(overdue_task.id, {t.id for t in resp.context["overdue_tasks"]})
        self.assertTrue(len(resp.context["unassigned_tasks"]) >= 1)
        self.assertTrue(len(resp.context["pending_applications"]) >= 1)
        self.assertEqual(len(resp.context["open_emergencies"]), 1)

    def test_region_breakdown_present(self):
        _hr(self.cli_a, region="sogd")
        rows = self._get().context["region_breakdown"]
        self.assertTrue(any(r["region"] == "sogd" and r["pending"] == 1 for r in rows))

    def test_stale_pending_surfaced(self):
        stuck = _hr(self.cli_a)
        HelpRequest.objects.filter(pk=stuck.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=60)
        )
        _hr(self.cli_a)  # fresh pending — not stale
        resp = self._get()
        self.assertEqual(resp.context["stats"]["tasks_stale"], 1)
        self.assertEqual({t.id for t in resp.context["stale_pending_tasks"]}, {stuck.id})

    def test_unassigned_tasks_carry_a_matching_suggestion(self):
        self.vol_a.profile.full_name = "Malika Safarova"
        self.vol_a.profile.latitude = "38.5600"
        self.vol_a.profile.longitude = "68.7800"
        self.vol_a.profile.location_updated_at = timezone.now()
        self.vol_a.profile.save()
        task = _hr(self.cli_a, latitude="38.5605", longitude="68.7805")
        resp = self._get()
        row = next(t for t in resp.context["unassigned_tasks"] if t.id == task.id)
        self.assertIsNotNone(row.assignment_suggestion)
        self.assertEqual(row.assignment_suggestion["name"], "Malika Safarova")
        self.assertIn("distance_km", row.assignment_suggestion)
        # read-only: surfacing a suggestion must not assign the task
        task.refresh_from_db()
        self.assertEqual(task.status, "pending")
        self.assertIsNone(task.volunteer)

    def test_suggestion_is_none_when_task_has_no_location(self):
        self.vol_a.profile.latitude = "38.56"
        self.vol_a.profile.longitude = "68.78"
        self.vol_a.profile.save()
        task = _hr(self.cli_a)  # no coordinates
        resp = self._get()
        row = next(t for t in resp.context["unassigned_tasks"] if t.id == task.id)
        self.assertIsNone(row.assignment_suggestion)


class AdminDashboardTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()

    def _get(self):
        self.client.login(username="d5_admin", password="pass12345")
        return self.client.get(reverse("profile"))

    def test_admin_gets_platform_dashboard(self):
        resp = self._get()
        self.assertEqual(resp.context["dash_role"], "admin")
        self.assertIn("stats", resp.context)          # inherits curator payload
        self.assertIn("users_by_role", resp.context)

    def test_users_by_role_counts(self):
        ubr = self._get().context["users_by_role"]
        self.assertEqual(ubr["volunteers"], 2)
        self.assertEqual(ubr["clients"], 2)
        self.assertEqual(ubr["curators"], 1)
        self.assertEqual(ubr["admins"], 1)

    def test_platform_totals(self):
        _hr(self.cli_a, status="completed", completed_at=timezone.now())
        _hr(self.cli_a)
        totals = self._get().context["platform_totals"]
        self.assertEqual(totals["requests_total"], 2)
        self.assertEqual(totals["requests_completed"], 1)


class DashboardSecurityTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("profile"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.url)

    def test_volunteer_dashboard_has_no_platform_or_other_user_data(self):
        _hr(self.cli_b, status="active", volunteer=self.vol_b, accepted_at=timezone.now())
        self.client.login(username="d5_vol_a", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertNotIn("users_by_role", resp.context)
        self.assertNotIn("stats", resp.context)
        self.assertNotIn("stale_pending_tasks", resp.context)
        self.assertNotContains(resp, "d5_cli_b")

    def test_client_cannot_reach_crm_data_via_dashboard(self):
        emg_task = _hr(self.cli_b, status="active", volunteer=self.vol_b, accepted_at=timezone.now())
        EmergencyReport.objects.create(help_request=emg_task, volunteer=self.vol_b)
        self.client.login(username="d5_cli_a", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertNotIn("open_emergencies", resp.context)
        self.assertNotContains(resp, reverse("emergency_list"))

    def test_query_param_tampering_is_ignored(self):
        self.client.login(username="d5_vol_a", password="pass12345")
        r1 = self.client.get(reverse("profile"))
        r2 = self.client.get(reverse("profile") + "?user=1&role=admin&id=999")
        self.assertEqual(r1.context["dash_role"], r2.context["dash_role"])
        self.assertEqual(r2.context["dash_role"], "volunteer")

    def test_dashboard_view_route_redirects_to_profile(self):
        self.client.login(username="d5_vol_a", password="pass12345")
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("profile"))

    def test_for_user_roleless_returns_guest(self):
        roleless = Users.objects.create_user(username="d5_none", email="d5_none@example.com", password="pass12345")
        self.assertEqual(dashboard.for_user(roleless)["dash_role"], "guest")


# ============================================================================
# In-kind donations — material assistance (Stage 6 increment C, reworked Stage 9)
# ============================================================================

def _donation_users():
    admin = Users.objects.create_superuser(username="dn_admin", email="dn_admin@example.com", password="pass12345")
    curator = Users.objects.create_user(username="dn_curator", email="dn_curator@example.com", password="pass12345", is_curator=True)
    volunteer = Users.objects.create_user(username="dn_vol", email="dn_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe")
    client_a = Users.objects.create_user(username="dn_cli_a", email="dn_cli_a@example.com", password="pass12345", is_client=True)
    client_b = Users.objects.create_user(username="dn_cli_b", email="dn_cli_b@example.com", password="pass12345", is_client=True)
    return admin, curator, volunteer, client_a, client_b


class ProductModelTests(TestCase):
    def test_active_by_default(self):
        p = Product.objects.create(name="Плед", category="blankets", unit="шт")
        self.assertTrue(p.is_active)
        self.assertEqual(p.category, "blankets")

    def test_category_defaults_to_other(self):
        self.assertEqual(Product.objects.create(name="x").category, "other")

    def test_ordering_is_by_name(self):
        Product.objects.create(name="Яблоки")
        Product.objects.create(name="Апельсины")
        self.assertEqual([p.name for p in Product.objects.all()], ["Апельсины", "Яблоки"])


class DonationModelTests(TestCase):
    def setUp(self):
        self.admin, _, self.volunteer, self.client_a, _ = _donation_users()

    def _offer(self, **kw):
        data = dict(donor=self.client_a, item_name="Хлеб", category="bakery", quantity=5)
        data.update(kw)
        return Donation.objects.create(**data)

    def test_clean_rejects_bad_quantity_and_missing_item(self):
        with self.assertRaises(ValidationError):
            Donation(donor=self.client_a, item_name="x", quantity=0).full_clean(exclude=["donor"])
        with self.assertRaises(ValidationError):
            Donation(donor=self.client_a, item_name="x", quantity=200000).full_clean(exclude=["donor"])
        with self.assertRaises(ValidationError):
            Donation(donor=self.client_a).full_clean(exclude=["donor"])  # no product, no item_name

    def test_clean_requires_organization_name_for_business(self):
        with self.assertRaises(ValidationError):
            Donation(
                donor=self.client_a, item_name="Хлеб", donor_type="business",
            ).full_clean(exclude=["donor"])
        # ok with an organization name
        Donation(
            donor=self.client_a, item_name="Хлеб", donor_type="business",
            organization_name="Пекарня",
        ).full_clean(exclude=["donor"])

    def test_item_label_prefers_item_name_then_product(self):
        product = Product.objects.create(name="Плед", category="blankets")
        self.assertEqual(self._offer(item_name="", product=product).item_label, "Плед")
        self.assertEqual(self._offer(item_name="Куртка").item_label, "Куртка")

    def test_transition_happy_path(self):
        d = self._offer()
        d.approve(self.admin)
        self.assertEqual(d.status, Donation.APPROVED)
        self.assertIsNotNone(d.approved_at)
        self.assertEqual(d.reviewed_by, self.admin)
        d.mark_ready(self.admin)
        d.mark_received(self.admin)
        d.mark_distributed(self.admin)
        self.assertEqual(d.status, Donation.DISTRIBUTED)
        self.assertIsNotNone(d.distributed_at)

    def test_cancel_from_each_open_state(self):
        for step in ([], ["approve"], ["approve", "mark_ready"]):
            d = self._offer()
            for s in step:
                getattr(d, s)(self.admin)
            d.cancel(self.admin)
            self.assertEqual(d.status, Donation.CANCELLED)

    def test_illegal_transitions_raise(self):
        d = self._offer()
        with self.assertRaises(ValueError):
            d.mark_received(self.admin)  # pending -> received not allowed
        d.approve(self.admin)
        d.mark_ready(self.admin)
        d.mark_received(self.admin)
        d.mark_distributed(self.admin)
        with self.assertRaises(ValueError):
            d.approve(self.admin)  # distributed is terminal
        cancelled = self._offer()
        cancelled.cancel(self.admin)
        with self.assertRaises(ValueError):
            cancelled.approve(self.admin)

    def test_assign_volunteer_only_while_approved_or_ready(self):
        d = self._offer()
        with self.assertRaises(ValueError):
            d.assign_volunteer(self.volunteer, self.admin)  # still pending
        d.approve(self.admin)
        d.assign_volunteer(self.volunteer, self.admin)
        self.assertEqual(d.assigned_volunteer, self.volunteer)
        d.mark_ready(self.admin)
        d.mark_received(self.admin)
        with self.assertRaises(ValueError):
            d.assign_volunteer(self.volunteer, self.admin)  # received — too late


class DonationTransitionConcurrencyTests(TestCase):
    """The model's _apply_transition uses a conditional UPDATE ... WHERE
    status=<old> so two staff actions racing on the same offer can't both win.

    Reproduced deterministically with two independently-loaded in-memory copies
    of the same row — each holds the pre-race ``status`` it read, so calling a
    transition on each in turn exercises exactly the interleaving a real race
    would produce, without depending on thread timing."""

    def setUp(self):
        self.admin, _, _, self.client_a, _ = _donation_users()
        self.offer = Donation.objects.create(
            donor=self.client_a, item_name="Плед", category="blankets", quantity=2,
        )

    def test_approve_then_racing_cancel_only_approve_wins(self):
        copy_a = Donation.objects.get(pk=self.offer.pk)
        copy_b = Donation.objects.get(pk=self.offer.pk)

        copy_a.approve(self.admin)  # DB is still "pending" when this applies
        with self.assertRaises(ValueError):
            copy_b.cancel(self.admin)  # DB is now "approved" — stale WHERE misses

        final = Donation.objects.get(pk=self.offer.pk)
        self.assertEqual(final.status, Donation.APPROVED)
        self.assertIsNotNone(final.approved_at)
        self.assertIsNone(final.cancelled_at)

    def test_cancel_then_racing_approve_only_cancel_wins(self):
        copy_a = Donation.objects.get(pk=self.offer.pk)
        copy_b = Donation.objects.get(pk=self.offer.pk)

        copy_a.cancel(self.admin)
        with self.assertRaises(ValueError):
            copy_b.approve(self.admin)

        final = Donation.objects.get(pk=self.offer.pk)
        self.assertEqual(final.status, Donation.CANCELLED)
        self.assertIsNone(final.approved_at)
        self.assertIsNotNone(final.cancelled_at)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DonationServiceTests(TestCase):
    def setUp(self):
        self.admin, _, self.volunteer, self.client_a, self.client_b = _donation_users()
        self.product = Product.objects.create(
            name="Продуктовый набор", category="food", unit="наборов", is_active=True,
        )

    def test_free_text_offer_created_pending(self):
        d = donations.create_offer(
            donor=self.client_a, item_name="  Тёплые куртки  ", category="clothing",
            quantity=3, unit="шт", description="  разные размеры  ",
        )
        self.assertEqual(d.status, Donation.PENDING)
        self.assertEqual(d.item_name, "Тёплые куртки")
        self.assertEqual(d.category, "clothing")
        self.assertEqual(d.quantity, 3)
        self.assertIsNone(d.product)
        self.assertEqual(d.description, "разные размеры")

    def test_offer_needs_a_product_or_item_name(self):
        with self.assertRaises(ValidationError):
            donations.create_offer(donor=self.client_a, item_name="", product=None)
        self.assertEqual(Donation.objects.count(), 0)

    def test_business_offer_needs_organization_name(self):
        with self.assertRaises(ValidationError):
            donations.create_offer(
                donor=self.client_a, item_name="Хлеб", donor_type="business", organization_name="",
            )
        self.assertEqual(Donation.objects.count(), 0)
        d = donations.create_offer(
            donor=self.client_a, item_name="Хлеб", donor_type="business",
            organization_name="Пекарня «Нон»", quantity=50, unit="буханок",
        )
        self.assertEqual(d.donor_label, "Пекарня «Нон»")

    def test_product_offer_carries_category_and_unit(self):
        d = donations.create_offer(donor=self.client_a, product=self.product, quantity=2)
        self.assertEqual(d.category, "food")
        self.assertEqual(d.unit, "наборов")
        self.assertEqual(d.item_name, "")
        self.assertEqual(d.item_label, "Продуктовый набор")

    def test_quantity_bounds_enforced(self):
        for bad_qty in (0, -1, 200000):
            with self.assertRaises(ValidationError):
                donations.create_offer(donor=self.client_a, item_name="x", quantity=bad_qty)
        self.assertEqual(Donation.objects.count(), 0)

    def test_inactive_product_rejected_and_nothing_written(self):
        self.product.is_active = False
        self.product.save()
        with self.assertRaises(ValidationError):
            donations.create_offer(donor=self.client_a, product=self.product, quantity=1)
        self.assertEqual(Donation.objects.count(), 0)

    def test_deleting_product_keeps_the_offer_record(self):
        d = donations.create_offer(donor=self.client_a, product=self.product, quantity=1)
        self.product.delete()
        d.refresh_from_db()
        self.assertIsNone(d.product)
        self.assertEqual(d.item_name, "")  # item_label falls back to "—"

    # --- read helpers ---
    def test_donations_for_is_scoped_and_ordered(self):
        d1 = donations.create_offer(donor=self.client_a, item_name="a")
        donations.create_offer(donor=self.client_b, item_name="b")
        d3 = donations.create_offer(donor=self.client_a, item_name="c")
        self.assertEqual([d.id for d in donations.donations_for(self.client_a)], [d3.id, d1.id])

    def test_assigned_to_is_scoped_to_the_volunteer_and_open_offers(self):
        mine = donations.create_offer(donor=self.client_a, item_name="Плед", quantity=2)
        donations.approve_offer(mine, actor=self.admin)
        donations.assign_offer_volunteer(mine, volunteer=self.volunteer, actor=self.admin)
        other = donations.create_offer(donor=self.client_b, item_name="Куртка")
        donations.approve_offer(other, actor=self.admin)
        self.assertEqual({d.id for d in donations.assigned_to(self.volunteer)}, {mine.id})
        # once distributed it drops out of the assigned queue
        donations.mark_offer_ready(mine, actor=self.admin)
        donations.mark_offer_received(mine, actor=self.admin)
        donations.mark_offer_distributed(mine, actor=self.admin)
        self.assertEqual(list(donations.assigned_to(self.volunteer)), [])

    def test_offer_summary_counts_items_and_businesses(self):
        a = donations.create_offer(donor=self.client_a, product=self.product, quantity=4)
        donations.approve_offer(a, actor=self.admin)
        donations.mark_offer_ready(a, actor=self.admin)
        donations.mark_offer_received(a, actor=self.admin)  # 4 received
        b = donations.create_offer(
            donor=self.client_a, item_name="Хлеб", donor_type="business",
            organization_name="Пекарня", quantity=50,
        )
        donations.approve_offer(b, actor=self.admin)
        donations.mark_offer_ready(b, actor=self.admin)
        donations.mark_offer_received(b, actor=self.admin)
        donations.mark_offer_distributed(b, actor=self.admin)  # 50 received + distributed
        donations.create_offer(donor=self.client_b, item_name="x")  # stays pending
        summary = donations.offer_summary()
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["items_received"], 54)
        self.assertEqual(summary["items_distributed"], 50)
        self.assertEqual(summary["businesses"], 1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DonationViewTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.volunteer, self.client_a, self.client_b = _donation_users()
        self.product = Product.objects.create(name="Плед", category="blankets", unit="шт")

    def _login(self, username):
        self.client.login(username=username, password="pass12345")

    def test_anonymous_redirected_from_donation_pages(self):
        for name in ("donate", "my_donations", "donations_admin", "assigned_donations"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/login", resp.url)

    def test_authenticated_user_creates_pending_offer(self):
        self._login("dn_cli_a")
        resp = self.client.post(reverse("donate"), {
            "donor_type": "individual", "item_name": "Тёплые куртки", "category": "clothing",
            "quantity": "4", "unit": "шт", "fulfilment": "pickup",
        })
        d = Donation.objects.get(donor=self.client_a)
        self.assertEqual(d.status, Donation.PENDING)
        self.assertEqual(d.quantity, 4)
        self.assertRedirects(resp, reverse("donation_detail", args=[d.id]))

    def test_my_donations_lists_only_own(self):
        mine = donations.create_offer(donor=self.client_a, item_name="a")
        donations.create_offer(donor=self.client_b, item_name="b")
        self._login("dn_cli_a")
        resp = self.client.get(reverse("my_donations"))
        self.assertEqual({d.id for d in resp.context["donations"]}, {mine.id})

    def test_donation_detail_idor_blocked_for_unrelated_user(self):
        theirs = donations.create_offer(donor=self.client_b, item_name="b")
        self._login("dn_cli_a")
        resp = self.client.get(reverse("donation_detail", args=[theirs.id]))
        self.assertRedirects(resp, reverse("profile"))

    def test_curator_can_view_ledger_and_any_offer(self):
        theirs = donations.create_offer(donor=self.client_b, item_name="b")
        self._login("dn_curator")
        self.assertEqual(self.client.get(reverse("donations_admin")).status_code, 200)
        self.assertEqual(self.client.get(reverse("donation_detail", args=[theirs.id])).status_code, 200)

    def test_donations_ledger_is_staff_only(self):
        offer = donations.create_offer(donor=self.client_a, item_name="a")
        self._login("dn_cli_a")
        self.assertRedirects(self.client.get(reverse("donations_admin")), reverse("profile"))
        self._login("dn_curator")
        self.assertEqual(self.client.get(reverse("donations_admin")).status_code, 200)

    def test_curator_approve_advances_status_and_notifies_donor(self):
        d = donations.create_offer(donor=self.client_a, item_name="Плед", quantity=2)
        mail.outbox.clear()
        self._login("dn_curator")
        self.client.post(reverse("donation_update", args=[d.id]), {"action": "approve"})
        d.refresh_from_db()
        self.assertEqual(d.status, Donation.APPROVED)
        self.assertEqual(d.reviewed_by, self.curator)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(self.client_a.email, mail.outbox[0].to)

    def test_donation_update_requires_post(self):
        d = donations.create_offer(donor=self.client_a, item_name="a")
        self._login("dn_admin")
        self.assertEqual(self.client.get(reverse("donation_update", args=[d.id])).status_code, 405)

    def test_client_cannot_advance_someone_elses_offer(self):
        d = donations.create_offer(donor=self.client_b, item_name="a")
        self._login("dn_cli_a")
        resp = self.client.post(reverse("donation_update", args=[d.id]), {"action": "approve"})
        self.assertEqual(resp.status_code, 302)
        d.refresh_from_db()
        self.assertEqual(d.status, Donation.PENDING)

    def test_assigned_volunteer_can_advance_received_and_distributed_only(self):
        d = donations.create_offer(donor=self.client_a, item_name="Плед", quantity=2)
        donations.approve_offer(d, actor=self.admin)
        donations.assign_offer_volunteer(d, volunteer=self.volunteer, actor=self.admin)
        donations.mark_offer_ready(d, actor=self.admin)
        self._login("dn_vol")
        # cannot approve/cancel
        self.client.post(reverse("donation_update", args=[d.id]), {"action": "cancel"})
        d.refresh_from_db()
        self.assertEqual(d.status, Donation.READY)
        # can mark received then distributed
        self.client.post(reverse("donation_update", args=[d.id]), {"action": "received"})
        d.refresh_from_db()
        self.assertEqual(d.status, Donation.RECEIVED)
        self.client.post(reverse("donation_update", args=[d.id]), {"action": "distributed"})
        d.refresh_from_db()
        self.assertEqual(d.status, Donation.DISTRIBUTED)

    def test_staff_assign_volunteer_and_notify(self):
        d = donations.create_offer(donor=self.client_a, item_name="Плед", region="dushanbe")
        donations.approve_offer(d, actor=self.admin)
        mail.outbox.clear()
        self._login("dn_curator")
        self.client.post(reverse("donation_update", args=[d.id]), {
            "action": "assign", "volunteer_id": str(self.volunteer.id),
        })
        d.refresh_from_db()
        self.assertEqual(d.assigned_volunteer, self.volunteer)
        self.assertTrue(any(self.volunteer.email in m.to for m in mail.outbox))

    def test_illegal_transition_via_view_is_a_noop_with_warning(self):
        d = donations.create_offer(donor=self.client_a, item_name="a")
        self._login("dn_admin")
        resp = self.client.post(
            reverse("donation_update", args=[d.id]), {"action": "distributed"}, follow=True
        )
        d.refresh_from_db()
        self.assertEqual(d.status, Donation.PENDING)
        self.assertContains(resp, "недоступно")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DonationDashboardTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()

    def test_client_dashboard_carries_own_offers(self):
        mine = donations.create_offer(donor=self.cli_a, item_name="Плед", quantity=2)
        donations.create_offer(donor=self.cli_b, item_name="Куртка")
        self.client.login(username="d5_cli_a", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertEqual(resp.context["donation_count"], 1)
        self.assertEqual({d.id for d in resp.context["recent_donations"]}, {mine.id})

    def test_admin_dashboard_has_offer_summary(self):
        donations.create_offer(donor=self.cli_a, item_name="a")
        self.client.login(username="d5_admin", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertIn("offer_summary", resp.context)
        self.assertEqual(resp.context["offer_summary"]["pending"], 1)

    def test_curator_dashboard_has_pending_offers_queue(self):
        donations.create_offer(donor=self.cli_a, item_name="a")
        self.client.login(username="d5_curator", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertIn("offer_summary", resp.context)
        self.assertEqual(len(resp.context["donation_offers_pending"]), 1)

    def test_volunteer_dashboard_carries_assigned_deliveries(self):
        d = donations.create_offer(donor=self.cli_a, item_name="Плед", quantity=2)
        donations.approve_offer(d, actor=self.admin)
        donations.assign_offer_volunteer(d, volunteer=self.vol_a, actor=self.admin)
        self.client.login(username="d5_vol_a", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertEqual({x.id for x in resp.context["assigned_donations"]}, {d.id})

# ===========================================================================
# Lost & Found pets (Stage 6, increment D)
# ===========================================================================

def _pet_users():
    admin = Users.objects.create_superuser(username="pet_admin", email="pet_admin@example.com", password="pass12345")
    curator = Users.objects.create_user(username="pet_curator", email="pet_curator@example.com", password="pass12345", is_curator=True)
    reporter = Users.objects.create_user(username="pet_owner", email="pet_owner@example.com", password="pass12345", is_client=True, region="dushanbe")
    other = Users.objects.create_user(username="pet_other", email="pet_other@example.com", password="pass12345", is_client=True, region="dushanbe")
    volunteer = Users.objects.create_user(username="pet_vol", email="pet_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe")
    return admin, curator, reporter, other, volunteer


def _pet(reporter, **kw):
    data = dict(report_type="lost", species="dog", description="brown dog", region="dushanbe")
    data.update(kw)
    return PetReport.objects.create(reporter=reporter, **data)


class PetReportModelTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, _ = _pet_users()

    def test_defaults(self):
        r = _pet(self.reporter)
        self.assertEqual(r.status, PetReport.STATUS_OPEN)
        self.assertTrue(r.is_open)
        self.assertFalse(r.has_location)
        self.assertEqual(r.display_name, "Собака")  # falls back to species

    def test_display_name_prefers_pet_name(self):
        self.assertEqual(_pet(self.reporter, pet_name="Rex").display_name, "Rex")

    def test_lost_and_found_creation(self):
        lost = _pet(self.reporter, report_type="lost")
        found = _pet(self.other, report_type="found")
        self.assertEqual(lost.report_type, "lost")
        self.assertEqual(found.report_type, "found")

    def test_transition_happy_path_and_stamps(self):
        r = _pet(self.reporter)
        r.mark_matched(self.curator)
        self.assertEqual(r.status, PetReport.STATUS_MATCHED)
        self.assertIsNotNone(r.matched_at)
        self.assertEqual(r.reviewed_by, self.curator)
        r.resolve(self.reporter)
        self.assertEqual(r.status, PetReport.STATUS_RESOLVED)
        self.assertIsNotNone(r.resolved_at)
        self.assertFalse(r.is_open)

    def test_matched_can_fall_back_to_open(self):
        r = _pet(self.reporter)
        r.mark_matched(self.curator)
        r.reopen(self.curator)
        self.assertEqual(r.status, PetReport.STATUS_OPEN)

    def test_illegal_transitions_raise(self):
        r = _pet(self.reporter)
        r.resolve(self.reporter)
        with self.assertRaises(ValueError):
            r.reopen(self.curator)          # resolved is terminal
        with self.assertRaises(ValueError):
            r.close(self.reporter)
        closed = _pet(self.reporter)
        closed.close(self.reporter)
        with self.assertRaises(ValueError):
            closed.mark_matched(self.curator)

    def test_coordinate_validation_via_form(self):
        from .forms import PetReportForm
        bad = PetReportForm(data={
            "report_type": "lost", "species": "dog", "description": "x",
            "region": "dushanbe", "latitude": "800", "longitude": "10",
        })
        self.assertFalse(bad.is_valid())
        half = PetReportForm(data={
            "report_type": "lost", "species": "dog", "description": "x",
            "region": "dushanbe", "latitude": "38.5", "longitude": "",
        })
        self.assertFalse(half.is_valid())
        ok = PetReportForm(data={
            "report_type": "lost", "species": "dog", "description": "x",
            "region": "dushanbe", "latitude": "38.5", "longitude": "68.7",
        })
        self.assertTrue(ok.is_valid(), ok.errors)

    def test_form_requires_region(self):
        from .forms import PetReportForm
        form = PetReportForm(data={"report_type": "lost", "species": "dog", "description": "x"})
        self.assertFalse(form.is_valid())
        self.assertIn("region", form.errors)

    def test_service_drops_invalid_coordinates_instead_of_storing(self):
        r = pets.create_pet_report(
            reporter=self.reporter, report_type="lost", description="x",
            species="dog", region="dushanbe", latitude="999", longitude="1",
        )
        self.assertFalse(r.has_location)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PetReportPermissionTests(TestCase):
    """Ownership / IDOR: only the reporter (and staff, where allowed) may act on
    a report. Everyone else gets bounced with no state change."""

    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, self.volunteer = _pet_users()
        self.report = _pet(self.reporter, contact_phone="+992 900 111 222")
        mail.outbox.clear()

    def _login(self, username):
        self.client.login(username=username, password="pass12345")

    def test_board_and_detail_require_login(self):
        for url in [reverse("pet_list"), reverse("pet_report_create"),
                    reverse("my_pet_reports"), reverse("pet_report_detail", args=[self.report.id])]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("/login", resp.url)

    def test_owner_sees_own_contact_details(self):
        self._login("pet_owner")
        resp = self.client.get(reverse("pet_report_detail", args=[self.report.id]))
        self.assertTrue(resp.context["can_manage"])
        self.assertContains(resp, "+992 900 111 222")

    def test_non_owner_non_staff_never_sees_contact_or_reporter(self):
        self._login("pet_other")
        resp = self.client.get(reverse("pet_report_detail", args=[self.report.id]))
        self.assertFalse(resp.context["can_manage"])
        self.assertNotContains(resp, "+992 900 111 222")
        self.assertNotContains(resp, self.reporter.email)
        self.assertNotContains(resp, self.reporter.username)
        self.assertEqual(resp.context["possible_matches"], [])

    def test_staff_sees_contact_and_matches(self):
        self._login("pet_curator")
        resp = self.client.get(reverse("pet_report_detail", args=[self.report.id]))
        self.assertTrue(resp.context["can_manage"])
        self.assertContains(resp, "+992 900 111 222")

    def test_other_user_cannot_edit_or_delete_report(self):
        self._login("pet_other")
        resp = self.client.post(reverse("pet_report_edit", args=[self.report.id]), {
            "report_type": "found", "species": "cat", "description": "hijacked",
            "region": "sogd",
        })
        self.assertRedirects(resp, reverse("profile"))
        self.report.refresh_from_db()
        self.assertEqual(self.report.description, "brown dog")

        resp = self.client.post(reverse("pet_report_delete", args=[self.report.id]))
        self.assertRedirects(resp, reverse("profile"))
        self.assertTrue(PetReport.objects.filter(pk=self.report.pk).exists())

    def test_other_user_cannot_change_status(self):
        self._login("pet_other")
        resp = self.client.post(reverse("pet_report_status", args=[self.report.id]), {"action": "resolve"})
        self.assertRedirects(resp, reverse("profile"))
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, PetReport.STATUS_OPEN)

    def test_owner_can_resolve_and_close_own_report(self):
        self._login("pet_owner")
        self.client.post(reverse("pet_report_status", args=[self.report.id]), {"action": "resolve"})
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, PetReport.STATUS_RESOLVED)

    def test_owner_can_edit_own_open_report(self):
        self._login("pet_owner")
        resp = self.client.get(reverse("pet_report_edit", args=[self.report.id]))
        self.assertEqual(resp.context["form"].initial["region"], "dushanbe")  # pre-filled
        resp = self.client.post(reverse("pet_report_edit", args=[self.report.id]), {
            "report_type": "lost", "pet_name": "Renamed", "species": "dog",
            "description": "updated details", "region": "sogd", "contact_phone": "+992 111",
        })
        self.assertRedirects(resp, reverse("pet_report_detail", args=[self.report.id]))
        self.report.refresh_from_db()
        self.assertEqual((self.report.pet_name, self.report.description, self.report.region),
                         ("Renamed", "updated details", "sogd"))

    def test_owner_cannot_use_staff_only_actions(self):
        self._login("pet_owner")
        resp = self.client.post(reverse("pet_report_status", args=[self.report.id]), {"action": "match"}, follow=True)
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, PetReport.STATUS_OPEN)
        self.assertContains(resp, "координатор")

    def test_staff_can_mark_matched(self):
        self._login("pet_curator")
        self.client.post(reverse("pet_report_status", args=[self.report.id]), {"action": "match"})
        self.report.refresh_from_db()
        self.assertEqual(self.report.status, PetReport.STATUS_MATCHED)

    def test_admin_can_delete_any_report(self):
        self._login("pet_admin")
        resp = self.client.post(reverse("pet_report_delete", args=[self.report.id]))
        self.assertRedirects(resp, reverse("my_pet_reports"))
        self.assertFalse(PetReport.objects.filter(pk=self.report.pk).exists())

    def test_status_and_delete_and_edit_are_post_only(self):
        self._login("pet_owner")
        self.assertEqual(self.client.get(reverse("pet_report_status", args=[self.report.id])).status_code, 405)
        self.assertEqual(self.client.get(reverse("pet_report_delete", args=[self.report.id])).status_code, 405)
        # edit GET is a normal form render for the owner
        self.assertEqual(self.client.get(reverse("pet_report_edit", args=[self.report.id])).status_code, 200)

    def test_resolved_report_is_hidden_from_strangers_but_not_owner(self):
        self.report.resolve(self.reporter)
        self._login("pet_other")
        self.assertEqual(self.client.get(reverse("pet_report_detail", args=[self.report.id])).status_code, 404)
        self._login("pet_owner")
        self.assertEqual(self.client.get(reverse("pet_report_detail", args=[self.report.id])).status_code, 200)
        self._login("pet_curator")
        self.assertEqual(self.client.get(reverse("pet_report_detail", args=[self.report.id])).status_code, 200)

    def test_cannot_edit_a_closed_report(self):
        self.report.close(self.reporter)
        self._login("pet_owner")
        resp = self.client.post(reverse("pet_report_edit", args=[self.report.id]), {
            "report_type": "lost", "species": "dog", "description": "changed", "region": "dushanbe",
        }, follow=True)
        self.report.refresh_from_db()
        self.assertEqual(self.report.description, "brown dog")


class PetBoardAndListTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, self.volunteer = _pet_users()

    def _login(self, u="pet_owner"):
        self.client.login(username=u, password="pass12345")

    def test_board_lists_only_open_and_matched(self):
        open_r = _pet(self.reporter)
        matched_r = _pet(self.reporter, pet_name="M")
        matched_r.mark_matched(self.curator)
        resolved_r = _pet(self.reporter, pet_name="R")
        resolved_r.resolve(self.reporter)
        closed_r = _pet(self.reporter, pet_name="C")
        closed_r.close(self.reporter)

        self._login()
        resp = self.client.get(reverse("pet_list"))
        ids = {c["id"] for c in resp.context["cards"]}
        self.assertEqual(ids, {open_r.id, matched_r.id})

    def test_board_filters_by_type_species_region(self):
        _pet(self.reporter, report_type="lost", species="dog", region="dushanbe")
        _pet(self.reporter, report_type="found", species="cat", region="sogd")
        self._login()
        resp = self.client.get(reverse("pet_list"), {"type": "found"})
        self.assertEqual(len(resp.context["cards"]), 1)
        self.assertEqual(resp.context["cards"][0]["report_type"], "found")
        resp = self.client.get(reverse("pet_list"), {"species": "dog"})
        self.assertEqual([c["species_display"] for c in resp.context["cards"]], ["Собака"])
        resp = self.client.get(reverse("pet_list"), {"region": "sogd"})
        self.assertEqual(len(resp.context["cards"]), 1)

    def test_board_cards_carry_no_reporter_or_contact(self):
        _pet(self.reporter, contact_phone="+992 555 000 111")
        self._login("pet_other")
        resp = self.client.get(reverse("pet_list"))
        self.assertNotContains(resp, "+992 555 000 111")
        self.assertNotContains(resp, self.reporter.username)
        card = resp.context["cards"][0]
        self.assertNotIn("contact_phone", card)
        self.assertNotIn("reporter", card)

    def test_my_reports_scoped_to_current_user(self):
        mine = _pet(self.reporter)
        _pet(self.other)
        self._login("pet_owner")
        resp = self.client.get(reverse("my_pet_reports"))
        self.assertEqual({r.id for r in resp.context["reports"]}, {mine.id})

    def test_my_reports_shows_all_statuses(self):
        r = _pet(self.reporter)
        r.resolve(self.reporter)
        self._login("pet_owner")
        resp = self.client.get(reverse("my_pet_reports"))
        self.assertEqual([x.id for x in resp.context["reports"]], [r.id])

    def test_create_view_publishes_and_redirects_to_detail(self):
        self._login("pet_owner")
        resp = self.client.post(reverse("pet_report_create"), {
            "report_type": "lost", "pet_name": "Bobik", "species": "dog",
            "description": "small brown dog", "region": "dushanbe",
        })
        report = PetReport.objects.get(pet_name="Bobik")
        self.assertEqual(report.reporter, self.reporter)
        self.assertEqual(report.status, PetReport.STATUS_OPEN)
        self.assertRedirects(resp, reverse("pet_report_detail", args=[report.id]))


class PetMapDataTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, self.volunteer = _pet_users()

    def _points(self, username):
        self.client.login(username=username, password="pass12345")
        resp = self.client.get(reverse("map_data"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        return [p for p in resp.json()["points"] if p.get("kind") == "pet"]

    def test_located_open_reports_appear_for_every_role(self):
        _pet(self.reporter, latitude="38.56", longitude="68.78", contact_phone="+992 900 000 111")
        for u in ("pet_owner", "pet_other", "pet_vol", "pet_curator", "pet_admin"):
            pts = self._points(u)
            self.assertEqual(len(pts), 1, u)

    def test_pet_map_points_expose_only_safe_fields(self):
        _pet(self.reporter, pet_name="Secret", latitude="38.56", longitude="68.78", contact_phone="+992 900 000 111")
        pts = self._points("pet_other")
        point = pts[0]
        self.assertEqual(set(point), {"id", "kind", "lat", "lng", "title", "subtitle", "report_type", "species", "region", "status", "url"})
        self.assertNotIn("+992 900 000 111", str(point))
        self.assertNotIn(self.reporter.username, str(point))

    def test_report_without_coordinates_has_no_marker(self):
        _pet(self.reporter)
        self.assertEqual(self._points("pet_other"), [])

    def test_resolved_reports_leave_the_map(self):
        r = _pet(self.reporter, latitude="38.56", longitude="68.78")
        self.assertEqual(len(self._points("pet_other")), 1)
        r.resolve(self.reporter)
        self.assertEqual(self._points("pet_other"), [])


class PetUploadTests(TestCase):
    def setUp(self):
        _, _, self.reporter, _, _ = _pet_users()
        self.client.login(username="pet_owner", password="pass12345")

    def _post(self, image):
        return self.client.post(reverse("pet_report_create"), {
            "report_type": "found", "species": "cat", "description": "found a cat",
            "region": "dushanbe", "image": image,
        })

    def test_valid_small_image_is_accepted(self):
        resp = self._post(_small_image_file("pet.png"))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(PetReport.objects.filter(reporter=self.reporter).exists())
        PetReport.objects.get(reporter=self.reporter).image.delete(save=False)

    def test_oversized_image_is_rejected_by_the_shared_validator(self):
        resp = self._post(_oversized_image_file("huge.png"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "5")
        self.assertFalse(PetReport.objects.filter(reporter=self.reporter).exists())

    def test_non_image_upload_is_rejected(self):
        bad = SimpleUploadedFile("evil.svg", b"<svg onload=alert(1)></svg>", content_type="image/svg+xml")
        resp = self._post(bad)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PetReport.objects.filter(reporter=self.reporter).exists())


class PetMatchingTests(TestCase):
    """services.pets.possible_matches — deterministic, opposite-type,
    same-species, near-or-same-region. Not a second matching engine."""

    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, _ = _pet_users()

    def test_opposite_type_same_species_nearby_is_a_match(self):
        lost = _pet(self.reporter, report_type="lost", species="cat", latitude="38.560", longitude="68.780")
        found = _pet(self.other, report_type="found", species="cat", latitude="38.561", longitude="68.781")
        matches = pets.possible_matches(lost)
        self.assertEqual([m["report"].id for m in matches], [found.id])
        self.assertLess(matches[0]["distance_km"], 1)

    def test_same_type_is_never_a_match(self):
        lost = _pet(self.reporter, report_type="lost", species="cat", latitude="38.56", longitude="68.78")
        _pet(self.other, report_type="lost", species="cat", latitude="38.56", longitude="68.78")
        self.assertEqual(pets.possible_matches(lost), [])

    def test_different_species_is_never_a_match(self):
        lost = _pet(self.reporter, report_type="lost", species="cat", latitude="38.56", longitude="68.78")
        _pet(self.other, report_type="found", species="dog", latitude="38.56", longitude="68.78")
        self.assertEqual(pets.possible_matches(lost), [])

    def test_far_located_report_is_excluded(self):
        lost = _pet(self.reporter, report_type="lost", species="dog", region="dushanbe", latitude="38.56", longitude="68.78")
        # Khujand ~ 200 km away, and a different region -> not a lead.
        _pet(self.other, report_type="found", species="dog", region="sogd", latitude="40.28", longitude="69.62")
        self.assertEqual(pets.possible_matches(lost), [])

    def test_same_region_without_coordinates_is_a_match(self):
        lost = _pet(self.reporter, report_type="lost", species="bird", region="khatlon")
        found = _pet(self.other, report_type="found", species="bird", region="khatlon")
        matches = pets.possible_matches(lost)
        self.assertEqual([m["report"].id for m in matches], [found.id])
        self.assertIsNone(matches[0]["distance_km"])
        self.assertTrue(matches[0]["same_region"])

    def test_resolved_counterpart_is_not_suggested(self):
        lost = _pet(self.reporter, report_type="lost", species="cat", region="rrp")
        found = _pet(self.other, report_type="found", species="cat", region="rrp")
        found.resolve(self.other)
        self.assertEqual(pets.possible_matches(lost), [])

    def test_nearest_first_ordering_is_deterministic(self):
        lost = _pet(self.reporter, report_type="lost", species="dog", region="dushanbe", latitude="38.560", longitude="68.780")
        near = _pet(self.other, report_type="found", species="dog", region="dushanbe", latitude="38.562", longitude="68.782")
        far = _pet(self.other, report_type="found", species="dog", region="dushanbe", latitude="38.60", longitude="68.82")
        ids = [m["report"].id for m in pets.possible_matches(lost)]
        self.assertEqual(ids, [near.id, far.id])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PetNotificationTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, _ = _pet_users()
        mail.outbox.clear()

    def test_new_report_notifies_staff_once(self):
        pets.create_pet_report(reporter=self.reporter, report_type="lost", description="x", species="dog", region="dushanbe")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].subject, pets.NEW_REPORT_SUBJECT)
        self.assertCountEqual(mail.outbox[0].to, [self.admin.email, self.curator.email])

    def test_new_report_pings_owner_of_a_matching_counterpart(self):
        found = pets.create_pet_report(reporter=self.other, report_type="found", description="grey cat",
                                       species="cat", region="dushanbe", latitude="38.56", longitude="68.78")
        mail.outbox.clear()
        pets.create_pet_report(reporter=self.reporter, report_type="lost", description="grey cat",
                               species="cat", region="dushanbe", latitude="38.561", longitude="68.781")
        subjects = [m.subject for m in mail.outbox]
        self.assertIn(pets.MATCH_HINT_SUBJECT, subjects)
        hint = next(m for m in mail.outbox if m.subject == pets.MATCH_HINT_SUBJECT)
        self.assertEqual(hint.to, [self.other.email])

    def test_no_self_match_ping_when_same_person_filed_both(self):
        pets.create_pet_report(reporter=self.reporter, report_type="found", description="dog",
                               species="dog", region="sogd")
        mail.outbox.clear()
        pets.create_pet_report(reporter=self.reporter, report_type="lost", description="dog",
                               species="dog", region="sogd")
        self.assertNotIn(pets.MATCH_HINT_SUBJECT, [m.subject for m in mail.outbox])

    def test_staff_status_change_notifies_reporter(self):
        report = _pet(self.reporter)
        mail.outbox.clear()
        pets.apply_action(report, "match", actor=self.curator)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.reporter.email])

    def test_owner_self_action_does_not_email_themselves(self):
        report = _pet(self.reporter)
        mail.outbox.clear()
        pets.apply_action(report, "resolve", actor=self.reporter)
        self.assertEqual(mail.outbox, [])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PetDashboardTests(TestCase):
    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, _ = _pet_users()

    def test_client_dashboard_carries_own_pet_reports_only(self):
        mine = _pet(self.reporter, pet_name="Mine")
        _pet(self.other, pet_name="Theirs")
        self.client.login(username="pet_owner", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertEqual(resp.context["pet_report_count"], 1)
        self.assertEqual({r.id for r in resp.context["my_pet_reports"]}, {mine.id})
        self.assertNotContains(resp, "Theirs")

    def test_curator_dashboard_shows_open_board_count_and_recent(self):
        _pet(self.reporter, pet_name="Buddy")
        self.client.login(username="pet_curator", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertEqual(resp.context["pets_open_count"], 1)
        self.assertEqual([r.pet_name for r in resp.context["recent_pet_reports"]], ["Buddy"])

    def test_curator_dashboard_pet_card_has_no_contact_detail(self):
        _pet(self.reporter, pet_name="Buddy", contact_phone="+992 900 777 000")
        self.client.login(username="pet_curator", password="pass12345")
        resp = self.client.get(reverse("profile"))
        self.assertNotContains(resp, "+992 900 777 000")


class PetRegressionGuardTests(TestCase):
    """Lost & Found must not have disturbed the neighbouring systems."""

    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, self.volunteer = _pet_users()

    def test_help_request_flow_untouched(self):
        task = HelpRequest.objects.create(
            client=self.reporter, help_type="grocery", description="d", address="a",
            phone="p", region="dushanbe", status="pending",
        )
        task.accept(self.volunteer)
        self.assertEqual(task.status, "active")
        task.complete()
        self.assertEqual(task.status, "completed")

    def test_pet_report_is_not_a_help_request(self):
        _pet(self.reporter)
        self.assertEqual(HelpRequest.objects.count(), 0)

    def test_map_data_still_returns_tasks_and_pets_together(self):
        HelpRequest.objects.create(
            client=self.reporter, help_type="grocery", description="d", address="a",
            phone="p", region="dushanbe", status="pending", latitude="38.55", longitude="68.77",
        )
        _pet(self.reporter, latitude="38.56", longitude="68.78")
        self.client.login(username="pet_curator", password="pass12345")
        resp = self.client.get(reverse("map_data"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        kinds = {p["kind"] for p in resp.json()["points"]}
        self.assertIn("task", kinds)
        self.assertIn("pet", kinds)


# ===========================================================================
# Stage 7 — production-hardening regression tests
# ===========================================================================
# One class per finding fixed in Stage 7. Each reproduces the pre-fix failure
# (or asserts the new invariant) deterministically — no reliance on thread
# timing or the network.


class PetReportCoordinateValidationTests(TestCase):
    """Stage 7 LOW 1: PetReportAdmin left latitude/longitude editable, and a
    bare DecimalField(max_digits=9) accepts latitude 800. PetReport.clean() now
    closes that path by reusing services.geo.is_valid_coordinate."""

    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, self.volunteer = _pet_users()

    def test_model_clean_rejects_out_of_range_coordinates(self):
        report = PetReport(
            reporter=self.reporter, report_type="lost", species="dog",
            description="x", region="dushanbe", latitude=Decimal("800"), longitude=Decimal("10"),
        )
        with self.assertRaises(ValidationError):
            report.full_clean()

    def test_model_clean_rejects_half_a_coordinate_pair(self):
        report = PetReport(
            reporter=self.reporter, report_type="lost", species="dog",
            description="x", region="dushanbe", latitude=Decimal("38.5"), longitude=None,
        )
        with self.assertRaises(ValidationError):
            report.full_clean()

    def test_model_clean_accepts_a_valid_pair_and_no_pair(self):
        PetReport(
            reporter=self.reporter, report_type="lost", species="dog",
            description="x", region="dushanbe", latitude=Decimal("38.5"), longitude=Decimal("68.7"),
        ).full_clean()
        PetReport(
            reporter=self.reporter, report_type="lost", species="dog",
            description="x", region="dushanbe",
        ).full_clean()

    def test_admin_change_form_rejects_bad_coordinates(self):
        """The Django-admin edit path specifically — an admin fat-fingering a
        pin must not persist latitude 800."""
        from django.contrib.admin.sites import AdminSite
        from myapp.admin import PetReportAdmin

        report = _pet(self.reporter, latitude=Decimal("38.5"), longitude=Decimal("68.7"))
        model_admin = PetReportAdmin(PetReport, AdminSite())
        FormClass = model_admin.get_form(request=None, obj=report, change=True)
        form = FormClass(
            data={
                "report_type": "lost", "species": "dog", "breed": "", "pet_name": "",
                "description": "x", "region": "dushanbe", "contact_phone": "",
                "latitude": "800", "longitude": "10",
            },
            instance=report,
        )
        self.assertFalse(form.is_valid())


class PossibleMatchesCoordinateSafetyTests(TestCase):
    """Stage 7 LOW 2: possible_matches() fed float(report.latitude) straight to
    haversine_km. A malformed stored pin (e.g. saved before clean() existed)
    must not crash it or skew the distance — it falls back to the region signal."""

    def setUp(self):
        self.admin, self.curator, self.reporter, self.other, self.volunteer = _pet_users()

    def test_malformed_stored_coordinate_does_not_crash_and_falls_back_to_region(self):
        lost = _pet(self.reporter, report_type="lost", species="cat", region="dushanbe")
        found = _pet(self.other, report_type="found", species="cat", region="dushanbe",
                     latitude=Decimal("38.55"), longitude=Decimal("68.77"))
        # Force an out-of-range latitude past the model/form guards.
        PetReport.objects.filter(pk=lost.pk).update(latitude=Decimal("900"), longitude=Decimal("10"))
        lost.refresh_from_db()

        matches = pets.possible_matches(lost)
        self.assertEqual([m["report"].pk for m in matches], [found.pk])
        self.assertIsNone(matches[0]["distance_km"])   # distance dropped, not garbage
        self.assertTrue(matches[0]["same_region"])

    def test_malformed_candidate_coordinate_is_ignored_for_distance(self):
        lost = _pet(self.reporter, report_type="lost", species="cat", region="dushanbe",
                    latitude=Decimal("38.55"), longitude=Decimal("68.77"))
        found = _pet(self.other, report_type="found", species="cat", region="dushanbe")
        PetReport.objects.filter(pk=found.pk).update(latitude=Decimal("-500"), longitude=Decimal("1"))

        matches = pets.possible_matches(lost)
        self.assertEqual([m["report"].pk for m in matches], [found.pk])
        self.assertIsNone(matches[0]["distance_km"])


class RecommendVolunteersDeterminismTests(TestCase):
    """Stage 7 LOW 3: with identical score and distance, two volunteers could
    swap order between calls (the candidate queryset carries no stable order).
    The sort now ends with volunteer.id."""

    def setUp(self):
        self.client_user = Users.objects.create_user(
            username="det_client", email="det_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, help_type="grocery", description="x", address="a", phone="p",
            region="dushanbe", status="pending", latitude=TASK_LAT, longitude=TASK_LNG,
        )

    def _twin_volunteer(self, name):
        v = Users.objects.create_user(
            username=name, email=f"{name}@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        p = v.profile
        p.latitude = TASK_LAT + 5 / KM_PER_DEGREE_LAT
        p.longitude = TASK_LNG
        p.location_updated_at = timezone.now()
        p.availability_status = "available"
        p.save()
        return v

    def test_identical_candidates_order_is_stable_and_by_id(self):
        a = self._twin_volunteer("twin_a")
        b = self._twin_volunteer("twin_b")
        first = [i["volunteer"].id for i in recommend_volunteers(self.task)]
        second = [i["volunteer"].id for i in recommend_volunteers(self.task)]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted([a.id, b.id]))

    def test_candidate_cap_keeps_the_nearest(self):
        from myapp.services import matching as _m
        with mock.patch.object(_m, "CANDIDATE_CAP", 2):
            near = self._twin_volunteer("near")
            near.profile.latitude = TASK_LAT + 1 / KM_PER_DEGREE_LAT
            near.profile.save()
            self._twin_volunteer("mid")
            far = self._twin_volunteer("far")
            far.profile.latitude = TASK_LAT + 30 / KM_PER_DEGREE_LAT
            far.profile.save()
            ids = [i["volunteer"].id for i in recommend_volunteers(self.task, limit=5)]
            self.assertIn(near.id, ids)
            self.assertNotIn(far.id, ids)   # dropped by the proximity pre-filter
            self.assertEqual(len(ids), 2)


class DonationQuantityFieldValidatorTests(TestCase):
    """Donation.quantity carries field-level Min/Max validators, so the bound
    holds on any full_clean() path (Django admin, shell) — not only
    Donation.clean() and the service."""

    def setUp(self):
        self.admin, _, _, self.client_a, _ = _donation_users()

    def test_quantity_field_has_bounds_validators(self):
        validators = Donation._meta.get_field("quantity").validators
        limits = {getattr(v, "limit_value", None) for v in validators}
        self.assertIn(1, limits)
        self.assertIn(100000, limits)

    def test_full_clean_rejects_zero_and_over_cap_quantity(self):
        for bad in (0, 100001):
            with self.assertRaises(ValidationError):
                Donation(
                    donor=self.client_a, item_name="x", quantity=bad,
                ).full_clean(exclude=["donor"])


class MapEndpointBoundsTests(TestCase):
    """Stage 7 LOW 5: the operations-map JSON built unbounded querysets. Each
    category is now capped (an order + slice), so one request can't serialise
    an arbitrarily large result."""

    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()

    def test_client_task_markers_are_capped(self):
        for i in range(4):
            _hr(self.cli_a, latitude="38.55", longitude="68.77", status="pending")
        self.client.login(username="d5_cli_a", password="pass12345")
        with mock.patch("myapp.views.MAP_TASK_LIMIT", 2):
            resp = self.client.get(reverse("map_data"))
        task_points = [p for p in resp.json()["points"] if p["kind"] == "task"]
        self.assertEqual(len(task_points), 2)

    def test_staff_volunteer_markers_are_capped(self):
        for i in range(4):
            v = Users.objects.create_user(
                username=f"mv_{i}", email=f"mv_{i}@example.com", password="pass12345",
                is_volunteer=True, region="dushanbe",
            )
            v.profile.latitude = "38.55"
            v.profile.longitude = "68.77"
            v.profile.location_updated_at = timezone.now()
            v.profile.save()
        self.client.login(username="d5_curator", password="pass12345")
        with mock.patch("myapp.views.MAP_VOLUNTEER_LIMIT", 3):
            resp = self.client.get(reverse("map_data"))
        vol_points = [p for p in resp.json()["points"] if p["kind"] == "volunteer"]
        self.assertEqual(len(vol_points), 3)

    def test_open_board_points_respects_limit(self):
        for i in range(4):
            _pet(self.cli_a, latitude="38.55", longitude="68.77")
        self.assertEqual(len(pets.open_board_points(limit=2)), 2)

    def test_admin_panel_task_grids_are_capped(self):
        for i in range(4):
            _hr(self.cli_a, status="pending")
        self.client.login(username="d5_admin", password="pass12345")
        with mock.patch("myapp.views.ADMIN_PANEL_TASK_LIMIT", 2):
            resp = self.client.get(reverse("admin_panel"))
        self.assertEqual(len(resp.context["free_requests"]), 2)


class CrmOverdueStaleFilterCentralisationTests(TestCase):
    """Stage 7 LOW 6: crm/tasks/?overdue=1 and ?stale=1 re-inlined the threshold
    comparison. They now apply the one predicate from services.overdue /
    services.stale, so the CRM filter, the sweep and the dashboard can't drift."""

    def setUp(self):
        self.admin, self.curator, self.vol_a, self.vol_b, self.cli_a, self.cli_b = _dash_users()
        self.client.login(username="d5_curator", password="pass12345")

    def test_overdue_filter_matches_the_service_queryset(self):
        fresh = _hr(self.cli_a, status="active", volunteer=self.vol_a, accepted_at=timezone.now())
        old = _hr(self.cli_a, status="active", volunteer=self.vol_a,
                  accepted_at=timezone.now() - timezone.timedelta(hours=4))
        resp = self.client.get(reverse("crm_tasks") + "?overdue=1")
        shown = {t.id for t in resp.context["page_obj"]}
        self.assertEqual(shown, {t.id for t in overdue.currently_overdue_tasks()})
        self.assertIn(old.id, shown)
        self.assertNotIn(fresh.id, shown)

    def test_stale_filter_matches_the_service_queryset(self):
        fresh = _hr(self.cli_a)
        stuck = _hr(self.cli_a)
        HelpRequest.objects.filter(pk=stuck.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=60)
        )
        resp = self.client.get(reverse("crm_tasks") + "?stale=1")
        shown = {t.id for t in resp.context["page_obj"]}
        self.assertEqual(shown, {t.id for t in stale.currently_stale_pending()})
        self.assertIn(stuck.id, shown)
        self.assertNotIn(fresh.id, shown)

    def test_q_helpers_agree_with_analytics_counts(self):
        _hr(self.cli_a, status="active", volunteer=self.vol_a,
            accepted_at=timezone.now() - timezone.timedelta(hours=4))
        stuck = _hr(self.cli_a)
        HelpRequest.objects.filter(pk=stuck.pk).update(
            created_at=timezone.now() - timezone.timedelta(hours=60)
        )
        stats = analytics.dashboard_stats()
        self.assertEqual(stats["tasks_overdue"], HelpRequest.objects.filter(overdue.currently_overdue_q()).count())
        self.assertEqual(stats["tasks_stale"], HelpRequest.objects.filter(stale.currently_stale_pending_q()).count())


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class CompleteTaskConcurrencyTests(TestCase):
    """Stage 7: complete_task_view used task.complete() (fetch-then-save), so a
    double-click / retried POST completed the task twice and added the rating
    points twice. It now claims the row with a conditional UPDATE."""

    def setUp(self):
        self.volunteer = Users.objects.create_user(
            username="ct_vol", email="ct_vol@example.com", password="pass12345",
            is_volunteer=True, region="dushanbe",
        )
        self.client_user = Users.objects.create_user(
            username="ct_client", email="ct_client@example.com", password="pass12345",
            is_client=True, region="dushanbe",
        )
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, help_type="grocery",
            description="x", address="a", phone="p", region="dushanbe", status="active",
            accepted_at=timezone.now(),
        )

    def test_double_complete_awards_points_once(self):
        self.client.login(username="ct_vol", password="pass12345")
        self.client.post(reverse("complete_task", args=[self.task.pk]))
        self.client.post(reverse("complete_task", args=[self.task.pk]))  # retry
        self.volunteer.profile.refresh_from_db()
        self.assertEqual(self.volunteer.profile.rating, 3)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "completed")

    def test_racing_completion_loser_is_a_no_op(self):
        # DB row already completed by a concurrent request a moment earlier.
        HelpRequest.objects.filter(pk=self.task.pk).update(status="completed", completed_at=timezone.now())
        self.client.login(username="ct_vol", password="pass12345")
        resp = self.client.post(reverse("complete_task", args=[self.task.pk]))
        self.assertEqual(resp.status_code, 404)  # get_object_or_404: no longer active
        self.volunteer.profile.refresh_from_db()
        self.assertEqual(self.volunteer.profile.rating, 0)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class PhotoReportHelpRequestScopingTests(TestCase):
    """Stage 7: PhotoReportForm.help_request enumerated every HelpRequest in the
    system, and its option labels carry the client's username. A volunteer now
    only sees tasks they are attached to; staff keep the full list."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(username="ps_admin", email="ps_admin@example.com", password="pass12345")
        self.vol = Users.objects.create_user(
            username="ps_vol", email="ps_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.other_vol = Users.objects.create_user(
            username="ps_vol2", email="ps_vol2@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )
        self.cli = Users.objects.create_user(
            username="ps_cli", email="ps_cli@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.mine = HelpRequest.objects.create(
            client=self.cli, volunteer=self.vol, help_type="grocery", description="x",
            address="a", phone="p", region="dushanbe", status="completed",
        )
        self.not_mine = HelpRequest.objects.create(
            client=self.cli, volunteer=self.other_vol, help_type="grocery", description="y",
            address="a", phone="p", region="dushanbe", status="active",
        )

    def test_volunteer_form_only_lists_their_own_tasks(self):
        form = PhotoReportForm(user=self.vol)
        qs_ids = set(form.fields["help_request"].queryset.values_list("id", flat=True))
        self.assertEqual(qs_ids, {self.mine.id})

    def test_staff_form_lists_all_tasks(self):
        form = PhotoReportForm(user=self.admin)
        qs_ids = set(form.fields["help_request"].queryset.values_list("id", flat=True))
        self.assertEqual(qs_ids, {self.mine.id, self.not_mine.id})

    def test_volunteer_cannot_attach_another_volunteers_task_via_post(self):
        self.client.login(username="ps_vol", password="pass12345")
        resp = self.client.post(reverse("photo_reports"), {
            "title": "T", "description": "d", "image": _small_image_file(),
            "help_request": self.not_mine.id,
        })
        # Out-of-queryset choice -> ModelChoiceField "invalid choice" -> form
        # re-renders (200), nothing written.
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(PhotoReport.objects.filter(help_request=self.not_mine).exists())

    def test_volunteer_can_attach_their_own_task(self):
        self.client.login(username="ps_vol", password="pass12345")
        resp = self.client.post(reverse("photo_reports"), {
            "title": "T", "description": "d", "image": _small_image_file(),
            "help_request": self.mine.id,
        })
        self.assertRedirects(resp, reverse("photo_reports"))
        self.assertEqual(PhotoReport.objects.latest("id").help_request_id, self.mine.id)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AiChatRateLimitTests(TestCase):
    """Stage 7: ai_chat_view had no throttle — one logged-in account could run
    up cost / saturate the upstream Groq endpoint."""

    def setUp(self):
        cache.clear()
        self.user = Users.objects.create_user(
            username="ai_user", email="ai_user@example.com", password="pass12345", is_client=True,
        )
        self.client.login(username="ai_user", password="pass12345")

    def tearDown(self):
        cache.clear()

    def test_requests_past_the_limit_get_429(self):
        from myapp.views import AI_CHAT_RATE_LIMIT
        with override_settings(GROQ_API_KEY="", GEMINI_API_KEY=""), mock.patch.dict(
            "os.environ", {"GROQ_API_KEY": "", "GEMINI_API_KEY": ""}
        ):
            for _ in range(AI_CHAT_RATE_LIMIT):
                ok = self.client.post(reverse("ai_chat"), data='{"message":"hi"}', content_type="application/json")
                self.assertEqual(ok.status_code, 200)
            blocked = self.client.post(reverse("ai_chat"), data='{"message":"hi"}', content_type="application/json")
        self.assertEqual(blocked.status_code, 429)


# ===========================================================================
# Stage 8 — production operations & observability
# ===========================================================================


class HealthEndpointTests(TestCase):
    """server/health.py — liveness + readiness. Public, cheap, no secrets."""

    def test_liveness_is_cheap_and_ok(self):
        resp = self.client.get(reverse("health_liveness"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})
        self.assertEqual(resp["Cache-Control"], "no-store")

    def test_liveness_rejects_post(self):
        self.assertEqual(self.client.post(reverse("health_liveness")).status_code, 405)

    def test_readiness_ok_when_database_reachable(self):
        resp = self.client.get(reverse("health_readiness"))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["checks"]["database"], "ok")
        self.assertEqual(body["checks"]["config"], "ok")

    def test_readiness_503_when_database_down(self):
        with mock.patch("server.health._check_database", return_value=False):
            resp = self.client.get(reverse("health_readiness"))
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["status"], "not ready")
        self.assertEqual(resp.json()["checks"]["database"], "error")

    def test_readiness_never_leaks_secrets_or_tracebacks(self):
        with mock.patch(
            "server.health.connections",
            new=mock.MagicMock(**{"__getitem__.side_effect": RuntimeError("boom: password=hunter2")}),
        ):
            resp = self.client.get(reverse("health_readiness"))
        text = resp.content.decode()
        self.assertNotIn(settings.SECRET_KEY, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("password", text)
        self.assertNotIn("hunter2", text)

    @override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "gc_cache"}})
    def test_readiness_reports_cache_error_when_shared_cache_unreachable(self):
        # DatabaseCache with no table -> the probe raises -> reported as "error".
        resp = self.client.get(reverse("health_readiness"))
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.json()["checks"]["cache"], "error")

    def test_readiness_omits_cache_check_for_locmem(self):
        resp = self.client.get(reverse("health_readiness"))
        self.assertNotIn("cache", resp.json()["checks"])


class EmailTimeoutConfigTests(TestCase):
    """Stage 8: Django's SMTP backend has no default timeout — a hung mail
    server would block a worker forever (notify_users sends synchronously)."""

    def test_email_timeout_is_configured_and_finite(self):
        self.assertIsNotNone(getattr(settings, "EMAIL_TIMEOUT", None))
        self.assertGreater(settings.EMAIL_TIMEOUT, 0)
        self.assertLessEqual(settings.EMAIL_TIMEOUT, 30)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class AiChatExternalFailureTests(TestCase):
    """ai_chat_view must degrade to the canned fallback (200, never 5xx) on any
    Groq failure, and the request must not hang — the call has a finite timeout."""

    def setUp(self):
        cache.clear()
        self.user = Users.objects.create_user(
            username="aicf_user", email="aicf_user@example.com", password="pass12345", is_volunteer=True,
        )
        self.client.login(username="aicf_user", password="pass12345")

    def tearDown(self):
        cache.clear()

    def _post(self):
        return self.client.post(
            reverse("ai_chat"), data='{"message": "hello"}', content_type="application/json"
        )

    @override_settings(GROQ_API_KEY="test-key")
    def test_groq_timeout_falls_back(self):
        with mock.patch("myapp.services.ai.client.requests.post", side_effect=requests.Timeout("slow")):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("reply", resp.json())

    @override_settings(GROQ_API_KEY="test-key")
    def test_groq_connection_error_falls_back(self):
        with mock.patch("myapp.services.ai.client.requests.post", side_effect=requests.ConnectionError("no route")):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("reply", resp.json())

    @override_settings(GROQ_API_KEY="test-key")
    def test_groq_malformed_response_falls_back(self):
        bad = mock.Mock()
        bad.raise_for_status.return_value = None
        bad.json.return_value = {"unexpected": "shape"}
        with mock.patch("myapp.services.ai.client.requests.post", return_value=bad):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("reply", resp.json())

    @override_settings(GROQ_API_KEY="test-key")
    def test_groq_request_uses_a_finite_timeout(self):
        captured = {}

        def fake_post(*args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")
            raise requests.Timeout("slow")

        with mock.patch("myapp.services.ai.client.requests.post", side_effect=fake_post):
            self._post()
        self.assertIsNotNone(captured["timeout"])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class StructuredTransitionLoggingTests(TestCase):
    """Stage 8: administrative state changes emit one safe INFO line (IDs only,
    never message contents or secrets)."""

    def setUp(self):
        self.admin, _, _, self.client_a, _ = _donation_users()
        self.a2, self.c2, self.reporter, self.other, self.volunteer = _pet_users()

    def test_donation_transition_logs_ids_only(self):
        d = Donation.objects.create(
            donor=self.client_a, item_name="Плед", category="blankets", quantity=2,
            message="secret note",
        )
        with self.assertLogs("myapp.services.donations", level="INFO") as cm:
            donations.approve_offer(d, actor=self.admin)
        line = "\n".join(cm.output)
        self.assertIn(f"donation #{d.pk}", line)
        self.assertIn("approved", line)
        self.assertNotIn("secret note", line)

    def test_emergency_transition_logs_ids_only(self):
        _, curator, volunteer, _, client_user = _emergency_users()
        task = _active_task(client_user, volunteer)
        report = EmergencyReport.objects.create(help_request=task, volunteer=volunteer)
        with self.assertLogs("myapp.services.emergency", level="INFO") as cm:
            emergency.acknowledge(report, actor=curator)
        self.assertIn(f"emergency #{report.pk}", "\n".join(cm.output))

    def test_pet_moderation_logs(self):
        report = _pet(self.reporter)
        with self.assertLogs("myapp.services.pets", level="INFO") as cm:
            pets.apply_action(report, "close", actor=self.reporter)
        self.assertIn(f"pet report #{report.pk}", "\n".join(cm.output))


class ListViewPaginationBoundsTests(TestCase):
    """Stage 8 / Phase 8: staff list views that grow with the platform are
    paginated, so one request can't render 10k+ rows."""

    def setUp(self):
        self.admin = Users.objects.create_superuser(
            username="pg_admin", email="pg_admin@example.com", password="pass12345"
        )
        self.client.login(username="pg_admin", password="pass12345")
        self.cli = Users.objects.create_user(
            username="pg_cli", email="pg_cli@example.com", password="pass12345", is_client=True, region="dushanbe"
        )
        self.vol = Users.objects.create_user(
            username="pg_vol", email="pg_vol@example.com", password="pass12345", is_volunteer=True, region="dushanbe"
        )

    def test_archive_is_paginated(self):
        for _ in range(3):
            HelpRequest.objects.create(
                client=self.cli, volunteer=self.vol, help_type="grocery", description="x", address="a",
                phone="p", region="dushanbe", status="completed", completed_at=timezone.now(),
            )
        with mock.patch("myapp.views.ARCHIVE_PAGE_SIZE", 2):
            page1 = self.client.get(reverse("completed_tasks")).context["page_obj"]
            page2 = self.client.get(reverse("completed_tasks") + "?page=2").context["page_obj"]
        self.assertEqual(len(page1), 2)
        self.assertEqual(page1.paginator.num_pages, 2)
        self.assertEqual(len(page2), 1)

    def test_people_list_is_paginated(self):
        for i in range(3):
            Users.objects.create_user(
                username=f"pg_extra_{i}", email=f"pg_extra_{i}@example.com", password="pass12345",
                is_volunteer=True, region="dushanbe",
            )
        with mock.patch("myapp.views.PEOPLE_PAGE_SIZE", 2):
            resp = self.client.get(reverse("people_list", args=["volunteer"]))
        self.assertEqual(len(resp.context["page_obj"]), 2)
        self.assertGreaterEqual(resp.context["page_obj"].paginator.num_pages, 2)

    def test_volunteer_applications_is_paginated(self):
        for i in range(3):
            u = Users.objects.create_user(
                username=f"pg_app_{i}", email=f"pg_app_{i}@example.com", password="pass12345", region="dushanbe"
            )
            VolunteerApplication.objects.create(user=u, region="dushanbe")
        with mock.patch("myapp.views.APPLICATIONS_PAGE_SIZE", 2):
            resp = self.client.get(reverse("volunteer_applications") + "?status=all")
        self.assertEqual(len(resp.context["page_obj"]), 2)

    def test_photo_reports_is_paginated(self):
        for i in range(3):
            PhotoReport.objects.create(
                author=self.admin, title=f"r{i}", description="d",
                image=_small_image_file(f"r{i}.png"), region="dushanbe",
            )
        with mock.patch("myapp.views.PHOTO_REPORTS_PAGE_SIZE", 2):
            resp = self.client.get(reverse("photo_reports"))
        self.assertEqual(len(resp.context["page_obj"]), 2)
