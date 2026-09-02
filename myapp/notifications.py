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


def notify_users(users, subject, message):
    users = list(users)
    sent_email = send_email(subject, message, [user.email for user in users])
    sent_telegram = sum(1 for user in users if send_telegram_message(user.telegram_id, message))
    return sent_email + sent_telegram


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
