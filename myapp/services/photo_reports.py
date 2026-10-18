"""Photo-report review — the one place a report is approved or rejected and a
task's reward is released.

    pending ──approve──▶ approved   (author notified; the task's reward, if it
       │                             waited for this report, is credited)
       └───reject────▶ rejected     (author notified with the reason; they
                                     upload a new report)

**The reward rule.** Completing a task (``task_actions.complete``) no longer
pays out at once: unless an approved report by the assigned volunteer already
exists, the task is completed with ``reward_pending=True`` — no rating points,
no achievement. ``credit_task`` pays exactly once: a conditional
``UPDATE … WHERE reward_pending`` claims the task, then the points
(5 urgent / 3 normal, as before) and the ``task_completed`` badge check run.
The public rating and the badges count a completed task only once it is
credited (``reward_pending=False``).

**Deletion** (``delete``) removes any report — the row, the original and its
WebP variants. If the deleted report was the approved one that paid its task,
the reward is taken back (``revoke_task_reward``: ``reward_pending`` set again,
the points subtracted, the badges that task earned revoked) and the task waits
for a new report — the volunteer is told (site + Telegram).

Review / delete: curator + admin — the views gate on ``role_required("admin", "curator")``.
Creating (``create``): the author's own form, or the admin-only CRM form.
Logs carry IDs only.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import F, Q, Value
from django.db.models.functions import Greatest
from django.urls import reverse
from django.utils import timezone

from accounts.models import Profile, clear_rating_cache

from ..models import HelpRequest, PhotoReport
from .notify import notify

logger = logging.getLogger(__name__)

OK = "ok"
ALREADY_REVIEWED = "already_reviewed"
REASON_REQUIRED = "reason_required"
NOT_FOUND = "not_found"
BULK_DELETE_MAX = 100  # ids per "Delete selected" (the CRM list shows 20 a page)

DEFAULT_STATUS_FILTER = "pending"


@dataclass
class Outcome:
    ok: bool
    code: str
    credited: bool = False
    reward_revoked: bool = False  # delete(): the task's reward was taken back


def create(report, image, *, author, uploaded_by):
    """The one way a report is created from the site — the volunteer's form
    (``photo_reports_view``) and the admin's CRM form alike. ``report`` is an
    unsaved ``PhotoReport`` (title / description / task / …), ``image`` the
    uploaded file. The photo loses its metadata and gets a ``<uuid>`` name
    (``images.clean_original``); the report starts ``pending`` and goes through
    the same review — ``approve`` releases the task's reward only when
    ``author`` is the task's volunteer, so a report an admin adds as themselves
    never pays the admin. Returns ``(report, None)`` or ``(None, error)`` when
    the file is not a usable image. The admin form's "approve right away"
    then calls ``approve`` — the same approval as the review button."""
    from . import images

    clean, err = images.clean_original(image)
    if err:
        return None, err
    report.author = author
    report.uploaded_by = uploaded_by
    if not report.region:
        task = report.help_request
        report.region = (task.region if task else "") or author.region
    report.image = clean
    report.status = PhotoReport.PENDING
    report.save()
    images.queue_variants(report)
    logger.info(
        "photo report #%s (task #%s) uploaded by user #%s for user #%s",
        report.pk, report.help_request_id, uploaded_by.pk, author.pk,
    )
    return report, None


def task_reward_points(task) -> int:
    return 5 if task.is_urgent else 3


def has_approved_report(task, volunteer) -> bool:
    return PhotoReport.objects.filter(
        help_request=task, author=volunteer, status=PhotoReport.APPROVED
    ).exists()


def credit_task(task) -> bool:
    """Release a completed task's reward once. True when this call paid it."""
    from . import achievements

    claimed = HelpRequest.objects.filter(
        pk=task.pk, status="completed", reward_pending=True, volunteer__isnull=False
    ).update(reward_pending=False)
    if not claimed:
        return False
    task.refresh_from_db()
    profile, _ = Profile.objects.get_or_create(user=task.volunteer)
    profile.add_points(task_reward_points(task))  # also clears the rating cache
    achievements.award_safely(task.volunteer, "task_completed")  # never raises
    logger.info("task #%s reward credited to volunteer #%s", task.pk, task.volunteer_id)
    return True


def _rewarded_task(report):
    """The task this report can release: the author must be its volunteer."""
    task = report.help_request
    if task is None or report.author_id is None or task.volunteer_id != report.author_id:
        return None
    return task


def approve(report, *, actor) -> Outcome:
    try:
        report.approve(actor)
    except ValueError:
        return Outcome(False, ALREADY_REVIEWED)
    logger.info("photo report #%s approved by user #%s", report.pk, actor.pk)
    task = _rewarded_task(report)
    credited = credit_task(task) if task else False
    if report.author_id and report.author_id != actor.pk:  # nobody is told about their own decision
        notify(
            [report.author], "photo_report_approved",
            params={"title": report.title, "task_id": report.help_request_id},
            link=reverse("dashboard"), dedupe_key=f"photo_report_reviewed:{report.pk}",
        )
    return Outcome(True, OK, credited=credited)


def reject(report, *, actor, reason) -> Outcome:
    reason = (reason or "").strip()
    if not reason:
        return Outcome(False, REASON_REQUIRED)
    try:
        report.reject(actor, reason)
    except ValueError:
        return Outcome(False, ALREADY_REVIEWED)
    logger.info("photo report #%s rejected by user #%s", report.pk, actor.pk)
    if report.author_id and report.author_id != actor.pk:
        upload = reverse("photo_reports")
        if report.help_request_id:
            upload += f"?task={report.help_request_id}"
        notify(
            [report.author], "photo_report_rejected",
            params={"title": report.title, "message": report.rejection_reason, "task_id": report.help_request_id},
            link=upload + "#report-form", dedupe_key=f"photo_report_reviewed:{report.pk}",
        )
    return Outcome(True, OK)


def revoke_task_reward(task) -> bool:
    """The reverse of ``credit_task``: a credited task goes back to waiting for
    a photo report. One conditional ``UPDATE … WHERE reward_pending=False``
    claims it (a second call, or a task never credited, does nothing); then the
    points are subtracted (never below 0) and the badges this task earned are
    revoked (``achievements.revoke_lost``). True when this call took it back."""
    from . import achievements

    if task.volunteer_id is None:
        return False
    before = achievements.counters(task.volunteer)
    claimed = HelpRequest.objects.filter(
        pk=task.pk, status="completed", reward_pending=False, volunteer_id=task.volunteer_id
    ).update(reward_pending=True)
    if not claimed:
        return False
    task.reward_pending = True
    Profile.objects.filter(user_id=task.volunteer_id).update(
        rating=Greatest(F("rating") - task_reward_points(task), Value(0)), updated_at=timezone.now()
    )
    clear_rating_cache()
    achievements.revoke_lost(task.volunteer, before)
    logger.info("task #%s reward revoked from volunteer #%s", task.pk, task.volunteer_id)
    return True


@dataclass
class _Deleted:
    report_id: int
    task_id: int | None
    author: object
    title: str
    revoked: bool


def _delete_one(report_id, *, actor):
    """Delete one report — row, then files — and take back the reward it paid.
    None when it is already gone. No notification (the callers group them)."""
    from .images import DEFAULT_SIZES, delete_variants, variant_name

    with transaction.atomic():
        # of=("self",): lock the report row only. help_request is a nullable FK
        # (LEFT OUTER JOIN), and PostgreSQL refuses FOR UPDATE on the nullable
        # side of an outer join (the 2026-10-17 prod 500; SQLite ignores FOR UPDATE).
        row = (
            PhotoReport.objects.select_for_update(of=("self",))
            .select_related("help_request").filter(pk=report_id).first()
        )
        if row is None:
            return None
        done = _Deleted(row.pk, row.help_request_id, row.author, row.title, False)
        task = _rewarded_task(row) if row.status == PhotoReport.APPROVED else None
        storage, image_name = row.image.storage, row.image.name
        # The recorded variants + the conventional names (a build that finished
        # after this row was read is still caught). ``image`` is the current
        # name — after rename_legacy_uploads that is the <uuid> file, and the
        # command already removed the old-named file and its old variants.
        variants = dict(row.image_variants or {})
        if image_name:
            variants.update({f"_{size}": variant_name(image_name, size) for size in DEFAULT_SIZES})
        row.delete()
        done.revoked = bool(task) and not has_approved_report(task, done.author) and revoke_task_reward(task)
    # Files only once the row is gone for good (a rollback keeps both).
    delete_variants(storage, variants)
    if image_name:
        try:
            storage.delete(image_name)
        except OSError:
            logger.warning("photo report #%s: original file could not be removed", done.report_id)
    logger.info(
        "photo report #%s (task #%s) deleted by user #%s, reward revoked: %s",
        done.report_id, done.task_id, actor.pk, done.revoked,
    )
    return done


def _notify_deleted(author, items, actor):
    """One notice per author: the plain one for a single report, the
    ``many`` variant (with the count) for several — a bulk delete never sends
    a volunteer a Telegram message per photo."""
    if author is None or author.pk == actor.pk or not items:
        return
    if len(items) == 1:
        item = items[0]
        upload = reverse("photo_reports") + (f"?task={item.task_id}" if item.task_id else "") + "#report-form"
        notify(
            [author], "photo_report_deleted",
            params={"title": item.title, "task_id": item.task_id},
            link=upload, dedupe_key=f"photo_report_deleted:{item.report_id}",
        )
        return
    ids = sorted(item.report_id for item in items)
    digest = hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()[:16]
    notify(
        [author], "photo_report_deleted", variant="many",
        params={"count": len(items)},
        link=reverse("photo_reports") + "#report-form", dedupe_key=f"photo_reports_deleted:{digest}",
    )


def delete(report, *, actor) -> Outcome:
    """Delete a report of any status — including the ones uploaded before the
    review existed (migration 0028 made them approved): the row, then the
    original + its WebP variants on disk. The approved report that paid its
    task takes the reward with it — unless another approved report by the same
    volunteer still covers the task. The author is notified; ID-only log."""
    done = _delete_one(report.pk, actor=actor)
    if done is None:
        return Outcome(False, NOT_FOUND)
    _notify_deleted(done.author, [done], actor)
    return Outcome(True, OK, reward_revoked=done.revoked)


@dataclass
class BulkOutcome:
    deleted: int = 0
    revoked: int = 0
    missing: int = 0  # already gone (a second tab, a double submit)


def delete_many(report_ids, *, actor) -> BulkOutcome:
    """``delete`` for several reports (the CRM "Delete selected"): each one
    exactly as a single delete, then one notice per author. At most
    ``BULK_DELETE_MAX`` ids per call."""
    ids = sorted({int(pk) for pk in report_ids})[:BULK_DELETE_MAX]
    outcome = BulkOutcome()
    by_author = {}
    for report_id in ids:
        done = _delete_one(report_id, actor=actor)
        if done is None:
            outcome.missing += 1
            continue
        outcome.deleted += 1
        outcome.revoked += done.revoked
        if done.author is not None:
            by_author.setdefault(done.author.pk, (done.author, []))[1].append(done)
    for author, items in by_author.values():
        _notify_deleted(author, items, actor)
    logger.info(
        "photo reports bulk delete by user #%s: %s deleted, %s rewards revoked, %s already gone",
        actor.pk, outcome.deleted, outcome.revoked, outcome.missing,
    )
    return outcome


# --- read helpers -------------------------------------------------------------

def pending_count() -> int:
    return PhotoReport.objects.filter(status=PhotoReport.PENDING).count()


def public_reports():
    """What the public pages may show: approved reports only."""
    return PhotoReport.objects.filter(status=PhotoReport.APPROVED)


def visible_to(user):
    """The reports list: approved ones + the viewer's own (any status)."""
    return PhotoReport.objects.filter(Q(status=PhotoReport.APPROVED) | Q(author=user)).select_related("uploaded_by")


def recent_for_author(user, limit):
    # The author's latest uploads (with their review status) — upload order;
    # the dashboard shows each one's event_date.
    return list(
        PhotoReport.objects.filter(author=user).select_related("help_request").order_by("-created_at")[:limit]
    )


def staff_queryset(filters):
    """The CRM list. ``filters``: status ("all" or a status), date_from,
    date_to (inclusive, compared with ``event_date`` — the day the help took
    place), volunteer (a user), task (an id). Ordered by upload time, newest
    first: the list is the review queue."""
    qs = PhotoReport.objects.select_related(
        "author", "author__profile", "uploaded_by", "help_request", "help_request__client"
    )
    status = filters.get("status") or DEFAULT_STATUS_FILTER
    if status != "all":
        qs = qs.filter(status=status)
    if filters.get("date_from"):
        qs = qs.filter(event_date__gte=filters["date_from"])
    if filters.get("date_to"):
        qs = qs.filter(event_date__lte=filters["date_to"])
    if filters.get("volunteer"):
        qs = qs.filter(author=filters["volunteer"])
    if filters.get("task"):
        qs = qs.filter(help_request_id=filters["task"])
    return qs.order_by("-created_at", "-id")


def report_authors():
    """Who can be picked in the volunteer filter: everyone with a report."""
    from accounts.models import Users

    return Users.objects.filter(photo_reports__isnull=False).distinct().order_by("username")
