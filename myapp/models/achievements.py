"""Volunteer achievements with badges (Feature Pack v2, feature 6).

Owner decisions (milestone 4, 2026-10-02): the set rewards contribution,
quality and steadiness — **never a race**: no time-boxed "top of the month",
no daily streaks, no night-shift badges, nothing for raising an SOS, no
points threshold; badges are never ranked against other volunteers. Quality
badges count *visible* reviews only (hidden ones never count); a task with a
visible 1–2 star review does not count towards the task milestones. A badge
once awarded is not revoked automatically (staff can remove a
``UserAchievement`` row in the Django admin — ID-only log). Achievements and
reviews are **not** a matching signal (``services/matching.py`` never reads
them; test-locked).

The rules (one evaluator per ``code``) live in ``myapp/services/achievements.py``;
this table only carries what the owner may tweak in the Django admin: the
threshold, tier, order and whether a badge is active. Titles / descriptions are
i18n keys ``ach.<code>.title`` / ``ach.<code>.desc``; the badge art is
``static/images/badges/<code>.svg``.
"""

from django.db import models

from accounts.models import Users

TIER_BRONZE = "bronze"
TIER_SILVER = "silver"
TIER_GOLD = "gold"
TIER_LEGENDARY = "legendary"
ACHIEVEMENT_TIER_CHOICES = [
    (TIER_BRONZE, "Бронза"),
    (TIER_SILVER, "Серебро"),
    (TIER_GOLD, "Золото"),
    (TIER_LEGENDARY, "Легенда"),
]
# Higher = rarer; public cards show the rarest badges first.
TIER_RANK = {TIER_BRONZE: 1, TIER_SILVER: 2, TIER_GOLD: 3, TIER_LEGENDARY: 4}

CATEGORY_MILESTONE = "milestone"
CATEGORY_QUALITY = "quality"
CATEGORY_STEADY = "steady"
CATEGORY_VERSATILE = "versatile"
CATEGORY_OTHER = "other"
ACHIEVEMENT_CATEGORY_CHOICES = [
    (CATEGORY_MILESTONE, "Вклад"),
    (CATEGORY_QUALITY, "Качество"),
    (CATEGORY_STEADY, "Постоянство"),
    (CATEGORY_VERSATILE, "Разносторонность"),
    (CATEGORY_OTHER, "Другие виды помощи"),
]


class Achievement(models.Model):
    code = models.SlugField(max_length=40, unique=True)
    tier = models.CharField(max_length=12, choices=ACHIEVEMENT_TIER_CHOICES, default=TIER_BRONZE)
    category = models.CharField(max_length=12, choices=ACHIEVEMENT_CATEGORY_CHOICES, default=CATEGORY_MILESTONE)
    # The target the badge's progress counts towards (tasks, reviews, months…).
    threshold = models.PositiveIntegerField(default=1)
    sort_order = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Достижение"
        verbose_name_plural = "Достижения"
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.code

    @property
    def title_key(self):
        return f"ach.{self.code}.title"

    @property
    def desc_key(self):
        return f"ach.{self.code}.desc"

    @property
    def icon_path(self):
        return f"images/badges/{self.code}.svg"


class UserAchievement(models.Model):
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="achievements")
    achievement = models.ForeignKey(Achievement, on_delete=models.CASCADE, related_name="awards")
    awarded_at = models.DateTimeField(auto_now_add=True)
    # The counter value when the badge was earned (e.g. 10 tasks) — history only.
    progress_snapshot = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "Полученное достижение"
        verbose_name_plural = "Полученные достижения"
        ordering = ["-awarded_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "achievement"], name="uniq_user_achievement"),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.achievement_id}"
