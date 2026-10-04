"""Logging filters attached to the console handler (server/settings.py LOGGING).

`RedactSecretsFilter` cuts Telegram bot tokens out of every log line and
traceback. Telegram puts the token in the URL path (`/bot<id>:<secret>/…`), so
any library that logs a request URL or an exception carrying one (requests,
httpx under python-telegram-bot) would otherwise write the token to
journald / `docker logs`. Milestone 5 fixed the one known call site; this is the
safety net for every other one.
"""

import logging
import re

# "123456789:AA…" — numeric bot id, colon, 30+ url-safe characters.
_BOT_TOKEN = re.compile(r"\d{5,}:[A-Za-z0-9_-]{30,}")
REDACTED = "<redacted-token>"


def redact(text):
    return _BOT_TOKEN.sub(REDACTED, text) if text else text


class RedactSecretsFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True
