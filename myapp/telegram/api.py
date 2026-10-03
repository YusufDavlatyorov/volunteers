"""Minimal sync Telegram Bot API client for Django-side calls (Feature Pack v2).

Used where there is no bot event loop: creating community invite links from a
web request, removing a member from a Celery task, mirroring a broadcast.
Every call returns the ``result`` (or ``None``) and **never raises**; failures
log the method, the HTTP status and the Telegram error code only — never the
URL (it contains the token), the token itself, or message text.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
TIMEOUT = 10


def call(token, method, **payload):
    """POST ``method`` with ``payload``; ``result`` on success, else None."""
    if not token:
        return None
    try:
        response = requests.post(f"{API_ROOT}/bot{token}/{method}", json=payload, timeout=TIMEOUT)
    except requests.RequestException as exc:
        logger.warning("telegram api %s failed: %s", method, type(exc).__name__)
        return None
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not data.get("ok"):
        logger.warning("telegram api %s failed: http=%s code=%s", method, response.status_code,
                       data.get("error_code"))
        return None
    return data.get("result")


def send_message(token, chat_id, text, **extra):
    return call(token, "sendMessage", chat_id=chat_id, text=text, **extra)


def create_invite_link(token, chat_id, *, name, expire_date, member_limit=1):
    result = call(token, "createChatInviteLink", chat_id=chat_id, name=name[:32],
                  expire_date=int(expire_date), member_limit=member_limit)
    return (result or {}).get("invite_link")


def kick(token, chat_id, user_id):
    """Remove without a permanent ban: ban + unban (the person can come back
    only with a new invite)."""
    banned = call(token, "banChatMember", chat_id=chat_id, user_id=user_id)
    call(token, "unbanChatMember", chat_id=chat_id, user_id=user_id, only_if_banned=True)
    return banned is not None


def pin(token, chat_id, message_id):
    return call(token, "pinChatMessage", chat_id=chat_id, message_id=message_id,
                disable_notification=True) is not None
