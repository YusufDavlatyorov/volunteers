"""Photo reports carry two dates: ``event_date`` (when the help took place —
chosen in the form, the only date on public pages, what period statistics
count by) and ``created_at`` (publication — CRM / Django admin show both)."""

import datetime
from unittest import mock

from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Users
from myapp.models import EVENT_DATE_MAX_AGE_DAYS, HelpRequest, PhotoReport
from myapp.models.photo_reports import LOCAL_TZ, local_today
from myapp.services import activity, photo_reports
from myapp.tests_photo_report_review import PASSWORD, _jpeg, _make
from myapp.testing import TempMediaRoot

_MEDIA_ROOT = TempMediaRoot("photo-event-date-tests-")


def _days_ago(n):
    return local_today() - datetime.timedelta(days=n)


@override_settings(MEDIA_ROOT=_MEDIA_ROOT.path)
class EventDateTestsBase(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        _MEDIA_ROOT.cleanup()

    def setUp(self):
        self.client_user = _make("ed_client", is_client=True)
        self.volunteer = _make("ed_vol", is_volunteer=True)
        self.curator = _make("ed_curator", is_curator=True)
        self.admin = Users.objects.create_superuser(username="ed_admin", email="ed_admin@example.com", password=PASSWORD)
        self.task = HelpRequest.objects.create(
            client=self.client_user, volunteer=self.volunteer, status="active", help_type="grocery",
            description="d", address="a", phone="+992901234567", region="dushanbe",
        )

    def _post_report(self, **extra):
        self.client.force_login(self.volunteer)
        data = {"title": "Субботник", "description": "d", "image": _jpeg(), "help_request": self.task.pk, **extra}
        return self.client.post(reverse("photo_reports"), data)

    def _report(self, event_date, status=PhotoReport.APPROVED, title="Убрали двор"):
        return PhotoReport.objects.create(
            author=self.volunteer, title=title, image=_jpeg(), help_request=self.task, region="dushanbe",
            event_date=event_date, status=status,
        )


class FormValidationTests(EventDateTestsBase):
    def test_a_past_date_is_saved(self):
        day = _days_ago(5)
        response = self._post_report(event_date=day.isoformat())
        self.assertRedirects(response, reverse("photo_reports"))
        report = PhotoReport.objects.get()
        self.assertEqual(report.event_date, day)
        self.assertEqual(timezone.localtime(report.created_at, LOCAL_TZ).date(), local_today())  # published today

    def test_the_oldest_allowed_day_is_accepted(self):
        self._post_report(event_date=_days_ago(EVENT_DATE_MAX_AGE_DAYS).isoformat())
        self.assertEqual(PhotoReport.objects.get().event_date, _days_ago(EVENT_DATE_MAX_AGE_DAYS))

    def test_a_future_date_is_rejected(self):
        response = self._post_report(event_date=_days_ago(-1).isoformat())
        self.assertEqual(response.status_code, 200)
        errors = response.context["form"].errors.as_data()["event_date"]
        self.assertEqual(errors[0].code, "event_date_future")
        self.assertContains(response, 'data-i18n="err.event_date_future"')
        self.assertFalse(PhotoReport.objects.exists())

    def test_a_date_older_than_30_days_is_rejected(self):
        response = self._post_report(event_date=_days_ago(EVENT_DATE_MAX_AGE_DAYS + 1).isoformat())
        self.assertEqual(response.status_code, 200)
        errors = response.context["form"].errors.as_data()["event_date"]
        self.assertEqual(errors[0].code, "event_date_too_old")
        self.assertContains(response, 'data-i18n="err.event_date_too_old"')
        self.assertFalse(PhotoReport.objects.exists())

    def test_the_admin_form_validates_the_same_way(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("crm_photo_report_create"), {
            "help_request": self.task.pk, "volunteer": self.volunteer.pk, "title": "T", "image": _jpeg(),
            "event_date": _days_ago(-3).isoformat(),
        })
        self.assertEqual(response.context["form"].errors.as_data()["event_date"][0].code, "event_date_future")
        self.assertFalse(PhotoReport.objects.exists())

    def test_input_is_a_date_picker_limited_to_the_window(self):
        self.client.force_login(self.volunteer)
        html = self.client.get(reverse("photo_reports")).content.decode()
        self.assertRegex(html, r'<input type="date" name="event_date"[^>]*>')
        tag = next(t for t in html.split("<input") if 'name="event_date"' in t)
        self.assertIn(f'min="{_days_ago(EVENT_DATE_MAX_AGE_DAYS).isoformat()}"', tag)
        self.assertIn(f'max="{local_today().isoformat()}"', tag)
        self.assertIn(f'value="{local_today().isoformat()}"', tag)  # default: today


class DefaultFromTaskOrEventTests(EventDateTestsBase):
    def _complete(self, days_ago):
        HelpRequest.objects.filter(pk=self.task.pk).update(
            status="completed", completed_at=timezone.now() - datetime.timedelta(days=days_ago)
        )
        self.task.refresh_from_db()

    def test_task_completion_day_is_the_default(self):
        self._complete(3)
        self.client.force_login(self.volunteer)
        form = self.client.get(reverse("photo_reports") + f"?task={self.task.pk}").context["form"]
        self.assertEqual(form.initial["event_date"], timezone.localtime(self.task.completed_at, LOCAL_TZ).date())

    def test_an_empty_date_falls_back_to_the_task_day(self):
        self._complete(2)
        self._post_report(event_date="")
        self.assertEqual(PhotoReport.objects.get().event_date, timezone.localtime(self.task.completed_at, LOCAL_TZ).date())

    def test_a_task_day_outside_the_window_falls_back_to_today(self):
        self._complete(EVENT_DATE_MAX_AGE_DAYS + 5)
        self._post_report(event_date="")
        self.assertEqual(PhotoReport.objects.get().event_date, local_today())

    def test_options_carry_their_date_for_the_script(self):
        self._complete(4)
        self.client.force_login(self.volunteer)
        html = self.client.get(reverse("photo_reports")).content.decode()
        day = timezone.localtime(self.task.completed_at, LOCAL_TZ).date().isoformat()
        self.assertIn(f'data-date="{day}"', html)
        self.assertIn("js/photo-report-date", html)


class PublicPagesShowOnlyTheEventDateTests(EventDateTestsBase):
    def setUp(self):
        super().setUp()
        self.day = datetime.date(2026, 9, 27)
        self.report = self._report(self.day)
        # Published on another day, so the two dates can't be confused.
        self.published = datetime.datetime(2026, 10, 3, 12, 0, tzinfo=LOCAL_TZ)
        PhotoReport.objects.filter(pk=self.report.pk).update(created_at=self.published)

    def _assert_only_event_date(self, html):
        self.assertIn('<time datetime="2026-09-27" data-i18n-date="2026-09-27">27 сентября 2026</time>', html)
        for published in ("03.10.2026", "3 октября 2026", "2026-10-03", "Oct. 3, 2026"):
            self.assertNotIn(published, html)

    def test_about_page(self):
        for user in (None, self.client_user):
            if user:
                self.client.force_login(user)
            self._assert_only_event_date(self.client.get(reverse("about")).content.decode())

    def test_landing_page(self):
        self._assert_only_event_date(self.client.get(reverse("home")).content.decode())

    def test_public_reports_list(self):
        self.client.force_login(self.client_user)
        self._assert_only_event_date(self.client.get(reverse("photo_reports")).content.decode())

    def test_newest_event_first(self):
        older_event = self._report(datetime.date(2026, 9, 1), title="Раньше")  # uploaded later
        self.assertEqual(list(PhotoReport.objects.all()), [self.report, older_event])


class StaffPagesShowBothDatesTests(EventDateTestsBase):
    def setUp(self):
        super().setUp()
        self.report = self._report(datetime.date(2026, 9, 27), status=PhotoReport.PENDING)
        PhotoReport.objects.filter(pk=self.report.pk).update(
            created_at=datetime.datetime(2026, 10, 3, 12, 30, tzinfo=datetime.timezone.utc)  # CRM shows TIME_ZONE (UTC)
        )

    def test_django_admin_list_and_change_page(self):
        self.client.force_login(self.admin)
        html = self.client.get(reverse("admin:myapp_photoreport_changelist")).content.decode()
        # Column headers are the verbose names; cells hold both values.
        self.assertIn("Дата проведения", html)
        self.assertIn("Дата публикации", html)
        self.assertRegex(html, r'class="field-event_date[^"]*">[^<]*2026')
        self.assertRegex(html, r'class="field-created_at[^"]*">[^<]*2026')
        self.assertIn("event_date__gte", html)  # list_filter / date_hierarchy on event_date
        change = self.client.get(reverse("admin:myapp_photoreport_change", args=[self.report.pk])).content.decode()
        self.assertIn('name="event_date"', change)        # editable
        self.assertNotIn('name="created_at"', change)     # read-only
        self.assertIn("Дата публикации", change)

    def test_django_admin_can_edit_the_event_date(self):
        from myapp.admin import PhotoReportAdmin

        self.assertIn("created_at", PhotoReportAdmin.readonly_fields)
        self.assertNotIn("event_date", PhotoReportAdmin.readonly_fields)
        self.assertEqual(PhotoReportAdmin.date_hierarchy, "event_date")
        self.assertIn("event_date", PhotoReportAdmin.list_filter)
        self.assertEqual(PhotoReportAdmin.list_display[-2:], ("event_date", "created_at"))

    def test_crm_list_and_detail(self):
        self.client.force_login(self.curator)
        listing = self.client.get(reverse("crm_photo_reports")).content.decode()
        for text in ('data-i18n="pr.event_date"', 'data-i18n="pr.published_at"', "27.09.2026", "03.10.2026, 12:30"):
            self.assertIn(text, listing)
        detail = self.client.get(reverse("crm_photo_report_detail", args=[self.report.pk])).content.decode()
        for text in ("Дата проведения", "Дата публикации", "27 сентября 2026", "03.10.2026, 12:30"):
            self.assertIn(text, detail)


class StatisticsUseTheEventDateTests(EventDateTestsBase):
    def test_crm_period_filter_is_by_event_date(self):
        inside = self._report(_days_ago(10), title="В периоде")
        outside = self._report(_days_ago(20), title="Вне периода")  # both uploaded today
        qs = photo_reports.staff_queryset({"status": "all", "date_from": _days_ago(12), "date_to": _days_ago(8)})
        self.assertEqual(list(qs), [inside])
        self.assertNotIn(outside, qs)

    def test_activity_calendar_counts_the_event_day(self):
        self._report(_days_ago(6))
        counts = activity.daily_counts(self.volunteer)
        self.assertEqual(counts.get(_days_ago(6), {}).get("photo_report"), 1)
        self.assertNotIn("photo_report", counts.get(local_today(), {}))


class EventDateMigrationTests(TransactionTestCase):
    """0030 for real: existing reports get the Asia/Dushanbe day of created_at,
    and the migration runs backwards."""

    BEFORE = [("myapp", "0029_photo_report_uploaded_by")]
    AFTER = [("myapp", "0030_photo_report_event_date")]

    def _migrate(self, targets):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return executor.loader.project_state(targets).apps

    def test_backfill_uses_the_local_day_and_reverses(self):
        from django.db import connection
        from django.db.migrations.loader import MigrationLoader

        latest = MigrationLoader(connection).graph.leaf_nodes()
        volunteer = _make("mg_vol", is_volunteer=True)
        try:
            old_apps = self._migrate(self.BEFORE)
            OldReport = old_apps.get_model("myapp", "PhotoReport")
            report = OldReport.objects.create(author_id=volunteer.pk, title="Старый", image="reports/x.jpg")
            # 21:30 UTC on Jan 31 = 02:30 on Feb 1 in Dushanbe (UTC+5).
            OldReport.objects.filter(pk=report.pk).update(
                created_at=datetime.datetime(2026, 1, 31, 21, 30, tzinfo=datetime.timezone.utc)
            )
            new_apps = self._migrate(self.AFTER)
            migrated = new_apps.get_model("myapp", "PhotoReport").objects.get(pk=report.pk)
            self.assertEqual(migrated.event_date, datetime.date(2026, 2, 1))
            # Backwards: back to 0029 without an error, the column is gone.
            self._migrate(self.BEFORE)
            with connection.cursor() as cursor:
                columns = [c.name for c in connection.introspection.get_table_description(cursor, "myapp_photoreport")]
            self.assertNotIn("event_date", columns)
        finally:
            self._migrate(latest)
