"""An admin adds a photo report from the CRM (/myapp/crm/photo-reports/new/):
on behalf of the task's assigned volunteer (no one else) or as themselves,
optionally approved right away; the report
records who uploaded it, is marked "added by an administrator", and goes
through the same review / reward path as a volunteer's own report. Volunteers
and clients keep exactly their old rights."""

from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Users
from myapp.models import HelpRequest, Notification, PhotoReport, UserAchievement
from myapp.services import task_actions
from myapp.tests_photo_report_review import PASSWORD, _jpeg, _make
from myapp.testing import TempMediaRoot

_MEDIA_ROOT = TempMediaRoot("photo-admin-create-tests-")
MARK = "Добавлено администратором"


@override_settings(MEDIA_ROOT=_MEDIA_ROOT.path)
class AdminPhotoReportTestsBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _MEDIA_ROOT.cleanup()

    def setUp(self):
        self.client_user = _make("apr_client", is_client=True)
        self.volunteer = _make("apr_vol", is_volunteer=True)
        self.other_volunteer = _make("apr_vol2", is_volunteer=True)
        self.curator = _make("apr_curator", is_curator=True)
        self.admin = Users.objects.create_superuser(username="apr_admin", email="apr_admin@example.com", password=PASSWORD)
        self.task = self._task(volunteer=self.volunteer, status="active")
        self.create_url = reverse("crm_photo_report_create")

    def _task(self, volunteer=None, status="pending"):
        return HelpRequest.objects.create(
            client=self.client_user, volunteer=volunteer, status=status, help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )

    def _post(self, task, volunteer, **extra):
        data = {
            "help_request": task.pk, "volunteer": volunteer.pk if volunteer else "",
            "title": "Привезли продукты", "description": "Всё на месте", "image": _jpeg(), **extra,
        }
        return self.client.post(self.create_url, data)

    def _points(self, user):
        user.profile.refresh_from_db()
        return user.profile.rating

    def _approve(self, report):
        self.client.force_login(self.admin)
        return self.client.post(reverse("crm_photo_report_review", args=[report.pk]), {"action": "approve"})


class AdminCreatesForVolunteerTests(AdminPhotoReportTestsBase):
    def test_form_preselects_the_task_and_its_volunteer(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.create_url + f"?task={self.task.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "myapp/crm_photo_report_create.html")
        form = response.context["form"]
        self.assertEqual(
            (form.initial["help_request"], form.initial["volunteer"]), (self.task.pk, self.volunteer.pk)
        )
        html = response.content.decode()
        self.assertIn('enctype="multipart/form-data"', html)
        # Each task option tells the JS whom to pre-select.
        self.assertRegex(
            html, rf'<option value="{self.task.pk}" selected data-volunteer="{self.volunteer.pk}" data-volunteer-label="apr_vol">'
        )
        # Only the task's volunteer is offered (+ "as the administrator").
        self.assertEqual(list(form.fields["volunteer"].queryset), [self.volunteer])
        self.assertNotIn("apr_vol2", html)
        # "Approve right away" is on by default.
        self.assertTrue(form.fields["approve_now"].initial)
        self.assertRegex(html, r'<input type="checkbox" name="approve_now"[^>]*checked')

    def test_admin_creates_a_report_for_the_assigned_volunteer(self):
        task_actions.complete(self.volunteer, self.task)  # completed, reward waits for an approved report
        before = self._points(self.volunteer)
        self.client.force_login(self.admin)
        response = self._post(self.task, self.volunteer)

        report = PhotoReport.objects.get()
        self.assertRedirects(response, reverse("crm_photo_report_detail", args=[report.pk]))
        self.assertEqual(
            (report.author, report.uploaded_by, report.status, report.help_request, report.region),
            (self.volunteer, self.admin, PhotoReport.PENDING, self.task, "dushanbe"),
        )
        self.assertTrue(report.added_by_admin)
        self.assertRegex(report.image.name, r"^reports/[0-9a-f]{32}\.jpg$")  # cleaned like any upload
        self.assertEqual(self._points(self.volunteer), before)  # nothing paid before the review

        # The same review path as a volunteer's own report: approval pays the volunteer once.
        with mock.patch("myapp.services.achievements.award_safely") as award:
            self._approve(report)
        award.assert_called_once_with(self.volunteer, "task_completed")
        self.assertEqual(self._points(self.volunteer), before + 3)
        self.task.refresh_from_db()
        self.assertFalse(self.task.reward_pending)
        self.assertTrue(Notification.objects.filter(recipient=self.volunteer, type="photo_report_approved").exists())
        self.assertEqual(self._points(self.admin), 0)

    def test_the_mark_is_shown_in_the_crm_and_on_the_public_list(self):
        self.client.force_login(self.admin)
        self._post(self.task, self.volunteer)
        report = PhotoReport.objects.get()
        for url in (reverse("crm_photo_reports"), reverse("crm_photo_report_detail", args=[report.pk])):
            self.assertContains(self.client.get(url), MARK)
        self._approve(report)
        self.client.force_login(self.client_user)
        self.assertContains(self.client.get(reverse("photo_reports")), MARK)

    def test_another_volunteer_cannot_be_picked(self):
        self.client.force_login(self.admin)
        response = self._post(self.task, self.other_volunteer)
        self.assertEqual(response.status_code, 200)
        self.assertIn("volunteer", response.context["form"].errors)
        self.assertFalse(PhotoReport.objects.exists())

    def test_volunteer_list_follows_the_posted_task(self):
        # A task without a volunteer offers nobody — its former volunteer is refused too.
        free_task = self._task()
        self.client.force_login(self.admin)
        response = self._post(free_task, self.volunteer)
        self.assertIn("volunteer", response.context["form"].errors)
        self.assertEqual(list(response.context["form"].fields["volunteer"].queryset), [])
        self.assertFalse(PhotoReport.objects.exists())

    def test_approve_right_away_runs_the_normal_approval(self):
        task_actions.complete(self.volunteer, self.task)
        before = self._points(self.volunteer)
        self.client.force_login(self.admin)
        with mock.patch("myapp.services.achievements.award_safely") as award:
            response = self._post(self.task, self.volunteer, approve_now="on")
        report = PhotoReport.objects.get()
        self.assertRedirects(response, reverse("crm_photo_report_detail", args=[report.pk]))
        self.assertEqual((report.status, report.reviewed_by), (PhotoReport.APPROVED, self.admin))
        award.assert_called_once_with(self.volunteer, "task_completed")
        self.assertEqual(self._points(self.volunteer), before + 3)
        self.task.refresh_from_db()
        self.assertFalse(self.task.reward_pending)
        self.assertTrue(Notification.objects.filter(recipient=self.volunteer, type="photo_report_approved").exists())
        self.assertEqual(self._points(self.admin), 0)

    def test_list_offers_the_add_button_to_admins_only(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("crm_photo_reports")), self.create_url)
        self.client.force_login(self.curator)
        self.assertNotContains(self.client.get(reverse("crm_photo_reports")), self.create_url)

    def test_task_page_links_admins_to_the_form(self):
        link = f"{self.create_url}?task={self.task.pk}"
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("task_detail", args=[self.task.pk])), link)
        self.client.force_login(self.volunteer)
        self.assertNotContains(self.client.get(reverse("task_detail", args=[self.task.pk])), link)


class AdminCreatesWithoutVolunteerTests(AdminPhotoReportTestsBase):
    def test_task_without_volunteer_defaults_to_the_admin(self):
        task = self._task()
        self.client.force_login(self.admin)
        response = self.client.get(self.create_url + f"?task={task.pk}")
        initial = response.context["form"].initial
        self.assertEqual((initial["help_request"], initial["volunteer"]), (task.pk, None))

    def test_admin_creates_a_report_as_themselves_and_earns_nothing(self):
        task = self._task()  # pending, nobody assigned
        self.client.force_login(self.admin)
        response = self._post(task, None)
        report = PhotoReport.objects.get()
        self.assertRedirects(response, reverse("crm_photo_report_detail", args=[report.pk]))
        self.assertEqual((report.author, report.uploaded_by), (self.admin, self.admin))
        self.assertTrue(report.added_by_admin)
        self.assertContains(self.client.get(reverse("crm_photo_report_detail", args=[report.pk])), MARK)

        with mock.patch("myapp.services.achievements.award_safely") as award:
            self._approve(report)
        report.refresh_from_db()
        self.assertEqual(report.status, PhotoReport.APPROVED)
        award.assert_not_called()
        self.assertEqual(self._points(self.admin), 0)
        self.assertFalse(UserAchievement.objects.filter(user=self.admin).exists())

    def test_report_as_admin_on_a_volunteers_task_pays_nobody(self):
        task_actions.complete(self.volunteer, self.task)
        self.client.force_login(self.admin)
        self._post(self.task, None)
        self._approve(PhotoReport.objects.get())
        self.assertEqual((self._points(self.admin), self._points(self.volunteer)), (0, 0))
        self.assertFalse(UserAchievement.objects.filter(user=self.admin).exists())
        self.task.refresh_from_db()
        self.assertTrue(self.task.reward_pending)  # still waits for the volunteer's report


    def test_approve_right_away_as_the_admin_pays_and_tells_nobody(self):
        task = self._task()
        self.client.force_login(self.admin)
        with mock.patch("myapp.services.achievements.award_safely") as award:
            self._post(task, None, approve_now="on")
        report = PhotoReport.objects.get()
        self.assertEqual((report.author, report.status), (self.admin, PhotoReport.APPROVED))
        award.assert_not_called()
        self.assertEqual(self._points(self.admin), 0)
        self.assertFalse(UserAchievement.objects.filter(user=self.admin).exists())
        # No notice to the admin about their own decision.
        self.assertFalse(Notification.objects.filter(recipient=self.admin).exists())


class FormValidationTests(AdminPhotoReportTestsBase):
    def test_a_task_is_required(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.create_url, {"title": "T", "image": _jpeg()})
        self.assertEqual(response.status_code, 200)
        self.assertIn("help_request", response.context["form"].errors)
        self.assertFalse(PhotoReport.objects.exists())

    def test_only_active_volunteers_can_be_picked(self):
        self.client.force_login(self.admin)
        for not_a_volunteer in (self.client_user, self.curator):
            response = self._post(self.task, not_a_volunteer)
            self.assertIn("volunteer", response.context["form"].errors)
        self.assertFalse(PhotoReport.objects.exists())

    def test_a_non_image_is_rejected(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_login(self.admin)
        response = self._post(self.task, self.volunteer, image=SimpleUploadedFile("x.jpg", b"not an image", "image/jpeg"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("image", response.context["form"].errors)
        self.assertFalse(PhotoReport.objects.exists())


class RightsUnchangedTests(AdminPhotoReportTestsBase):
    def test_client_gets_403(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(self.create_url).status_code, 403)
        self.assertEqual(self._post(self.task, self.volunteer).status_code, 403)
        self.assertFalse(PhotoReport.objects.exists())

    def test_volunteer_and_curator_get_403(self):
        for user in (self.volunteer, self.curator):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.create_url).status_code, 403)
            self.assertEqual(self._post(self.task, self.volunteer).status_code, 403)
        self.assertFalse(PhotoReport.objects.exists())

    def test_anonymous_goes_to_login(self):
        response = self.client.get(self.create_url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.create_url}", fetch_redirect_response=False)

    def test_volunteer_cannot_report_on_someone_elses_task(self):
        foreign = self._task(volunteer=self.other_volunteer, status="active")
        self.client.force_login(self.volunteer)
        form = self.client.get(reverse("photo_reports")).context["form"]
        self.assertNotIn(foreign, form.fields["help_request"].queryset)
        response = self.client.post(reverse("photo_reports"), {
            "title": "Чужой", "description": "d", "image": _jpeg(), "help_request": foreign.pk,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("help_request", response.context["form"].errors)
        self.assertFalse(PhotoReport.objects.exists())

    def test_volunteer_own_report_records_the_uploader_without_the_mark(self):
        self.client.force_login(self.volunteer)
        response = self.client.post(reverse("photo_reports"), {
            "title": "Свой", "description": "d", "image": _jpeg(), "help_request": self.task.pk,
        })
        self.assertRedirects(response, reverse("photo_reports"))
        report = PhotoReport.objects.get()
        self.assertEqual((report.author, report.uploaded_by), (self.volunteer, self.volunteer))
        self.assertFalse(report.added_by_admin)
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("crm_photo_reports")), MARK)

    def test_client_still_cannot_publish_on_the_reports_page(self):
        self.client.force_login(self.client_user)
        response = self.client.post(reverse("photo_reports"), {"title": "T", "description": "d", "image": _jpeg()})
        self.assertRedirects(response, reverse("photo_reports"))
        self.assertFalse(PhotoReport.objects.exists())


class DjangoAdminTests(AdminPhotoReportTestsBase):
    def test_django_admin_add_records_the_uploader(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("admin:myapp_photoreport_add"), {
            "author": self.volunteer.pk, "title": "Из админки", "description": "", "image": _jpeg(),
            "region": "dushanbe", "help_request": self.task.pk, "image_variants": "{}", "event_date": "2026-10-01",
        })
        self.assertEqual(response.status_code, 302, response.content[:2000])
        report = PhotoReport.objects.get()
        self.assertEqual((report.author, report.uploaded_by), (self.volunteer, self.admin))
        self.assertTrue(report.added_by_admin)
