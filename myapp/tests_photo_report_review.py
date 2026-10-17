"""Photo-report review in the CRM: pending → approved | rejected, the task's
reward only after approval, author notifications, staff-only access, and the
public pages showing approved reports only."""

import io
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from PIL import Image

from accounts.models import Users
from myapp.models import HelpRequest, Notification, PhotoReport
from myapp.services import photo_reports, rating, task_actions
from myapp.testing import TempMediaRoot

PASSWORD = "Photo-Review-2026!"
_MEDIA_ROOT = TempMediaRoot("photo-review-tests-")  # fork-safe (myapp/testing.py)


def _jpeg(name="report.jpg", size=(64, 48)):
    buf = io.BytesIO()
    img = Image.new("RGB", size, (40, 120, 200))
    exif = Image.Exif()
    exif[0x8825] = {2: (38.0, 33.0, 0.0)}  # a GPS IFD — must not survive the upload
    img.save(buf, "JPEG", exif=exif)
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def _make(username, **flags):
    return Users.objects.create_user(
        username=username, email=f"{username}@example.com", password=PASSWORD, region="dushanbe", **flags
    )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT.path)
class PhotoReportReviewTestsBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _MEDIA_ROOT.cleanup()

    def setUp(self):
        self.client_user = _make("pr_client", is_client=True)
        self.volunteer = _make("pr_vol", is_volunteer=True)
        self.other_volunteer = _make("pr_vol2", is_volunteer=True)
        self.curator = _make("pr_curator", is_curator=True)
        self.admin = Users.objects.create_superuser(username="pr_admin", email="pr_admin@example.com", password=PASSWORD)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, status="active", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )

    def _login(self, user):
        self.client.force_login(user)

    def _report(self, author=None, task=None, **extra):
        return PhotoReport.objects.create(
            author=author or self.volunteer, title="Привезли продукты", image=_jpeg(),
            help_request=task if task is not None else self.task, region="dushanbe", **extra,
        )

    def _points(self, user=None):
        (user or self.volunteer).profile.refresh_from_db()
        return (user or self.volunteer).profile.rating


class ModelAndMigrationTests(PhotoReportReviewTestsBase):
    def test_new_report_starts_pending(self):
        self.assertEqual(self._report().status, PhotoReport.PENDING)

    def test_approve_and_reject_are_one_way(self):
        report = self._report()
        report.approve(self.curator)
        self.assertEqual((report.status, report.reviewed_by), (PhotoReport.APPROVED, self.curator))
        self.assertIsNotNone(report.reviewed_at)
        with self.assertRaises(ValueError):
            report.reject(self.curator, "late")

    def test_reject_needs_a_reason(self):
        report = self._report()
        with self.assertRaises(ValueError):
            report.reject(self.curator, "   ")
        report.reject(self.curator, "Фото размыто")
        report.refresh_from_db()
        self.assertEqual((report.status, report.rejection_reason), (PhotoReport.REJECTED, "Фото размыто"))

    def test_migration_marks_existing_reports_approved(self):
        from importlib import import_module

        migration = import_module("myapp.migrations.0028_photo_report_review").Migration
        add = next(op for op in migration.operations if getattr(op, "name", "") == "status" and op.__class__.__name__ == "AddField")
        alter = next(op for op in migration.operations if getattr(op, "name", "") == "status" and op.__class__.__name__ == "AlterField")
        self.assertEqual(add.field.default, "approved")  # fills every existing row
        self.assertEqual(alter.field.default, "pending")  # new uploads


class RewardOnlyAfterApprovalTests(PhotoReportReviewTestsBase):
    def test_completion_without_approved_report_pays_nothing(self):
        before = self._points()
        outcome = task_actions.complete(self.volunteer, self.task)
        self.task.refresh_from_db()
        self.assertTrue(outcome.ok and outcome.reward_pending)
        self.assertEqual(self.task.status, "completed")
        self.assertTrue(self.task.reward_pending)
        self.assertEqual(self._points(), before)

    def test_approval_after_completion_pays_once(self):
        before = self._points()
        task_actions.complete(self.volunteer, self.task)
        report = self._report()
        with mock.patch("myapp.services.achievements.award_safely") as award:
            outcome = photo_reports.approve(report, actor=self.curator)
        self.assertTrue(outcome.ok and outcome.credited)
        award.assert_called_once_with(self.volunteer, "task_completed")
        self.assertEqual(self._points(), before + 3)
        self.task.refresh_from_db()
        self.assertFalse(self.task.reward_pending)
        # A second approved report on the same task pays nothing more.
        second = self._report()
        self.assertFalse(photo_reports.approve(second, actor=self.curator).credited)
        self.assertEqual(self._points(), before + 3)

    def test_rejection_pays_nothing(self):
        before = self._points()
        task_actions.complete(self.volunteer, self.task)
        photo_reports.reject(self._report(), actor=self.curator, reason="Не видно работы")
        self.task.refresh_from_db()
        self.assertTrue(self.task.reward_pending)
        self.assertEqual(self._points(), before)

    def test_report_approved_before_completion_pays_at_completion(self):
        before = self._points()
        photo_reports.approve(self._report(), actor=self.curator)
        self.assertEqual(self._points(), before)  # the task is still active
        outcome = task_actions.complete(self.volunteer, self.task)
        self.assertFalse(outcome.reward_pending)
        self.assertEqual(self._points(), before + 3)

    def test_report_by_someone_else_does_not_release_the_reward(self):
        task_actions.complete(self.volunteer, self.task)
        photo_reports.approve(self._report(author=self.other_volunteer), actor=self.curator)
        self.task.refresh_from_db()
        self.assertTrue(self.task.reward_pending)

    def test_public_rating_counts_the_task_only_after_approval(self):
        task_actions.complete(self.volunteer, self.task)
        row = next(r for r in rating.build()["rows"] if r["user_id"] == self.volunteer.pk)
        self.assertEqual(row["completed"], 0)
        photo_reports.approve(self._report(), actor=self.curator)
        row = next(r for r in rating.build()["rows"] if r["user_id"] == self.volunteer.pk)
        self.assertEqual(row["completed"], 1)

    def test_badge_counters_skip_unapproved_tasks(self):
        from myapp.services import achievements

        task_actions.complete(self.volunteer, self.task)
        self.assertEqual(achievements.counters(self.volunteer)["tasks"], 0)
        photo_reports.approve(self._report(), actor=self.curator)
        self.assertEqual(achievements.counters(self.volunteer)["tasks"], 1)

    def test_tasks_completed_before_review_keep_counting(self):
        HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, status="completed", help_type="grocery",
            description="old", address="a", phone="+992901234567", region="dushanbe",
        )  # reward_pending defaults to False, like every migrated task
        from myapp.services import achievements

        self.assertEqual(achievements.counters(self.volunteer)["tasks"], 1)


class NotificationTests(PhotoReportReviewTestsBase):
    def test_author_notified_on_approval(self):
        report = self._report()
        photo_reports.approve(report, actor=self.curator)
        self.assertTrue(Notification.objects.filter(recipient=self.volunteer, type="photo_report_approved").exists())

    def test_author_notified_on_rejection_with_reason_and_upload_link(self):
        report = self._report()
        photo_reports.reject(report, actor=self.curator, reason="Фото размыто")
        note = Notification.objects.get(recipient=self.volunteer, type="photo_report_rejected")
        self.assertEqual(note.params["message"], "Фото размыто")
        self.assertIn(f"?task={self.task.pk}", note.link)

    def test_rejection_goes_to_telegram_by_default(self):
        from myapp.services.notify import NOTIFICATION_TYPES, TELEGRAM

        for code in ("photo_report_approved", "photo_report_rejected"):
            self.assertIn(TELEGRAM, NOTIFICATION_TYPES[code].defaults)

    def test_telegram_text_carries_the_reason(self):
        from myapp.services import notify_texts

        for lang in notify_texts.LANGS:
            _subject, body = notify_texts.render_channel("photo_report_rejected", lang, {"title": "T", "message": "размыто"})
            self.assertIn("размыто", body)


class CrmAccessAndFlowTests(PhotoReportReviewTestsBase):
    def test_list_and_detail_are_staff_only(self):
        report = self._report()
        urls = [reverse("crm_photo_reports"), reverse("crm_photo_report_detail", args=[report.pk])]
        for user in (self.volunteer, self.client_user):
            self._login(user)
            for url in urls:
                self.assertRedirects(self.client.get(url), reverse("dashboard"), msg_prefix=user.username)
            resp = self.client.post(reverse("crm_photo_report_review", args=[report.pk]), {"action": "approve"})
            self.assertRedirects(resp, reverse("dashboard"))
        report.refresh_from_db()
        self.assertEqual(report.status, PhotoReport.PENDING)
        self.client.logout()
        resp = self.client.get(urls[0])
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)
        for user in (self.curator, self.admin):
            self._login(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 200, user.username)

    def test_list_defaults_to_pending_newest_first(self):
        old = self._report()
        new = self._report()
        done = self._report(status=PhotoReport.APPROVED)
        self._login(self.curator)
        resp = self.client.get(reverse("crm_photo_reports"))
        ids = [r.pk for r in resp.context["page_obj"]]
        self.assertEqual(ids, [new.pk, old.pk])
        self.assertNotIn(done.pk, ids)
        resp = self.client.get(reverse("crm_photo_reports") + "?status=all")
        self.assertEqual(len(resp.context["page_obj"].object_list), 3)

    def test_filters_by_volunteer_and_task(self):
        mine = self._report()
        other_task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.other_volunteer, status="active", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )
        theirs = self._report(author=self.other_volunteer, task=other_task)
        self._login(self.curator)
        base = reverse("crm_photo_reports")
        self.assertEqual([r.pk for r in self.client.get(f"{base}?volunteer={self.volunteer.pk}").context["page_obj"]], [mine.pk])
        self.assertEqual([r.pk for r in self.client.get(f"{base}?task={other_task.pk}").context["page_obj"]], [theirs.pk])
        # Garbage never 500s.
        self.assertEqual(self.client.get(f"{base}?status=zzz&from=nope&task=x").status_code, 200)

    def test_detail_shows_photo_task_volunteer_client(self):
        report = self._report()
        self._login(self.curator)
        html = self.client.get(reverse("crm_photo_report_detail", args=[report.pk])).content.decode()
        for needle in (report.image.url, f"#{self.task.pk}", self.volunteer.username, self.client_user.username,
                       "data-pr-lightbox", 'name="reason"', "required"):
            self.assertIn(needle, html)

    def test_approve_through_the_view(self):
        task_actions.complete(self.volunteer, self.task)
        report = self._report()
        before = self._points()
        self._login(self.curator)
        resp = self.client.post(reverse("crm_photo_report_review", args=[report.pk]), {"action": "approve"})
        self.assertRedirects(resp, reverse("crm_photo_reports"))
        report.refresh_from_db()
        self.assertEqual((report.status, report.reviewed_by), (PhotoReport.APPROVED, self.curator))
        self.assertEqual(self._points(), before + 3)

    def test_reject_through_the_view_requires_a_reason(self):
        report = self._report()
        self._login(self.admin)
        url = reverse("crm_photo_report_review", args=[report.pk])
        resp = self.client.post(url, {"action": "reject", "reason": "  "})
        self.assertRedirects(resp, reverse("crm_photo_report_detail", args=[report.pk]))
        report.refresh_from_db()
        self.assertEqual(report.status, PhotoReport.PENDING)
        self.client.post(url, {"action": "reject", "reason": "Не видно выполненной работы"})
        report.refresh_from_db()
        self.assertEqual((report.status, report.rejection_reason), (PhotoReport.REJECTED, "Не видно выполненной работы"))
        self.assertEqual(report.reviewed_by, self.admin)

    def test_panel_menu_shows_pending_count(self):
        self._report()
        self._report()
        self._login(self.curator)
        html = self.client.get(reverse("admin_panel")).content.decode()
        self.assertIn(reverse("crm_photo_reports"), html)
        self.assertIn('class="panel-badge"', html)
        self.assertEqual(self.client.get(reverse("admin_panel")).context["photo_reports_pending"], 2)


class UploadAndVisibilityTests(PhotoReportReviewTestsBase):
    def test_upload_is_pending_and_still_stripped_and_downscaled(self):
        from myapp.services import images

        self._login(self.volunteer)
        big = _jpeg("Zamira_home.jpg", size=(images.MAX_ORIGINAL_SIDE + 400, 300))
        resp = self.client.post(reverse("photo_reports"), {
            "title": "Готово", "description": "", "region": "dushanbe", "help_request": self.task.pk, "image": big,
        })
        self.assertRedirects(resp, reverse("photo_reports"))
        report = PhotoReport.objects.get(author=self.volunteer)
        self.assertEqual(report.status, PhotoReport.PENDING)
        self.assertNotIn("Zamira", report.image.name)
        with Image.open(report.image.path) as img:
            self.assertEqual(max(img.size), images.MAX_ORIGINAL_SIDE)
            self.assertEqual(len(img.getexif()), 0)

    def test_public_pages_show_approved_only(self):
        pending = self._report()
        pending.title = "PENDING-ONE"
        pending.save()
        approved = self._report(status=PhotoReport.APPROVED)
        approved.title = "APPROVED-ONE"
        approved.save()
        landing = self.client.get(reverse("home")).content.decode()
        self.assertIn("APPROVED-ONE", landing)
        self.assertNotIn("PENDING-ONE", landing)
        self._login(self.other_volunteer)
        listing = self.client.get(reverse("photo_reports")).content.decode()
        self.assertIn("APPROVED-ONE", listing)
        self.assertNotIn("PENDING-ONE", listing)
        self._login(self.volunteer)  # the author sees their own, with its status
        listing = self.client.get(reverse("photo_reports")).content.decode()
        self.assertIn("PENDING-ONE", listing)
        self.assertIn('data-i18n="pr.status_pending"', listing)

    def test_completing_sends_the_volunteer_to_the_report_form(self):
        self._login(self.volunteer)
        resp = self.client.post(reverse("complete_task", args=[self.task.pk]))
        self.assertRedirects(resp, reverse("photo_reports") + f"?task={self.task.pk}#report-form", fetch_redirect_response=False)
        form = self.client.get(reverse("photo_reports") + f"?task={self.task.pk}").context["form"]
        self.assertEqual(form["help_request"].value(), self.task.pk)

    def test_volunteer_dashboard_shows_report_status(self):
        report = self._report()
        photo_reports.reject(report, actor=self.curator, reason="Фото размыто")
        self._login(self.volunteer)
        html = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('data-i18n="pr.mine_title"', html)
        self.assertIn('data-i18n="pr.status_rejected"', html)
        self.assertIn("Фото размыто", html)


class DeleteTests(PhotoReportReviewTestsBase):
    """Staff delete any report: row + files gone; an approved report that paid
    its task takes the reward back and the task waits for a new report."""

    def _with_variants(self, report):
        from myapp.services import images

        report.image_variants = images.make_webp_variants(report.image)
        report.save(update_fields=["image_variants"])
        storage = report.image.storage
        paths = [report.image.name, *report.image_variants.values()]
        self.assertTrue(all(storage.exists(path) for path in paths))
        return storage, paths

    def _credited(self):
        """Task completed, report approved → points + first_help paid."""
        from myapp.models import UserAchievement

        task_actions.complete(self.volunteer, self.task)
        report = self._report()
        photo_reports.approve(report, actor=self.curator)
        self.task.refresh_from_db()
        self.assertFalse(self.task.reward_pending)
        self.assertTrue(UserAchievement.objects.filter(user=self.volunteer, achievement__code="first_help").exists())
        return report

    def test_delete_pending_report_removes_row_and_files(self):
        report = self._report()
        storage, paths = self._with_variants(report)
        before = self._points()
        self._login(self.curator)
        with self.assertLogs("myapp.services.photo_reports", "INFO") as logs:
            resp = self.client.post(reverse("crm_photo_report_delete", args=[report.pk]))
        self.assertRedirects(resp, reverse("crm_photo_reports"))
        self.assertFalse(PhotoReport.objects.filter(pk=report.pk).exists())
        self.assertFalse(any(storage.exists(path) for path in paths))
        self.assertEqual(self._points(), before)
        line = next(m for m in logs.output if "deleted by" in m)
        self.assertIn(f"photo report #{report.pk} (task #{self.task.pk}) deleted by user #{self.curator.pk}", line)
        self.assertNotIn(report.title, " ".join(logs.output))  # IDs only
        note = Notification.objects.get(recipient=self.volunteer, type="photo_report_deleted")
        self.assertIn(f"?task={self.task.pk}", note.link)

    def test_delete_approved_report_revokes_points_badges_and_rating(self):
        from myapp.models import UserAchievement

        start = self._points()
        report = self._credited()
        self.assertEqual(self._points(), start + 3)
        row = next(r for r in rating.build()["rows"] if r["user_id"] == self.volunteer.pk)
        self.assertEqual(row["completed"], 1)
        self._login(self.admin)
        self.client.post(reverse("crm_photo_report_delete", args=[report.pk]))
        self.assertFalse(PhotoReport.objects.filter(pk=report.pk).exists())
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "completed")
        self.assertTrue(self.task.reward_pending)  # waits for a new photo report
        self.assertEqual(self._points(), start)
        self.assertFalse(UserAchievement.objects.filter(user=self.volunteer, achievement__code="first_help").exists())
        row = next(r for r in rating.build()["rows"] if r["user_id"] == self.volunteer.pk)
        self.assertEqual(row["completed"], 0)
        self.assertTrue(Notification.objects.filter(recipient=self.volunteer, type="photo_report_deleted").exists())
        # A new report, once approved, pays the task again — once.
        photo_reports.approve(self._report(), actor=self.curator)
        self.assertEqual(self._points(), start + 3)
        self.assertTrue(UserAchievement.objects.filter(user=self.volunteer, achievement__code="first_help").exists())

    def test_reward_stays_while_another_approved_report_covers_the_task(self):
        report = self._credited()
        photo_reports.approve(self._report(), actor=self.curator)  # a second approved report
        points = self._points()
        outcome = photo_reports.delete(report, actor=self.curator)
        self.assertTrue(outcome.ok)
        self.assertFalse(outcome.reward_revoked)
        self.task.refresh_from_db()
        self.assertFalse(self.task.reward_pending)
        self.assertEqual(self._points(), points)

    def test_only_badges_earned_by_that_task_are_revoked(self):
        from myapp.models import UserAchievement

        HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, status="completed", help_type="grocery",
            description="old", address="a", phone="+992901234567", region="dushanbe",
        )  # an earlier credited task already earned first_help
        from myapp.services import achievements

        achievements.evaluate(self.volunteer, send_notifications=False)
        report = self._credited()
        photo_reports.delete(report, actor=self.curator)
        self.assertTrue(UserAchievement.objects.filter(user=self.volunteer, achievement__code="first_help").exists())

    def test_points_never_go_below_zero(self):
        report = self._credited()
        self.volunteer.profile.rating = 1
        self.volunteer.profile.save(update_fields=["rating"])
        photo_reports.delete(report, actor=self.curator)
        self.assertEqual(self._points(), 0)

    def test_deleting_twice_is_harmless(self):
        report = self._credited()
        self.assertTrue(photo_reports.delete(report, actor=self.curator).reward_revoked)
        again = photo_reports.delete(report, actor=self.curator)
        self.assertEqual((again.ok, again.code), (False, photo_reports.NOT_FOUND))
        self._login(self.curator)
        resp = self.client.post(reverse("crm_photo_report_delete", args=[report.pk]))
        self.assertEqual(resp.status_code, 404)

    def test_delete_is_staff_only_and_post_only(self):
        report = self._credited()
        url = reverse("crm_photo_report_delete", args=[report.pk])
        points = self._points()
        for user in (self.volunteer, self.other_volunteer, self.client_user):
            self._login(user)
            self.assertRedirects(self.client.post(url), reverse("dashboard"), msg_prefix=user.username)
        self.client.logout()
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)
        self._login(self.curator)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertTrue(PhotoReport.objects.filter(pk=report.pk).exists())
        self.assertEqual(self._points(), points)

    def test_delete_buttons_ask_first(self):
        report = self._report(status=PhotoReport.APPROVED)
        self._login(self.curator)
        url = reverse("crm_photo_report_delete", args=[report.pk])
        for page in (reverse("crm_photo_reports") + "?status=all", reverse("crm_photo_report_detail", args=[report.pk])):
            html = self.client.get(page).content.decode()
            self.assertIn(f'action="{url}" data-confirm="pr.delete_confirm"', html)

    def test_list_filters_survive_the_delete(self):
        report = self._report()
        self._login(self.curator)
        back = reverse("crm_photo_reports") + "?status=all&page=1"
        resp = self.client.post(reverse("crm_photo_report_delete", args=[report.pk]), {"next": back})
        self.assertRedirects(resp, back)
        other = self._report()
        resp = self.client.post(reverse("crm_photo_report_delete", args=[other.pk]), {"next": "https://evil.example/"})
        self.assertRedirects(resp, reverse("crm_photo_reports"))

    def test_django_admin_delete_takes_the_same_path(self):
        report = self._credited()
        self._login(self.admin)
        resp = self.client.post(
            reverse("admin:myapp_photoreport_delete", args=[report.pk]), {"post": "yes"}
        )
        self.assertEqual(resp.status_code, 302)
        self.task.refresh_from_db()
        self.assertTrue(self.task.reward_pending)

    def test_deleted_notice_goes_to_telegram_in_every_language(self):
        from myapp.services import notify_texts
        from myapp.services.notify import NOTIFICATION_TYPES, TELEGRAM

        self.assertIn(TELEGRAM, NOTIFICATION_TYPES["photo_report_deleted"].defaults)
        for lang in notify_texts.LANGS:
            _subject, body = notify_texts.render_channel("photo_report_deleted", lang, {"title": "Т1"})
            self.assertIn("Т1", body)


class BulkDeleteTests(PhotoReportReviewTestsBase):
    """The list's checkboxes + "Delete selected": every report deleted exactly
    like a single delete, one confirmation with the count, one notice per author."""

    def _credited(self):
        task_actions.complete(self.volunteer, self.task)
        report = self._report()
        photo_reports.approve(report, actor=self.curator)
        return report

    def test_bulk_delete_of_several_reports(self):
        from myapp.models import UserAchievement
        from myapp.services import images

        start = self._points()
        approved = self._credited()
        pending = self._report()
        other_task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.other_volunteer, status="active", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )
        theirs = self._report(author=self.other_volunteer, task=other_task)
        keep = self._report()
        storage = approved.image.storage
        files = []
        for report in (approved, pending, theirs):
            report.image_variants = images.make_webp_variants(report.image)
            report.save(update_fields=["image_variants"])
            files += [report.image.name, *report.image_variants.values()]
        self.assertTrue(all(storage.exists(f) for f in files))
        Notification.objects.all().delete()

        self._login(self.admin)
        back = reverse("crm_photo_reports") + "?status=all"
        with self.assertLogs("myapp.services.photo_reports", "INFO") as logs:
            resp = self.client.post(reverse("crm_photo_reports_bulk_delete"), {
                "ids": [approved.pk, pending.pk, theirs.pk, 999999], "next": back,
            }, follow=True)
        self.assertRedirects(resp, back)
        self.assertContains(resp, 'data-i18n="flash.reports_bulk_deleted"')
        self.assertEqual(list(PhotoReport.objects.values_list("pk", flat=True)), [keep.pk])
        self.assertFalse(any(storage.exists(f) for f in files))
        # The approved one took its reward back; the task waits for a new report.
        self.task.refresh_from_db()
        self.assertTrue(self.task.reward_pending)
        self.assertEqual(self._points(), start)
        self.assertFalse(UserAchievement.objects.filter(user=self.volunteer, achievement__code="first_help").exists())
        # One notice per author: the "many" variant for two reports, the plain one for one.
        mine = Notification.objects.get(recipient=self.volunteer, type="photo_report_deleted")
        self.assertEqual((mine.title_key, mine.params["count"]), ("ntf.photo_report_deleted.many.title", 2))
        theirs_note = Notification.objects.get(recipient=self.other_volunteer, type="photo_report_deleted")
        self.assertEqual(theirs_note.title_key, "ntf.photo_report_deleted.title")
        self.assertIn("bulk delete by user", " ".join(logs.output))
        self.assertIn("3 deleted, 1 rewards revoked, 1 already gone", " ".join(logs.output))

    def test_list_has_checkboxes_and_one_confirmation_with_the_count(self):
        reports = [self._report(), self._report(status=PhotoReport.APPROVED)]
        self._login(self.curator)
        html = self.client.get(reverse("crm_photo_reports") + "?status=all").content.decode()
        self.assertIn(f'action="{reverse("crm_photo_reports_bulk_delete")}"', html)
        for report in reports:
            self.assertIn(f'name="ids" value="{report.pk}" form="prBulkForm"', html)
        self.assertIn("data-pr-select-all", html)
        self.assertIn("window.confirm(question)", html)
        self.assertIn("({n})", html)  # the count goes into the one question

    def test_nothing_selected(self):
        report = self._report()
        self._login(self.curator)
        resp = self.client.post(reverse("crm_photo_reports_bulk_delete"), {"ids": ["x"]}, follow=True)
        self.assertContains(resp, 'data-i18n="flash.reports_bulk_none"')
        self.assertTrue(PhotoReport.objects.filter(pk=report.pk).exists())

    def test_bulk_delete_is_staff_only(self):
        report = self._report()
        url = reverse("crm_photo_reports_bulk_delete")
        for user in (self.volunteer, self.client_user):
            self._login(user)
            self.assertRedirects(self.client.post(url, {"ids": [report.pk]}), reverse("dashboard"))
        self._login(self.curator)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertTrue(PhotoReport.objects.filter(pk=report.pk).exists())

    def test_bulk_delete_is_capped(self):
        with mock.patch.object(photo_reports, "BULK_DELETE_MAX", 2):
            reports = [self._report() for _ in range(3)]
            outcome = photo_reports.delete_many([r.pk for r in reports], actor=self.curator)
        self.assertEqual(outcome.deleted, 2)
        self.assertEqual(PhotoReport.objects.count(), 1)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT.path)
class LegacyReportFromBeforeReviewTests(TransactionTestCase):
    """A report uploaded before migration 0028 (and under a personal file name,
    before milestone 6): after the migration it is approved and listed in the
    CRM with a Delete button; deleting it removes the renamed file + variants
    (no orphan), revokes the points / badge paid at completion, puts the task
    back to waiting for a report and notifies the volunteer."""

    # No serialized_rollback: restoring the snapshot after another
    # TransactionTestCase re-inserted content types twice on PostgreSQL. The
    # badge this test needs is seeded here (0021's own function) instead.
    OLD = [("myapp", "0027_donation_goods_page")]

    def _migrate(self, targets):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_old_report_is_listed_approved_and_deletes_cleanly(self):
        from django.core.cache import cache
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        from django.core.management import call_command
        from django.db.migrations.loader import MigrationLoader
        from django.db import connection

        from myapp.models import UserAchievement
        from myapp.services import achievements, images

        cache.clear()
        from django.apps import apps as global_apps
        from importlib import import_module

        import_module("myapp.migrations.0021_achievements").seed_achievements(global_apps, None)
        latest = MigrationLoader(connection).graph.leaf_nodes()
        client_user = _make("lg_client", is_client=True)
        volunteer = _make("lg_vol", is_volunteer=True)
        admin = Users.objects.create_superuser(username="lg_admin", email="lg_admin@example.com", password=PASSWORD)
        try:
            old_apps = self._migrate(self.OLD)
            OldTask = old_apps.get_model("myapp", "HelpRequest")
            OldReport = old_apps.get_model("myapp", "PhotoReport")
            # Completed the old way: points paid at once (3), no review.
            task = OldTask.objects.create(
                client_id=client_user.pk, volunteer_id=volunteer.pk, status="completed", help_type="grocery",
                description="d", address="a", phone="+992901234567", region="dushanbe",
            )
            personal = default_storage.save("reports/Ivanov_Ivan.jpg", ContentFile(_jpeg().read()))
            report = OldReport.objects.create(
                author_id=volunteer.pk, title="Старый отчёт", image=personal, help_request_id=task.pk, region="dushanbe",
            )
        finally:
            self._migrate(latest)

        volunteer.profile.rating = 3
        volunteer.profile.save(update_fields=["rating"])
        achievements.evaluate(volunteer, send_notifications=False)  # first_help, as it was then
        self.assertTrue(UserAchievement.objects.filter(user=volunteer, achievement__code="first_help").exists())
        legacy = PhotoReport.objects.get(pk=report.pk)
        self.assertEqual(legacy.status, PhotoReport.APPROVED)
        self.assertFalse(HelpRequest.objects.get(pk=task.pk).reward_pending)

        # The milestone-6 rename + the variants backfill, as on the server.
        call_command("rename_legacy_uploads", stdout=io.StringIO())
        legacy.refresh_from_db()
        self.assertNotEqual(legacy.image.name, personal)
        self.assertFalse(default_storage.exists(personal))
        images.apply_variants("myapp.PhotoReport", legacy.pk)
        legacy.refresh_from_db()
        files = [legacy.image.name, *legacy.image_variants.values()]
        self.assertEqual(len(files), 3)
        self.assertTrue(all(default_storage.exists(f) for f in files))

        self.client.force_login(admin)
        html = self.client.get(reverse("crm_photo_reports") + "?status=approved").content.decode()
        self.assertIn(f'action="{reverse("crm_photo_report_delete", args=[legacy.pk])}"', html)
        self.assertIn(f'name="ids" value="{legacy.pk}"', html)

        self.client.post(reverse("crm_photo_report_delete", args=[legacy.pk]))
        self.assertFalse(PhotoReport.objects.filter(pk=legacy.pk).exists())
        self.assertFalse(any(default_storage.exists(f) for f in [personal, *files]))
        # Nothing of this photo is left under reports/ (no orphan of either name).
        stem = files[0].rsplit("/", 1)[-1].split(".")[0]
        _dirs, left = default_storage.listdir("reports")
        self.assertFalse([name for name in left if name.startswith(stem) or name.startswith("Ivanov_Ivan")])
        self.assertTrue(HelpRequest.objects.get(pk=task.pk).reward_pending)
        volunteer.profile.refresh_from_db()
        self.assertEqual(volunteer.profile.rating, 0)
        self.assertFalse(UserAchievement.objects.filter(user=volunteer, achievement__code="first_help").exists())
        self.assertTrue(Notification.objects.filter(recipient=volunteer, type="photo_report_deleted").exists())
