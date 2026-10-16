"""Client reviews of a completed task (Feature Pack v2, feature 9).

One review per task, by its client, editable for ``REVIEW_EDIT_HOURS``. Comments
are visible to the volunteer and staff only; public shapes show only the
volunteer's average (``Profile.review_avg`` / ``review_count``, denormalised by
myapp/services/feedback.py). Display-only — not a matching weight (owner
decision §9.7). Staff can hide an abusive review (ID-only log).

``show_on_landing`` is the one exception to "comments are never public": staff
tick it (Django admin) to feature a 5-star comment on the About page, where the
client appears as "First L." only. Off by default; reset when the client
edits the comment, so an approved text can't be swapped afterwards.
"""

from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator
from django.db import models

from accounts.models import Users

from .help_requests import HelpRequest

REVIEW_COMMENT_MAX_LENGTH = 1000
# Fixed quick tags (keys; labels are i18n `review.tag.<key>`).
REVIEW_TAGS = ["polite", "punctual", "above_and_beyond", "careful", "friendly", "communicative"]


class TaskReview(models.Model):
    task = models.OneToOneField(HelpRequest, on_delete=models.CASCADE, related_name="review")
    client = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="reviews_given")
    volunteer = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="reviews_received")
    stars = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True, validators=[MaxLengthValidator(REVIEW_COMMENT_MAX_LENGTH)])
    tags = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    hidden_at = models.DateTimeField(null=True, blank=True)
    hidden_by = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    show_on_landing = models.BooleanField(
        "Показывать на главной", default=False,
        help_text="Отзыв попадёт на страницу «О нас», только если у него 5 звёзд, есть текст и он не скрыт.",
    )

    class Meta:
        verbose_name = "Отзыв"
        verbose_name_plural = "Отзывы"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["volunteer", "-created_at"], name="review_volunteer_idx")]

    def __str__(self):
        return f"#{self.task_id}: {self.stars}★"
