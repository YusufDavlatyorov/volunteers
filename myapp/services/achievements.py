"""Volunteer achievements (Feature Pack v2, feature 6) — the award engine.

Owner-approved "no race" set (models/achievements.py docstring): contribution
milestones without deadlines, quality from **visible** reviews only,
steadiness counted in months (never daily streaks), versatility, and the
other kinds of help (donation deliveries, pet reunions). Nothing here is ever
read by ``services/matching.py`` — achievements (like reviews) are not a
matching signal; test-locked in ``myapp/tests_achievements.py``.

Public API:

* ``counters(user) -> dict`` — the raw numbers behind every badge, in a
  constant number of aggregate queries (4), whatever the volunteer's history.
* ``progress_for(user) -> list[dict]`` — every active badge (+ earned inactive
  ones) for the profile grid: earned / awarded_at / value / threshold /
  percent. One more query for the awards.
* ``evaluate(user, *, send_notifications=True) -> list[UserAchievement]`` —
  award every newly met badge. Idempotent via the (user, achievement) unique
  constraint: a concurrent evaluation never awards (or notifies) twice.
  Volunteers only. Never revokes (owner decision).
* ``award_safely(user, event)`` — what event sites call (task completed,
  review saved / unhidden, donation delivered, pet report resolved). Never
  raises into the caller; ID-only log on failure.
* ``top_badges(user_ids, limit=3) -> {user_id: [Achievement, …]}`` — rarest
  first, for public cards (one query).
* ``ui_i18n_keys(lang)`` — the ``ach.*`` strings (source of the i18n.js keys).

Rules that keep quality first:

* task milestones / months / help types count completed tasks **unless** the
  task has a visible review of ``feedback.LOW_STARS`` (1–2) or less;
* ``kind_words`` / ``trusted`` read visible reviews only (hidden never count);
* ``trusted`` also needs an average of ``TRUSTED_MIN_AVG`` (4.5).

Logs carry IDs and badge codes only.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.urls import reverse
from django.utils import timezone

from ..models import (
    TIER_RANK,
    Achievement,
    Donation,
    HelpRequest,
    PetReport,
    TaskReview,
    UserAchievement,
)
from . import notify_texts

logger = logging.getLogger(__name__)

# Months are counted on the local calendar (TIME_ZONE is UTC).
LOCAL_TZ = ZoneInfo("Asia/Dushanbe")
LOW_STARS = 2  # == feedback.LOW_STARS (kept local: feedback imports nothing from here)
QUALITY_TAGS = ("polite", "above_and_beyond", "careful")
TRUSTED_MIN_AVG = 4.5
YEAR_WINDOW = timedelta(days=365)
PUBLIC_BADGES = 3

# code -> counter key (the value compared with Achievement.threshold)
COUNTER_FOR = {
    "first_help": "tasks",
    "tasks_10": "tasks",
    "tasks_50": "tasks",
    "tasks_100": "tasks",
    "kind_words": "kind_reviews",
    "trusted": "visible_reviews",
    "steady_helper": "months",
    "year_together": "months_year",
    "versatile": "help_types",
    "donation_courier": "donations_delivered",
    "pet_friend": "pets_reunited",
    "mentor": "mentor_actions",
}
# Badges whose explanation in the modal carries an extra rule line.
RULE_KEYS = {
    "milestone": "ach.rule_milestone",
    "quality": "ach.rule_quality",
    "steady": "ach.rule_steady",
    "versatile": "ach.rule_milestone",
}


def _good_task_q():
    """A completed task counts unless it holds a *visible* review of 1–2 stars."""
    return Q(review__isnull=True) | Q(review__hidden_at__isnull=False) | Q(review__stars__gt=LOW_STARS)


def counters(user) -> dict:
    """Every number the badges need — 4 aggregate queries, constant."""
    uid = getattr(user, "pk", user)
    now = timezone.now()
    good = _good_task_q()
    month = TruncMonth("completed_at", tzinfo=LOCAL_TZ)
    tasks = HelpRequest.objects.filter(volunteer_id=uid, status="completed").aggregate(
        tasks=Count("id", filter=good),
        help_types=Count("help_type", filter=good, distinct=True),
        months=Count(month, filter=good & Q(completed_at__isnull=False), distinct=True),
        months_year=Count(month, filter=good & Q(completed_at__gte=now - YEAR_WINDOW), distinct=True),
    )
    stars_and_tags = list(
        TaskReview.objects.filter(volunteer_id=uid, hidden_at__isnull=True).values_list("stars", "tags")
    )
    visible = len(stars_and_tags)
    avg = round(sum(s for s, _ in stars_and_tags) / visible, 2) if visible else None
    kind = sum(1 for _, tags in stars_and_tags if set(tags or ()) & set(QUALITY_TAGS))
    delivered = Donation.objects.filter(
        assigned_volunteer_id=uid, status__in=("received", "distributed")
    ).count()
    reunited = PetReport.objects.filter(reporter_id=uid, status=PetReport.STATUS_RESOLVED).count()
    return {
        "tasks": tasks["tasks"] or 0,
        "help_types": tasks["help_types"] or 0,
        "months": tasks["months"] or 0,
        "months_year": tasks["months_year"] or 0,
        "visible_reviews": visible,
        "review_avg": avg,
        "kind_reviews": kind,
        "donations_delivered": delivered,
        "pets_reunited": reunited,
        "mentor_actions": 0,  # community bot (milestone 5)
    }


def value_for(achievement, numbers) -> int:
    return int(numbers.get(COUNTER_FOR.get(achievement.code, ""), 0) or 0)


def is_met(achievement, numbers) -> bool:
    if achievement.code not in COUNTER_FOR:
        return False  # a row added in the admin without a rule never awards itself
    if value_for(achievement, numbers) < achievement.threshold:
        return False
    if achievement.code == "trusted":
        avg = numbers.get("review_avg")
        return avg is not None and avg >= TRUSTED_MIN_AVG
    return True


def _eligible(user) -> bool:
    return bool(user is not None and getattr(user, "pk", None) and user.is_active and user.is_volunteer)


# --------------------------------------------------------------------------- #
# awarding
# --------------------------------------------------------------------------- #

def evaluate(user, *, send_notifications=True) -> list:
    """Award every badge ``user`` now meets and doesn't hold yet."""
    if not _eligible(user):
        return []
    numbers = counters(user)
    held = set(UserAchievement.objects.filter(user=user).values_list("achievement_id", flat=True))
    awarded = []
    for achievement in Achievement.objects.filter(is_active=True):
        if achievement.pk in held or not is_met(achievement, numbers):
            continue
        try:
            with transaction.atomic():
                row = UserAchievement.objects.create(
                    user=user, achievement=achievement, progress_snapshot=value_for(achievement, numbers)
                )
        except IntegrityError:
            continue  # a concurrent evaluation won — it also sends the notice
        logger.info("achievement awarded user=%s code=%s", user.pk, achievement.code)
        awarded.append(row)
        if send_notifications:
            _notify(user, achievement)
    return awarded


def _notify(user, achievement):
    from .notify import notify

    try:
        notify(
            [user], "achievement_unlocked",
            params={"achievement_code": achievement.code},
            link=reverse("profile") + "#achievements",
            dedupe_key=f"achievement:{user.pk}:{achievement.code}",
        )
    except Exception:  # noqa: BLE001 — the badge is saved; a fan-out hiccup must not undo it
        logger.exception("achievement notify failed user=%s code=%s", user.pk, achievement.code)


def award_safely(user, event="") -> list:
    """Event hook: never raises into the view / service that called it."""
    try:
        return evaluate(user)
    except Exception:  # noqa: BLE001
        logger.exception("achievement evaluation failed user=%s event=%s", getattr(user, "pk", None), event)
        return []


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #

def progress_for(user) -> list:
    """The profile grid: active badges + earned ones that were deactivated."""
    numbers = counters(user)
    awards = {
        row.achievement_id: row
        for row in UserAchievement.objects.filter(user=user).only("achievement_id", "awarded_at")
    }
    items = []
    for achievement in Achievement.objects.all():
        award = awards.get(achievement.pk)
        if award is None and not achievement.is_active:
            continue
        value = value_for(achievement, numbers)
        threshold = max(achievement.threshold, 1)
        shown = threshold if award else min(value, threshold)
        items.append({
            "achievement": achievement,
            "code": achievement.code,
            "tier": achievement.tier,
            "category": achievement.category,
            "earned": award is not None,
            "awarded_at": award.awarded_at if award else None,
            "value": shown,
            "threshold": threshold,
            "percent": 100 if award else int(min(value, threshold) * 100 / threshold),
            "rule_key": RULE_KEYS.get(achievement.category, ""),
            "review_avg": numbers["review_avg"] if achievement.code == "trusted" else None,
            "coming_soon": not achievement.is_active and award is None,
        })
    return items


def top_badges(user_ids, limit=PUBLIC_BADGES) -> dict:
    """``{user_id: [Achievement, …]}`` — rarest tier first, then the set's
    order; one query for any number of users. Inactive badges stay shown if
    earned (a badge is never taken back)."""
    ids = [uid for uid in {getattr(u, "pk", u) for u in user_ids} if uid]
    out = {uid: [] for uid in ids}
    if not ids:
        return out
    rows = UserAchievement.objects.filter(user_id__in=ids).select_related("achievement")
    grouped = {}
    for row in rows:
        grouped.setdefault(row.user_id, []).append(row.achievement)
    for uid, items in grouped.items():
        items.sort(key=lambda a: (-TIER_RANK.get(a.tier, 0), a.sort_order, a.pk))
        out[uid] = items[:limit]
    return out


# --------------------------------------------------------------------------- #
# texts (source of the ach.* keys in static/js/i18n.js)
# --------------------------------------------------------------------------- #

DESCRIPTIONS = {
    "first_help": {"en": "Completed your first help request.",
                   "ru": "Выполнен первый запрос о помощи.",
                   "tj": "Аввалин дархости кумак иҷро шуд."},
    "tasks_10": {"en": "Completed 10 help requests.",
                 "ru": "Выполнено 10 запросов о помощи.",
                 "tj": "10 дархости кумак иҷро шуд."},
    "tasks_50": {"en": "Completed 50 help requests.",
                 "ru": "Выполнено 50 запросов о помощи.",
                 "tj": "50 дархости кумак иҷро шуд."},
    "tasks_100": {"en": "Completed 100 help requests.",
                  "ru": "Выполнено 100 запросов о помощи.",
                  "tj": "100 дархости кумак иҷро шуд."},
    "kind_words": {"en": "5 reviews where clients noted your politeness, care or help beyond expectations.",
                   "ru": "5 отзывов, где клиенты отметили вежливость, внимательность или помощь сверх ожидаемого.",
                   "tj": "5 фикре, ки дар онҳо мизоҷон хушмуомилагӣ, диққат ё кумаки аз интизорӣ зиёдро қайд карданд."},
    "trusted": {"en": "10 reviews with an average rating of 4.5 or higher.",
                "ru": "10 отзывов со средней оценкой 4,5 или выше.",
                "tj": "10 фикр бо баҳои миёнаи 4,5 ё баландтар."},
    "steady_helper": {"en": "Helped in three different months — no rush, just steady.",
                      "ru": "Помогали в трёх разных месяцах — без спешки, но постоянно.",
                      "tj": "Дар се моҳи гуногун кумак кардед — бе шитоб, вале доимӣ."},
    "year_together": {"en": "Helped in six different months over the last year.",
                      "ru": "Помогали в шести разных месяцах за последний год.",
                      "tj": "Дар давоми як соли охир дар шаш моҳи гуногун кумак кардед."},
    "versatile": {"en": "Completed requests of four different kinds of help.",
                  "ru": "Выполнены запросы четырёх разных видов помощи.",
                  "tj": "Дархостҳои чор навъи гуногуни кумак иҷро шуданд."},
    "donation_courier": {"en": "Delivered your first in-kind donation.",
                         "ru": "Доставлено первое пожертвование вещами.",
                         "tj": "Аввалин хайрияи ашёӣ расонида шуд."},
    "pet_friend": {"en": "Your lost-and-found pet report ended in a happy reunion.",
                   "ru": "Ваше объявление о животном закончилось счастливой встречей.",
                   "tj": "Эълони шумо дар бораи ҳайвон бо вохӯрии хушҳолона анҷом ёфт."},
    "mentor": {"en": "Helps newcomers in the volunteer community (coming soon).",
               "ru": "Помогает новичкам в сообществе волонтёров (скоро).",
               "tj": "Ба навомадагон дар ҷамъияти ихтиёриён кумак мекунад (ба зудӣ)."},
}

UI_STRINGS = {
    "ach.section_title": {"en": "Achievements", "ru": "Достижения", "tj": "Дастовардҳо"},
    "ach.section_lede": {
        "en": "Badges mark your contribution, care and steadiness — no race and no comparison with others. They never affect which requests you are offered.",
        "ru": "Значки отмечают ваш вклад, заботу и постоянство — без гонки и сравнения с другими. На подбор запросов они не влияют.",
        "tj": "Нишонҳо саҳм, ғамхорӣ ва доимии шуморо қайд мекунанд — бе мусобиқа ва муқоиса бо дигарон. Ба интихоби дархостҳо таъсир намерасонанд.",
    },
    "ach.badges": {"en": "Badges", "ru": "Значки", "tj": "Нишонҳо"},
    "ach.earned_on": {"en": "Earned on", "ru": "Получен", "tj": "Гирифта шуд"},
    "ach.status_earned": {"en": "Earned", "ru": "Получен", "tj": "Гирифта шуд"},
    "ach.status_locked": {"en": "Not earned yet", "ru": "Ещё не получен", "tj": "Ҳанӯз гирифта нашудааст"},
    "ach.coming_soon": {"en": "Coming soon", "ru": "Скоро", "tj": "Ба зудӣ"},
    "ach.progress": {"en": "Progress", "ru": "Прогресс", "tj": "Пешрафт"},
    "ach.close": {"en": "Close", "ru": "Закрыть", "tj": "Пӯшидан"},
    "ach.open_hint": {"en": "Tap a badge to see what it is for.", "ru": "Нажмите на значок, чтобы узнать, за что он.",
                      "tj": "Нишонро пахш кунед, то бифаҳмед барои чист."},
    "ach.tier.bronze": {"en": "Bronze", "ru": "Бронза", "tj": "Биринҷӣ"},
    "ach.tier.silver": {"en": "Silver", "ru": "Серебро", "tj": "Нуқра"},
    "ach.tier.gold": {"en": "Gold", "ru": "Золото", "tj": "Тилло"},
    "ach.tier.legendary": {"en": "Legendary", "ru": "Легенда", "tj": "Афсонавӣ"},
    "ach.rule_milestone": {"en": "Completed requests count unless the client rated them 1–2 stars.",
                           "ru": "Засчитываются выполненные запросы, кроме оценённых клиентом на 1–2 звезды.",
                           "tj": "Дархостҳои иҷрошуда ҳисоб мешаванд, ба истиснои онҳое, ки мизоҷ бо 1–2 ситора баҳо додааст."},
    "ach.rule_quality": {"en": "Only visible reviews count; hidden reviews never do.",
                         "ru": "Учитываются только видимые отзывы; скрытые не засчитываются.",
                         "tj": "Танҳо фикрҳои намоён ҳисоб мешаванд; фикрҳои пинҳоншуда ҳисоб намешаванд."},
    "ach.rule_steady": {"en": "Months with at least one completed request count — no daily streaks.",
                        "ru": "Считаются месяцы, в которых был хотя бы один выполненный запрос, — без ежедневных серий.",
                        "tj": "Моҳҳое ҳисоб мешаванд, ки ақаллан як дархост иҷро шудааст — бе силсилаи ҳаррӯза."},
    "ach.review_avg": {"en": "Average rating: {avg}", "ru": "Средняя оценка: {avg}", "tj": "Баҳои миёна: {avg}"},
    "ach.none_yet": {"en": "No badges yet — the first one comes with your first completed request.",
                     "ru": "Значков пока нет — первый появится после первого выполненного запроса.",
                     "tj": "Ҳоло нишон нест — аввалинаш пас аз аввалин дархости иҷрошуда пайдо мешавад."},
}


def ui_i18n_keys(lang) -> dict:
    """{key: text} of every ``ach.*`` string for one language."""
    titles = notify_texts.LABELS["achievement"].get(lang) or notify_texts.LABELS["achievement"]["en"]
    flat = {f"ach.{code}.title": title for code, title in titles.items()}
    flat.update({f"ach.{code}.desc": texts[lang] for code, texts in DESCRIPTIONS.items()})
    flat.update({key: texts[lang] for key, texts in UI_STRINGS.items()})
    return flat
