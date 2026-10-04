"""Background notification delivery (Celery).

``myapp.notifications.notify_users`` queues one task per email address and one
per linked Telegram chat, so a slow or failing SMTP server / Telegram API never
holds up a web request, and a failure retries just that one delivery (never a
duplicate to the others). Retries back off exponentially with jitter; a
delivery that still fails after the last retry is logged at ERROR — with the
task id and channel only, never the address or message body.

Without a broker (tests, local dev) settings run these inline ("eager").
"""

import logging

from celery import Task, shared_task
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

MAX_RETRIES = 5


class DeliveryError(Exception):
    """A delivery attempt failed and should be retried."""


class _DeliveryTask(Task):
    autoretry_for = (Exception,)
    max_retries = MAX_RETRIES
    retry_backoff = True          # 1s, 2s, 4s, 8s, 16s …
    retry_backoff_max = 600
    retry_jitter = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        logger.error(
            "notification delivery failed permanently: task=%s id=%s error=%s",
            self.name, task_id, type(exc).__name__,
        )


@shared_task(base=_DeliveryTask, name="notifications.send_email")
def send_email_task(recipient, subject, message):
    """One email to one recipient (recipients never see each other's address)."""
    sent = send_mail(
        subject=subject,
        message=message,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
        recipient_list=[recipient],
        fail_silently=False,
    )
    if not sent:
        raise DeliveryError("email backend reported 0 messages sent")
    return sent


@shared_task(base=_DeliveryTask, name="notifications.send_telegram")
def send_telegram_task(chat_id, text):
    from .notifications import send_telegram_message

    if not send_telegram_message(chat_id, text):
        raise DeliveryError("telegram send failed")
    return True


@shared_task(name="chat.make_image_variants", ignore_result=True)
def make_chat_image_variants(message_id):
    """WebP thumb + medium for one chat image (services/images.py), then a
    ``chat.message_updated`` event so open chats swap the thumb in. A broken
    source is logged (id only) and left as is — the original stays usable."""
    from .services import chat

    try:
        return chat.apply_image_variants(message_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat image variants failed message=%s error=%s", message_id, type(exc).__name__)
        return None


@shared_task(name="images.make_variants", ignore_result=True)
def make_image_variants(label, pk):
    """WebP thumb + medium for an avatar / photo report / pet photo
    (services/images.apply_variants). A broken source is logged (id only); the
    original stays in use — templates fall back to it."""
    from .services import images

    try:
        return images.apply_variants(label, pk)
    except Exception as exc:  # noqa: BLE001
        logger.warning("image variants failed model=%s id=%s error=%s", label, pk, type(exc).__name__)
        return None


@shared_task(name="community.revoke", ignore_result=True)
def community_revoke_task(user_id, reason="not_eligible"):
    """Remove a no-longer-eligible user from the volunteer community chats
    (services/community.revoke). Telegram failures are logged there (IDs only)."""
    from accounts.models import Users

    from .services import community

    user = Users.objects.filter(pk=user_id).first()
    return community.revoke(user, reason) if user is not None else 0


@shared_task(name="community.mirror", ignore_result=True)
def community_mirror_task(kind, object_id):
    """Pin a website broadcast / new event in the volunteer community."""
    from .models import Broadcast, Event
    from .services import community

    if kind == "broadcast":
        obj = Broadcast.objects.filter(pk=object_id).first()
        return community.mirror_broadcast(obj) if obj else 0
    if kind == "event":
        obj = Event.objects.filter(pk=object_id).first()
        return community.mirror_event(obj) if obj else 0
    return 0
