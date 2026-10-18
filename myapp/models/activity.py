"""Per-day activity counter behind the profile heatmap (services/activity.py).

One row per user and local (Asia/Dushanbe) day. ``DailyActivityMiddleware``
writes real activity — the first page of the day, then +1 per successful
state-changing request (an ``F()`` UPDATE); ``backfill_activity`` restores the
days before it from real timestamps (``source``); ``seed_demo`` writes the demo
users' history (``is_demo=True``; ``seed_demo --reset`` deletes only those).
"""

from django.db import models

from accounts.models import Users


SOURCE_LIVE = "live"          # written by DailyActivityMiddleware
SOURCE_BACKFILL = "backfill"  # restored from real timestamps (manage.py backfill_activity)
SOURCE_MERGED = "merged"      # a live row that backfill_activity raised to the day's real events
SOURCE_DEMO = "demo"          # seed_demo (a demo stand only)
SOURCE_CHOICES = [
    (SOURCE_LIVE, "Сайт (мидлварь)"),
    (SOURCE_BACKFILL, "Восстановлено из истории"),
    (SOURCE_MERGED, "Сайт + история"),
    (SOURCE_DEMO, "Демо"),
]


class UserDailyActivity(models.Model):
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="daily_activity")
    date = models.DateField()
    actions_count = models.PositiveIntegerField(default=0)
    is_demo = models.BooleanField(default=False, db_index=True)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_LIVE, db_index=True)

    class Meta:
        verbose_name = "Активность за день"
        verbose_name_plural = "Активность по дням"
        ordering = ["-date"]
        constraints = [
            models.UniqueConstraint(fields=["user", "date"], name="uniq_user_daily_activity"),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.date}={self.actions_count}"
