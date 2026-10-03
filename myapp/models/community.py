"""Volunteer community on Telegram (Feature Pack v2, feature 5).

The second bot (``run_community_bot``) manages one general supergroup plus
optional per-region groups (``TELEGRAM_COMMUNITY_CHAT_IDS``). Rules live in
``myapp/services/community.py``; these tables are its memory:

* ``CommunityInvite`` — a single-use, 24-hour invite link minted for one
  eligible user (approved volunteer or staff with a linked Telegram). Only the
  SHA-256 of the link is stored (like the account-link tokens); the raw link
  is shown once.
* ``CommunityMember`` — who is in which chat (bound to an account when they
  joined through their invite); ``left_at`` set when they leave / are removed.
* ``ModerationAction`` — warn / mute / ban / kick / delete / report / mentor:
  the moderation audit trail (IDs + a short reason, never message text).
* ``CommunityPost`` — what the bot already posted (mirrored broadcast / event,
  weekly "thank you" post), unique per (kind, ref, chat) so a retry never posts
  twice.
"""

from django.db import models

from accounts.models import Users


class CommunityInvite(models.Model):
    user = models.ForeignKey(Users, on_delete=models.CASCADE, related_name="community_invites")
    chat_id = models.BigIntegerField()
    link_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_by_tg_id = models.BigIntegerField(null=True, blank=True)

    class Meta:
        verbose_name = "Приглашение в сообщество"
        verbose_name_plural = "Приглашения в сообщество"
        indexes = [models.Index(fields=["user", "chat_id"], name="cinvite_user_chat_idx")]

    def __str__(self):
        return f"{self.user_id}@{self.chat_id}"


class CommunityMember(models.Model):
    chat_id = models.BigIntegerField()
    tg_user_id = models.BigIntegerField()
    user = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="community_memberships")
    joined_at = models.DateTimeField(auto_now_add=True)
    left_at = models.DateTimeField(null=True, blank=True)
    removed_reason = models.CharField(max_length=40, blank=True)

    class Meta:
        verbose_name = "Участник сообщества"
        verbose_name_plural = "Участники сообщества"
        constraints = [models.UniqueConstraint(fields=["chat_id", "tg_user_id"], name="uniq_community_member")]

    def __str__(self):
        return f"{self.tg_user_id}@{self.chat_id}"


class ModerationAction(models.Model):
    WARN = "warn"
    MUTE = "mute"
    BAN = "ban"
    KICK = "kick"
    DELETE = "delete"
    REPORT = "report"
    MENTOR = "mentor"
    ACTION_CHOICES = [
        (WARN, "Предупреждение"), (MUTE, "Запрет писать"), (BAN, "Бан"), (KICK, "Удалён из чата"),
        (DELETE, "Сообщение удалено"), (REPORT, "Жалоба"), (MENTOR, "Значок «Наставник»"),
    ]

    chat_id = models.BigIntegerField()
    target_tg_id = models.BigIntegerField(null=True, blank=True)
    target_user = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    actor = models.ForeignKey(Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    reason = models.CharField(max_length=300, blank=True)  # a code / short note — never message text
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Действие модерации"
        verbose_name_plural = "Модерация сообщества"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["chat_id", "target_tg_id", "action"], name="modaction_target_idx")]

    def __str__(self):
        return f"{self.action}:{self.target_tg_id}@{self.chat_id}"


class CommunityPost(models.Model):
    KIND_BROADCAST = "broadcast"
    KIND_EVENT = "event"
    KIND_WEEKLY = "weekly"
    KIND_CHOICES = [(KIND_BROADCAST, "Рассылка"), (KIND_EVENT, "Акция"), (KIND_WEEKLY, "Спасибо недели")]

    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    ref = models.CharField(max_length=64)  # broadcast / event id, ISO week
    chat_id = models.BigIntegerField()
    message_id = models.BigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Пост в сообществе"
        verbose_name_plural = "Посты в сообществе"
        constraints = [models.UniqueConstraint(fields=["kind", "ref", "chat_id"], name="uniq_community_post")]

    def __str__(self):
        return f"{self.kind}:{self.ref}@{self.chat_id}"
