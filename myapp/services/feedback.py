"""Client reviews of a completed task (Feature Pack v2, feature 9).

The one path for leaving, editing, hiding and reading reviews
(``myapp/models/feedback.py::TaskReview``):

- ``can_review`` / ``can_edit`` — the permission rules (client of a
  ``completed`` task with a volunteer; one review per task; editable by its
  author for ``REVIEW_EDIT_HOURS`` after creation).
- ``submit_review`` — create or edit; validates stars / comment / tags, is
  race-safe on the task OneToOne (a lost race is applied as an edit of the
  winner), keeps ``Profile.review_avg`` / ``review_count`` in sync and fans out
  ``review_received`` (volunteer, on create) and ``review_low`` (staff, once per
  review via a dedupe key, when stars <= ``LOW_STARS``).
- ``hide`` / ``unhide`` — staff moderation; recompute the stats.
- ``public_summary`` — ``{"avg", "count"}`` only: the single shape any public
  surface (rating page, client dashboard, cards) may show. Never comments.
- ``reviews_for`` — comments, only to the reviewed volunteer and staff (hidden
  ones to staff only); per review, ``can_see_comment`` also keeps the comment
  of a 1–2 star review from the volunteer (coordinators only).
- ``review_request`` — the "rate the help" prompt, called when a task completes.
- ``set_landing`` / ``landing_testimonials`` — the About page's featured
  reviews: staff opt a review in (``TaskReview.show_on_landing``, default off);
  only 5-star, non-empty, visible ones are shown, the client as "First L."
  (``short_public_name``) — never the username, the volunteer or any id.

Display-only: reviews are **not** a matching weight (owner decision, FEATURES_PLAN
§9.7) — nothing here is read by ``services/matching.py``.

Logs carry IDs (and the star count) only — never the comment text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count
from django.urls import reverse
from django.utils import timezone

from accounts.models import ABOUT_COMMUNITY_CACHE_KEY, REGION_CHOICES, Profile, Users, clear_rating_cache
from myapp.models import HELP_TYPE_CHOICES, REVIEW_COMMENT_MAX_LENGTH, REVIEW_TAGS, TaskReview

from .access import is_staff_user

logger = logging.getLogger(__name__)

LOW_STARS = 2
MIN_STARS = 1
MAX_STARS = 5
REVIEWS_PAGE_SIZE = 20

OK = "ok"
FORBIDDEN = "forbidden"
EDIT_CLOSED = "edit_closed"
INVALID_STARS = "invalid_stars"
COMMENT_TOO_LONG = "comment_too_long"
INVALID_TAGS = "invalid_tags"
NOT_CHANGED = "not_changed"


@dataclass(frozen=True)
class Outcome:
    ok: bool
    code: str
    review: TaskReview | None = None
    created: bool = False


# --------------------------------------------------------------------------- #
# permissions
# --------------------------------------------------------------------------- #

def _active_user(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.is_active)


def edit_hours() -> int:
    return int(getattr(settings, "REVIEW_EDIT_HOURS", 24))


def edit_deadline(review):
    """When the author's edit window closes."""
    return review.created_at + timedelta(hours=edit_hours())


def is_reviewable(user, task) -> bool:
    """``user`` is the client of ``task`` and the task can carry a review
    (completed, with a volunteer). Says nothing about an existing review."""
    return bool(
        _active_user(user)
        and user.is_client
        and task.client_id == user.pk
        and task.status == "completed"
        and task.volunteer_id
    )


def can_review(user, task) -> bool:
    """May ``user`` leave a *new* review on ``task``? Client of their own
    completed task (with a volunteer), and no review yet (one per task)."""
    if not is_reviewable(user, task):
        return False
    return not TaskReview.objects.filter(task_id=task.pk).exists()


def can_edit(user, review, now=None) -> bool:
    """The author, within ``REVIEW_EDIT_HOURS`` of creation."""
    if not _active_user(user) or review.client_id != user.pk:
        return False
    return (now or timezone.now()) <= edit_deadline(review)


def can_see_comments(viewer, volunteer) -> bool:
    """Review comments about ``volunteer``: that volunteer and staff only."""
    if not _active_user(viewer):
        return False
    vid = volunteer if isinstance(volunteer, int) else volunteer.pk
    return is_staff_user(viewer) or viewer.pk == vid


def can_see_comment(viewer, review) -> bool:
    """The comment of one review: its author and staff always; the reviewed
    volunteer only when the rating is above ``LOW_STARS`` — a 1–2 star
    comment is for the coordinators (owner decision, milestone 3 follow-up).
    The volunteer still sees the stars and tags of a low review."""
    if not _active_user(viewer):
        return False
    if is_staff_user(viewer) or viewer.pk == review.client_id:
        return True
    return viewer.pk == review.volunteer_id and review.stars > LOW_STARS


def can_view_review(viewer, review) -> bool:
    """One review (the per-task page): its author, staff, and the reviewed
    volunteer while it is not hidden."""
    if not _active_user(viewer):
        return False
    if is_staff_user(viewer) or viewer.pk == review.client_id:
        return True
    return viewer.pk == review.volunteer_id and review.hidden_at is None


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #

def clean_stars(stars):
    if isinstance(stars, bool):
        return None
    try:
        value = int(str(stars).strip())
    except (TypeError, ValueError):
        return None
    return value if MIN_STARS <= value <= MAX_STARS else None


def _clean_comment(comment):
    text = "" if comment is None else str(comment)
    # Browsers submit textarea newlines as CRLF; count a newline once, like the
    # textarea's own maxlength does.
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _clean_tags(tags):
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    cleaned = []
    for tag in tags:
        if not isinstance(tag, str) or tag not in REVIEW_TAGS:
            return None
        if tag not in cleaned:
            cleaned.append(tag)
    return cleaned


def validate(stars, comment, tags):
    """``(code, (stars, comment, tags))`` — code is OK or the first error."""
    stars_value = clean_stars(stars)
    if stars_value is None:
        return INVALID_STARS, None
    clean_comment = _clean_comment(comment)
    if len(clean_comment) > REVIEW_COMMENT_MAX_LENGTH:
        return COMMENT_TOO_LONG, None
    clean_tags = _clean_tags(tags)
    if clean_tags is None:
        return INVALID_TAGS, None
    return OK, (stars_value, clean_comment, clean_tags)


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #

def recompute_volunteer_stats(volunteer) -> dict:
    """Recompute ``Profile.review_avg`` / ``review_count`` from the volunteer's
    *visible* reviews: one aggregate query + one UPDATE. Returns the new
    ``{"avg": Decimal|None, "count": int}``."""
    vid = volunteer if isinstance(volunteer, int) else volunteer.pk
    agg = TaskReview.objects.filter(volunteer_id=vid, hidden_at__isnull=True).aggregate(
        avg=Avg("stars"), count=Count("id")
    )
    count = agg["count"] or 0
    avg = None
    if count and agg["avg"] is not None:
        avg = Decimal(str(agg["avg"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    Profile.objects.filter(user_id=vid).update(review_avg=avg, review_count=count)
    # Keep an already-loaded profile in step (the caller may render it next).
    if isinstance(volunteer, Users) and Users.profile.is_cached(volunteer):
        volunteer.profile.review_avg = avg
        volunteer.profile.review_count = count
    # The public rating caches the averages; show the new one at once. The
    # About page's leaders (and its testimonials, a hidden review) too.
    clear_rating_cache()
    return {"avg": avg, "count": count}


def public_summary(volunteer) -> dict:
    """``{"avg": float|None, "count": int}`` — the ONLY review data a public
    shape may carry (no comments, no tags, no reviewer). Accepts a ``Users`` or
    its ``Profile``; zero queries when the profile is already loaded."""
    if volunteer is None:
        return {"avg": None, "count": 0}
    if isinstance(volunteer, Profile):
        profile = volunteer
    else:
        try:
            profile = volunteer.profile
        except Profile.DoesNotExist:
            profile = None
    if profile is None or not profile.review_count:
        return {"avg": None, "count": 0}
    avg = profile.review_avg
    return {"avg": float(avg) if avg is not None else None, "count": int(profile.review_count)}


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #

def _notify(recipients, type_, **kwargs) -> int:
    """A review is already saved when we notify; a fan-out failure must not
    turn the client's submit into a 500. Logged at ERROR (IDs only)."""
    from .notify import notify

    try:
        return notify(recipients, type_, **kwargs)
    except Exception:  # noqa: BLE001 — side effect after the commit
        logger.exception("review notify failed type=%s", type_)
        return 0


def _display_name(user) -> str:
    try:
        full_name = user.profile.full_name
    except Profile.DoesNotExist:
        full_name = ""
    return full_name or user.username


def _alert_low(review) -> int:
    from myapp.notifications import staff_recipients

    reached = _notify(
        staff_recipients(),
        "review_low",
        params={"task_id": review.task_id, "review_id": review.pk, "stars": review.stars,
                "volunteer_name": _display_name(review.volunteer)},
        link=reverse("crm_reviews") + "?low=1",
        dedupe_key=f"review_low:{review.pk}",
    )
    if not reached:
        logger.error("review_low reached no staff review=%s task=%s", review.pk, review.task_id)
    return reached


def review_request(task) -> int:
    """The "rate the help" prompt to the task's client — called by the
    orchestrator when a task completes. Deduped per task, so a retry or a
    second completion path never prompts twice. No-op unless the task is
    reviewable (completed, with a volunteer, not yet reviewed)."""
    if task.status != "completed" or not task.volunteer_id or not task.client_id:
        return 0
    if TaskReview.objects.filter(task_id=task.pk).exists():
        return 0
    return _notify(
        [task.client],
        "review_request",
        params={"task_id": task.pk},
        link=reverse("task_review", args=[task.pk]),
        dedupe_key=f"review_request:{task.pk}",
    )


# --------------------------------------------------------------------------- #
# submit / edit
# --------------------------------------------------------------------------- #

def submit_review(client, task, *, stars, comment="", tags=()) -> Outcome:
    """Create the task's review, or edit it within the window.

    Codes: OK, FORBIDDEN (not the client of a completed task with a volunteer),
    EDIT_CLOSED (review exists, window over), INVALID_STARS, COMMENT_TOO_LONG,
    INVALID_TAGS. ``Outcome.created`` tells a create from an edit.
    """
    if not is_reviewable(client, task):
        return Outcome(False, FORBIDDEN)
    code, cleaned = validate(stars, comment, tags)
    if code != OK:
        return Outcome(False, code)
    stars, comment, tags = cleaned

    existing = TaskReview.objects.filter(task_id=task.pk).first()
    if existing is not None:
        return _edit(client, existing, stars, comment, tags)

    try:
        with transaction.atomic():
            review = TaskReview.objects.create(
                task=task, client=client, volunteer=task.volunteer,
                stars=stars, comment=comment, tags=tags,
                is_demo=bool(getattr(task, "is_demo", False)),  # a demo request's review is demo too
            )
    except IntegrityError:
        # A concurrent submit (double click, two tabs) created it first: apply
        # this one as an edit of the winner — last write wins, one row, one
        # review_received.
        existing = TaskReview.objects.filter(task_id=task.pk).first()
        if existing is None:
            raise
        return _edit(client, existing, stars, comment, tags)

    logger.info(
        "review created review=%s task=%s client=%s volunteer=%s stars=%s",
        review.pk, task.pk, client.pk, review.volunteer_id, stars,
    )
    recompute_volunteer_stats(review.volunteer_id)
    _notify(
        [task.volunteer],
        "review_received",
        params={"task_id": task.pk, "stars": stars},
        link=reverse("my_reviews"),
        dedupe_key=f"review_received:{review.pk}",
    )
    if stars <= LOW_STARS:
        _alert_low(review)
    _evaluate_badges(review)
    return Outcome(True, OK, review, created=True)


def _edit(client, review, stars, comment, tags) -> Outcome:
    if not can_edit(client, review):
        return Outcome(False, EDIT_CLOSED if review.client_id == client.pk else FORBIDDEN, review)
    old_stars = review.stars
    if (old_stars, review.comment, list(review.tags or [])) == (stars, comment, tags):
        return Outcome(True, NOT_CHANGED, review)
    fields = ["stars", "comment", "tags", "updated_at"]
    # Staff approved the old words for the About page, not the new ones.
    unfeatured = review.show_on_landing and review.comment != comment
    if unfeatured:
        review.show_on_landing = False
        fields.append("show_on_landing")
    review.stars, review.comment, review.tags = stars, comment, tags
    review.save(update_fields=fields)
    if unfeatured:
        cache.delete(ABOUT_COMMUNITY_CACHE_KEY)
    logger.info(
        "review edited review=%s task=%s client=%s stars=%s->%s",
        review.pk, review.task_id, client.pk, old_stars, stars,
    )
    if old_stars != stars:
        recompute_volunteer_stats(review.volunteer_id)
    # Newly dropped to a low rating: alert staff — the dedupe key makes sure a
    # review alerts at most once over its whole life (4→2→5→1 alerts once).
    if stars <= LOW_STARS < old_stars:
        _alert_low(review)
    _evaluate_badges(review)  # stars or tags changed — kind_words / trusted may now be met
    return Outcome(True, OK, review)


def _evaluate_badges(review):
    """Quality badges read visible reviews (services/achievements.py). Hiding
    never revokes a badge (owner decision), so only create / edit / unhide
    re-evaluate. Never raises."""
    from . import achievements

    achievements.award_safely(review.volunteer, "review")


# --------------------------------------------------------------------------- #
# moderation
# --------------------------------------------------------------------------- #

def hide(staff, review) -> Outcome:
    """Hide an abusive review (staff only). Conditional UPDATE — idempotent."""
    if not is_staff_user(staff):
        return Outcome(False, FORBIDDEN, review)
    now = timezone.now()
    changed = TaskReview.objects.filter(pk=review.pk, hidden_at__isnull=True).update(
        hidden_at=now, hidden_by=staff
    )
    if not changed:
        review.refresh_from_db()
        return Outcome(True, NOT_CHANGED, review)
    review.hidden_at, review.hidden_by = now, staff
    recompute_volunteer_stats(review.volunteer_id)
    logger.info("review hidden review=%s task=%s by=%s", review.pk, review.task_id, staff.pk)
    return Outcome(True, OK, review)


def unhide(staff, review) -> Outcome:
    if not is_staff_user(staff):
        return Outcome(False, FORBIDDEN, review)
    changed = TaskReview.objects.filter(pk=review.pk, hidden_at__isnull=False).update(
        hidden_at=None, hidden_by=None
    )
    if not changed:
        review.refresh_from_db()
        return Outcome(True, NOT_CHANGED, review)
    review.hidden_at, review.hidden_by = None, None
    recompute_volunteer_stats(review.volunteer_id)
    logger.info("review unhidden review=%s task=%s by=%s", review.pk, review.task_id, staff.pk)
    _evaluate_badges(review)
    return Outcome(True, OK, review)


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

def review_for_task(task):
    return TaskReview.objects.filter(task_id=task.pk).first()


def reviews_for(viewer, volunteer, page=1, *, per_page=REVIEWS_PAGE_SIZE):
    """A page of reviews (with comments) about ``volunteer``, or ``None`` when
    ``viewer`` may not see comments (anyone but that volunteer and staff).
    Hidden reviews are included — marked by ``hidden_at`` — for staff only."""
    if not can_see_comments(viewer, volunteer):
        return None
    vid = volunteer if isinstance(volunteer, int) else volunteer.pk
    qs = TaskReview.objects.filter(volunteer_id=vid).select_related("task").order_by("-created_at", "-id")
    if not is_staff_user(viewer):
        qs = qs.filter(hidden_at__isnull=True)
    return Paginator(qs, per_page).get_page(page)


def staff_reviews(*, low=False, hidden=None, volunteer_id=None):
    """The CRM list queryset. ``low`` → stars <= LOW_STARS; ``hidden`` True/False
    filters on moderation state, None = both; ``volunteer_id`` narrows to one
    volunteer (the CRM volunteer page links here)."""
    qs = TaskReview.objects.select_related("task", "client", "volunteer", "hidden_by").order_by(
        "-created_at", "-id"
    )
    if volunteer_id:
        qs = qs.filter(volunteer_id=volunteer_id)
    if low:
        qs = qs.filter(stars__lte=LOW_STARS)
    if hidden is True:
        qs = qs.filter(hidden_at__isnull=False)
    elif hidden is False:
        qs = qs.filter(hidden_at__isnull=True)
    return qs


# --------------------------------------------------------------------------- #
# About page testimonials
# --------------------------------------------------------------------------- #

LANDING_TESTIMONIALS = 3


def set_landing(staff, review, value: bool) -> Outcome:
    """Feature a review on the About page (or take it off). Staff only;
    conditional UPDATE — idempotent. Featuring does not check the stars: the
    page filter (5 stars, a comment, not hidden) decides what is shown."""
    if not is_staff_user(staff):
        return Outcome(False, FORBIDDEN, review)
    value = bool(value)
    changed = TaskReview.objects.filter(pk=review.pk).exclude(show_on_landing=value).update(
        show_on_landing=value
    )
    review.show_on_landing = value
    if not changed:
        return Outcome(True, NOT_CHANGED, review)
    cache.delete(ABOUT_COMMUNITY_CACHE_KEY)
    logger.info("review landing=%s review=%s task=%s by=%s", value, review.pk, review.task_id, staff.pk)
    return Outcome(True, OK, review)


def short_public_name(full_name: str) -> str:
    """"Ходжимурод Тошматов" -> "Ходжимурод Т.": the first word in full, every
    other word cut to its initial. "" when there is no name (the page then
    shows a neutral label — never the username)."""
    words = (full_name or "").split()
    if not words:
        return ""
    return " ".join([words[0], *(f"{w[0].upper()}." for w in words[1:])])


def _initials(full_name: str) -> str:
    return "".join(w[0].upper() for w in (full_name or "").split()[:2])


def landing_testimonials(limit: int = LANDING_TESTIMONIALS) -> list[dict]:
    """The newest staff-approved 5-star reviews with a comment, as plain dicts
    (one query, no model objects — nothing else can leak into the template):
    ``{"comment", "name", "initials", "help_type", "help_type_label", "region",
    "region_label"}`` (labels = the Russian fallback text; i18n.js translates)."""
    help_labels, region_labels = dict(HELP_TYPE_CHOICES), dict(REGION_CHOICES)
    rows = (
        TaskReview.objects.filter(
            show_on_landing=True, stars=MAX_STARS, hidden_at__isnull=True, client__is_active=True,
        )
        .exclude(comment="")
        .order_by("-created_at", "-pk")
        .values("comment", "client__profile__full_name", "client__region", "task__help_type")[:limit]
    )
    return [
        {
            "comment": row["comment"],
            "name": short_public_name(row["client__profile__full_name"]),
            "initials": _initials(row["client__profile__full_name"]),
            "help_type": row["task__help_type"],
            "help_type_label": help_labels.get(row["task__help_type"], ""),
            "region": row["client__region"] or "",
            "region_label": region_labels.get(row["client__region"], ""),
        }
        for row in rows
    ]
