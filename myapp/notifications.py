import logging

import requests
from django.conf import settings
from django.core.mail import send_mail


logger = logging.getLogger(__name__)


def send_email(subject, message, recipients):
    recipients = [email for email in recipients if email]
    if not recipients:
        return 0
    return send_mail(
        subject=subject,
        message=message,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", settings.EMAIL_HOST_USER),
        recipient_list=recipients,
        fail_silently=True,
    )


def send_telegram_message(chat_id, text):
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning("Telegram send failed for %s: %s", chat_id, exc)
        return False


def _dispatch(task, *args):
    """Queue ``task`` on Celery; if the broker is unreachable, deliver inline
    rather than lose the notification (the request just takes longer)."""
    try:
        task.delay(*args)
    except Exception as exc:  # broker down / misconfigured
        logger.warning("notification queue unavailable (%s); delivering inline: %s", type(exc).__name__, task.name)
        try:
            task(*args)
        except Exception as inline_exc:
            logger.error("inline notification delivery failed: %s %s", task.name, type(inline_exc).__name__)


def queue_email(recipient, subject, message):
    """One transactional email (verification / password reset / welcome),
    delivered in the background like notify_users. Returns 1 if queued."""
    from .tasks import send_email_task

    if not recipient:
        return 0
    _dispatch(send_email_task, recipient, subject, message)
    return 1


def notify_users(users, subject, message):
    """Queue one email per address and one Telegram message per linked chat
    (myapp/tasks.py — delivered in the background with retries; inline when no
    broker is configured). Returns the number of deliveries queued: 0 means
    nobody in ``users`` is reachable at all, which the overdue / stale /
    emergency sweeps log at ERROR."""
    from .tasks import send_email_task, send_telegram_task

    users = list(users)
    queued = 0
    for email in dict.fromkeys(user.email for user in users if user.email):
        _dispatch(send_email_task, email, subject, message)
        queued += 1
    if getattr(settings, "TELEGRAM_BOT_TOKEN", ""):
        for chat_id in dict.fromkeys(user.telegram_id for user in users if user.telegram_id):
            _dispatch(send_telegram_task, chat_id, message)
            queued += 1
    return queued


def volunteer_queryset_for_region(region):
    from accounts.models import Users

    qs = Users.objects.filter(is_volunteer=True, is_active=True)
    if region and region != "all":
        qs = qs.filter(region=region)
    return qs


def staff_recipients():
    """Active curators and admins — the operational audience for coordination
    events (overdue tasks, emergency alerts, donation opportunities). Admin is
    ``is_superuser``, not a role flag, hence the OR."""
    from django.db.models import Q

    from accounts.models import Users

    return Users.objects.filter(Q(is_curator=True) | Q(is_superuser=True), is_active=True)
