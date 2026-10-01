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
