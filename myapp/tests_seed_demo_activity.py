"""seed_demo_activity: history for the imported @khayrkhoh.invalid accounts only.

Locks: no account is created or changed; protected accounts (staff, everyone
without the demo e-mail, parties of existing requests) get no row and no
change; nothing leaves the process (mail / Telegram / Celery / HTTP); dates in
[1 Aug 2026, now] and growing towards now; idempotent; ``--dry-run`` writes
nothing; ``--purge`` returns the database to its state before the run.
"""

import datetime
import json
import re
import tempfile
from pathlib import Path
from unittest import mock

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.models import Profile, Users
from myapp.management.commands import seed_demo_activity as seed
from myapp.models import (
    ChatMessage, HelpRequest, Notification, TaskReview, UserAchievement, UserDailyActivity,
)
from myapp.services import chat_flags

AS_OF = "2026-10-19T12:00:00+05:00"
AS_OF_DT = datetime.datetime.fromisoformat(AS_OF)
N = 40


def make_user(username, *, email=None, role="volunteer", **extra):
    flags = {"is_volunteer": role == "volunteer", "is_client": role == "client", "is_curator": role == "curator"}
    if role == "admin":
        flags = {"is_superuser": True, "is_staff": True}
    user = Users.objects.create_user(username=username, email=email or f"{username}@example.org",
                                     password="x-Test-12345", region="dushanbe", **flags, **extra)
    Profile.objects.get_or_create(user=user)
    return user


def comparable(snap):
    """A snapshot without the Profile fields a run / purge may legitimately
    touch on demo accounts (updated_at, recomputed availability)."""
    out = {}
    for label, data in snap.items():
        names = data["fields"]
        drop = {"updated_at", "availability_status"} if label == "accounts.Profile" else set()
        keep = [i for i, n in enumerate(names) if n not in drop]
        out[label] = {pk: tuple(row[i] for i in keep) for pk, row in data["rows"].items()}
    return out


@override_settings(DEMO_MODE=True)
class SeedDemoActivityTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manifest = Path(self.tmp.name) / "manifest.json"
        self.demo_volunteers = [make_user(f"dv{i}", email=f"volunteer-{i:02d}@khayrkhoh.invalid") for i in range(9)]
        self.demo_clients = [make_user(f"dc{i}", email=f"client-{i:02d}@khayrkhoh.invalid", role="client")
                             for i in range(7)]
        # Protected: staff, ordinary accounts, and a demo account that is a
        # party of an existing request.
        self.admin = make_user("boss", role="admin")
        self.curator = make_user("cur", role="curator")
        self.real_volunteer = make_user("realvol")
        self.real_client = make_user("realcl", role="client")
        self.staff_like = make_user("odd", email="odd@khayrkhoh.invalid", role="curator")
        self.busy_demo = make_user("dvbusy", email="volunteer-99@khayrkhoh.invalid")
        self.existing = HelpRequest.objects.create(
            client=self.real_client, volunteer=self.busy_demo, status="active", help_type="grocery",
            description="existing", address="x", phone="+992900000000", region="dushanbe",
        )
        Notification.objects.create(recipient=self.real_client, type="task_accepted", title_key="a", body_key="b")
        self.protected_ids = {u.pk for u in (self.admin, self.curator, self.real_volunteer, self.real_client,
                                             self.staff_like, self.busy_demo)}
        self.demo_ids = {u.pk for u in (*self.demo_volunteers, *self.demo_clients)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, *args, **opts):
        mail.outbox = []  # the welcome mails of setUp's accounts
        opts.setdefault("manifest", str(self.manifest))
        opts.setdefault("as_of", AS_OF)
        opts.setdefault("requests", N)
        return call_command("seed_demo_activity", *args, **opts)

    def seed_rows(self, model):
        return model.objects.filter(pk__in=json.loads(self.manifest.read_text())["rows"][model._meta.label])

    # ------------------------------------------------------------- guards

    def test_write_needs_the_confirm_flag(self):
        with self.assertRaises(CommandError):
            self.run_cmd()
        self.assertFalse(HelpRequest.objects.filter(is_demo=True).exists())

    @override_settings(DEMO_MODE=False)
    def test_write_needs_demo_mode(self):
        with self.assertRaises(CommandError):
            self.run_cmd(confirm=True)
        self.assertFalse(HelpRequest.objects.filter(is_demo=True).exists())

    def test_dry_run_writes_nothing(self):
        before = comparable(seed.snapshot())
        self.run_cmd(dry_run=True)
        self.assertEqual(comparable(seed.snapshot()), before)
        self.assertFalse(self.manifest.exists())

    def test_participants_are_demo_e_mail_non_staff_only(self):
        ids = set(seed.demo_accounts().values_list("pk", flat=True))
        self.assertEqual(ids, self.demo_ids | {self.busy_demo.pk})  # busy_demo is left out later, as a party
        self.assertNotIn(self.staff_like.pk, ids)

    # ---------------------------------------------------------------- run

    def test_run_writes_only_for_demo_accounts(self):
        users_before = {u["id"]: u for u in Users.objects.values()}
        self.run_cmd(confirm=True)
        users_after = {u["id"]: u for u in Users.objects.values()}
        self.assertEqual(set(users_after), set(users_before))  # no account created / deleted
        for pk, row in users_after.items():
            changed = {k for k, v in row.items() if users_before[pk][k] != v}
            if pk in self.protected_ids:
                self.assertEqual(changed, set(), pk)
            else:
                self.assertLessEqual(changed, {"date_joined"}, pk)  # logins, e-mails, roles … untouched
        self.assertTrue(self.manifest.exists())

        tasks = self.seed_rows(HelpRequest)
        self.assertEqual(tasks.count(), N)
        self.assertFalse(tasks.filter(is_demo=False).exists())
        self.assertFalse(tasks.exclude(client_id__in=self.demo_ids).exists())
        self.assertFalse(tasks.filter(volunteer__isnull=False).exclude(volunteer_id__in=self.demo_ids).exists())
        self.assertEqual(Notification.objects.exclude(recipient_id__in=self.demo_ids).count(), 1)  # the old one
        self.assertFalse(self.seed_rows(Notification).exclude(recipient_id__in=self.demo_ids).exists())
        self.assertFalse(ChatMessage.objects.filter(sender_id__in=self.protected_ids).exists())
        self.assertFalse(UserAchievement.objects.filter(user_id__in=self.protected_ids).exists())
        self.assertFalse(UserDailyActivity.objects.filter(user_id__in=self.protected_ids).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.busy_demo).exists())  # left out = nothing
        self.assertFalse(TaskReview.objects.exclude(client_id__in=self.demo_ids).exists())
        self.existing.refresh_from_db()
        self.assertEqual((self.existing.status, self.existing.volunteer_id), ("active", self.busy_demo.pk))
        self.assertEqual(mail.outbox, [])

        manifest = json.loads(self.manifest.read_text())
        self.assertIn(self.busy_demo.pk, manifest["excluded_user_ids"])
        self.assertFalse(HelpRequest.objects.filter(pk__in=manifest["rows"]["myapp.HelpRequest"],
                                                    client=self.busy_demo).exists())

    def test_statuses_dates_growth_places(self):
        self.run_cmd(confirm=True)
        tasks = list(self.seed_rows(HelpRequest))
        status = {s: sum(t.status == s for t in tasks) for s in ("completed", "active", "pending", "cancelled")}
        self.assertGreater(status["completed"], len(tasks) / 2)  # most are done
        self.assertTrue(all(status.values()), status)  # every state exists
        start = seed.WINDOW_START
        for t in tasks:
            for value in (t.created_at, t.accepted_at, t.completed_at, t.cancelled_at, t.updated_at):
                if value is not None:
                    self.assertTrue(start <= value <= AS_OF_DT, (t.pk, value))
            self.assertTrue(38.50 < float(t.latitude) < 38.62 and 68.70 < float(t.longitude) < 68.85)
            self.assertTrue(t.phone.startswith("+99200"))
        months = {m: sum(t.created_at.astimezone(seed.TZ).month == m for t in tasks) for m in (8, 9, 10)}
        self.assertLess(months[8], months[9])
        self.assertLess(months[9], months[10])
        for model, fieldname in ((TaskReview, "created_at"), (ChatMessage, "created_at"),
                                 (Notification, "created_at"), (UserAchievement, "awarded_at")):
            for value in self.seed_rows(model).values_list(fieldname, flat=True):
                self.assertTrue(start <= value <= AS_OF_DT, (model.__name__, value))
        for day in self.seed_rows(UserDailyActivity).values_list("date", flat=True):
            self.assertTrue(start.date() <= day <= AS_OF_DT.date())

    def test_registration_moves_before_the_first_demo_activity(self):
        self.run_cmd(confirm=True)
        tasks = list(self.seed_rows(HelpRequest))
        first = {}
        for t in tasks:
            first[t.client_id] = min(first.get(t.client_id, t.created_at), t.created_at)
            if t.accepted_at:
                first[t.volunteer_id] = min(first.get(t.volunteer_id, t.accepted_at), t.accepted_at)
        manifest = json.loads(self.manifest.read_text())
        self.assertEqual({int(k) for k in manifest["date_joined"]}, self.demo_ids)
        for user in Users.objects.filter(pk__in=self.demo_ids):
            self.assertGreaterEqual(user.date_joined, seed.JOINED_FROM)
            if user.pk in first:
                self.assertLessEqual(user.date_joined, first[user.pk] - datetime.timedelta(days=1), user.pk)
            else:
                self.assertLessEqual(user.date_joined, seed.JOINED_IDLE_UNTIL)
        for n in self.seed_rows(Notification).select_related("recipient"):
            self.assertGreaterEqual(n.created_at, n.recipient.date_joined)  # nothing before registration
        self.busy_demo.refresh_from_db()
        self.assertEqual(self.busy_demo.date_joined, Users.objects.get(pk=self.busy_demo.pk).date_joined)
        self.assertNotIn(str(self.busy_demo.pk), manifest["date_joined"])

    def test_volunteers_with_a_photo_carry_the_activity(self):
        from myapp.services import rating

        photo = self.demo_volunteers[:3]
        for user in photo:
            Profile.objects.filter(user=user).update(image=f"avatars/{user.pk}.jpg")
        self.run_cmd(confirm=True)
        done = {u.pk: HelpRequest.objects.filter(status="completed", volunteer=u, is_demo=True).count()
                for u in self.demo_volunteers}
        reviews = {u.pk: TaskReview.objects.filter(volunteer=u, is_demo=True).count() for u in self.demo_volunteers}
        no_photo = [u for u in self.demo_volunteers if u not in photo]
        self.assertGreater(min(done[u.pk] for u in photo), max(done[u.pk] for u in no_photo))
        self.assertTrue(all(done[u.pk] <= seed.NO_PHOTO_MAX_TASKS for u in no_photo))
        self.assertGreater(sum(reviews[u.pk] for u in photo), sum(reviews[u.pk] for u in no_photo))
        top3 = [u.pk for u in rating.ranked_queryset()[:3]]
        self.assertEqual(set(top3), {u.pk for u in photo})

    def _shares(self):
        manifest = json.loads(self.manifest.read_text())
        tasks = HelpRequest.objects.filter(pk__in=manifest["rows"]["myapp.HelpRequest"])
        today = AS_OF_DT.astimezone(seed.TZ).date()
        out = {}
        for user in Users.objects.filter(pk__in=manifest["demo_user_ids"]):
            n = (tasks.filter(volunteer=user, status="completed").count() if user.is_volunteer
                 else tasks.filter(client=user).count())
            first = max(seed.WINDOW_START.date(), user.date_joined.astimezone(seed.TZ).date())
            days = set(UserDailyActivity.objects.filter(user=user, date__gte=first, date__lte=today)
                       .values_list("date", flat=True))
            out[user.pk] = (n, len(days) / ((today - first).days + 1), first)
        return out

    def test_active_day_shares_follow_the_tiers(self):
        self.run_cmd(confirm=True)
        for uid, (n, share, first) in self._shares().items():
            low, high = next(band for threshold, band in seed.ACTIVITY_TIERS if n >= threshold)
            self.assertGreaterEqual(share, low - 0.02, (uid, n, share))
            # Real event days are never removed, so a busy account may sit a little above its band.
            self.assertLessEqual(share, high + 0.08, (uid, n, share))
        for row in self.seed_rows(UserDailyActivity).select_related("user"):
            self.assertGreaterEqual(row.date, row.user.date_joined.astimezone(seed.TZ).date())
            self.assertTrue(row.is_demo)

    def test_fill_activity_tops_up_records_ids_and_purge_removes_them(self):
        before = comparable(seed.snapshot())
        self.run_cmd(confirm=True)
        # An older run without the visit days: drop them, then top up.
        manifest = json.loads(self.manifest.read_text())
        visits = UserDailyActivity.objects.filter(pk__in=manifest["rows"]["myapp.UserDailyActivity"], actions_count__lte=3)
        removed = set(visits.values_list("pk", flat=True))
        visits.delete()
        manifest["rows"]["myapp.UserDailyActivity"] = sorted(set(manifest["rows"]["myapp.UserDailyActivity"]) - removed)
        self.manifest.write_text(json.dumps(manifest))
        protected_before = {m: m.objects.filter(**{f: self.protected_ids}).count() for m, f in
                            ((UserDailyActivity, "user_id__in"), (Notification, "recipient_id__in"))}
        self.run_cmd(confirm=True, fill_activity=True, dry_run=True)
        self.assertEqual(json.loads(self.manifest.read_text())["rows"], manifest["rows"])   # dry run: nothing
        self.run_cmd(confirm=True, fill_activity=True)
        after = json.loads(self.manifest.read_text())
        added = set(after["rows"]["myapp.UserDailyActivity"]) - set(manifest["rows"]["myapp.UserDailyActivity"])
        self.assertTrue(added)
        self.assertEqual(len(after["activity_fill"]), 1)
        self.assertFalse(UserDailyActivity.objects.filter(pk__in=added).exclude(user_id__in=self.demo_ids).exists())
        self.assertEqual({m: m.objects.filter(**{f: self.protected_ids}).count() for m, f in
                          ((UserDailyActivity, "user_id__in"), (Notification, "recipient_id__in"))}, protected_before)
        self.run_cmd(confirm=True, fill_activity=True)        # a second top-up adds nothing
        self.assertEqual(json.loads(self.manifest.read_text())["rows"]["myapp.UserDailyActivity"],
                         after["rows"]["myapp.UserDailyActivity"])
        self.run_cmd(confirm=True, purge=True)
        self.assertEqual(comparable(seed.snapshot()), before)

    def test_cron_sweeps_will_not_alert_staff_about_demo_requests(self):
        self.run_cmd(confirm=True)
        tasks = self.seed_rows(HelpRequest)
        self.assertFalse(tasks.filter(status="active", alarm_sent=False).exists())
        self.assertFalse(tasks.filter(status="pending", stale_alert_sent=False).exists())

    def test_reviews_stay_off_the_landing_page_and_badges_follow_the_data(self):
        from myapp.services import achievements

        self.run_cmd(confirm=True)
        reviews = self.seed_rows(TaskReview)
        self.assertTrue(reviews.exists())
        self.assertFalse(reviews.filter(show_on_landing=True).exists())
        self.assertFalse(reviews.filter(is_demo=False).exists())
        for volunteer in self.demo_volunteers:
            self.assertEqual(achievements.evaluate(volunteer, send_notifications=False), [])  # nothing left to award
        completed = HelpRequest.objects.filter(status="completed", volunteer__in=self.demo_volunteers)
        self.assertFalse(completed.filter(reward_pending=True).exists())

    def test_three_languages_and_no_unintended_chat_flags(self):
        self.run_cmd(confirm=True)
        texts = " ".join(self.seed_rows(HelpRequest).values_list("description", flat=True))
        self.assertRegex(texts, r"[ӣҳқғҷӯ]")      # Tajik
        self.assertRegex(texts, r"[A-Za-z]{4,}")   # English
        self.assertRegex(texts, r"[ыэё]")          # Russian
        for key, text in seed.all_texts()["chat"]:
            flags = chat_flags.detect(text)
            if key == "money":
                continue
            self.assertEqual(flags, [], (key, text))

    def test_second_run_is_a_no_op(self):
        self.run_cmd(confirm=True)
        counts = {m: m.objects.count() for m in (HelpRequest, ChatMessage, Notification, TaskReview)}
        self.run_cmd(confirm=True)
        self.assertEqual({m: m.objects.count() for m in counts}, counts)

    def test_purge_returns_the_database_to_its_state_before_the_run(self):
        before = comparable(seed.snapshot())
        self.run_cmd(confirm=True)
        self.assertNotEqual(comparable(seed.snapshot()), before)
        self.run_cmd(confirm=True, purge=True)
        self.assertEqual(comparable(seed.snapshot()), before)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(len(list(Path(self.tmp.name).glob("manifest.purged-*.json"))), 1)

    def test_reset_dry_run_previews_purge_and_run_and_writes_nothing(self):
        from io import StringIO

        self.run_cmd(confirm=True)
        before = comparable(seed.snapshot())
        manifest = self.manifest.read_text()
        out = StringIO()
        self.run_cmd(reset=True, dry_run=True, stdout=out)
        text = out.getvalue()
        self.assertIn("Purge:", text)
        self.assertIn("Help requests:", text)        # the fresh run's report
        self.assertIn("Rating top 10", text)
        self.assertEqual(comparable(seed.snapshot()), before)
        self.assertEqual(self.manifest.read_text(), manifest)

    def test_reset_recreates_the_same_amount(self):
        self.run_cmd(confirm=True)
        first = HelpRequest.objects.filter(is_demo=True).count()
        self.run_cmd(confirm=True, reset=True)
        self.assertEqual(HelpRequest.objects.filter(is_demo=True).count(), first)
        self.assertTrue(self.manifest.exists())

    def test_purge_refuses_when_it_would_cascade_into_unlisted_rows(self):
        self.run_cmd(confirm=True)
        task = self.seed_rows(HelpRequest).filter(status="completed").first()
        ChatMessage.objects.create(task=task, sender=None, kind="system", body="added later")
        with self.assertRaises(CommandError):
            self.run_cmd(confirm=True, purge=True)
        self.assertTrue(self.manifest.exists())
        self.assertTrue(HelpRequest.objects.filter(pk=task.pk).exists())

    def test_an_external_send_attempt_rolls_everything_back(self):
        from myapp import notifications

        def leaky_review_request(task):
            notifications.queue_email("x@example.org", "s", "b")

        before = comparable(seed.snapshot())
        with mock.patch("myapp.services.feedback.review_request", leaky_review_request):
            with self.assertRaises(Exception):
                self.run_cmd(confirm=True)
        self.assertEqual(comparable(seed.snapshot()), before)
        self.assertFalse(self.manifest.exists())
        self.assertEqual(mail.outbox, [])


class SeedDemoActivityPlanTests(TestCase):
    def test_plan_respects_one_active_request_per_volunteer(self):
        now = AS_OF_DT
        people = seed.build_people(list(range(1, 20)), list(range(100, 130)))
        plan = seed.build_plan(people, 66, now)
        by_volunteer = {}
        for r in plan:
            if r.accepted:
                by_volunteer.setdefault(r.volunteer, []).append((r.accepted, r.completed))
        for spans in by_volunteer.values():
            spans.sort()
            for (_a1, c1), (a2, _c2) in zip(spans, spans[1:]):
                self.assertLessEqual(c1, a2)

    def test_activity_is_uneven(self):
        people = seed.build_people(list(range(1, 47)), list(range(100, 143)))
        plan = seed.build_plan(people, 66, AS_OF_DT)
        per = {}
        for r in plan:
            if r.volunteer:
                per[r.volunteer] = per.get(r.volunteer, 0) + 1
        counts = sorted(per.values(), reverse=True)
        self.assertGreaterEqual(counts[0], 4)          # a few very active
        self.assertLess(len(per), 46)                   # some never took one
        self.assertTrue(re.match(r"^\+99200\d{7}$", people[100].phone))
