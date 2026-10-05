"""Long polling for both bots — the one place for the ``run_polling`` arguments.

``bootstrap_retries=-1``: if Telegram is unreachable while the bot starts
(``getMe`` / ``deleteWebhook`` — the server's uplink drops for seconds at a
time: 31 "Network is unreachable" in 4 days, 2026-10), keep retrying with
python-telegram-bot's backoff (1 s → ×1.5 → 30 s) instead of exiting; the
default (0) made the process die and systemd restart it. A wrong token is not
retried: PTB aborts on ``InvalidToken`` and the process exits at once.
Once polling runs, PTB already retries network errors, timeouts and 5xx
indefinitely. Tests: ``myapp.tests_telegram_resilience``.
"""

from django.core.management.base import CommandError
from telegram.error import InvalidToken

BOOTSTRAP_RETRIES = -1


def run(application, allowed_updates):
    """Block in long polling until SIGINT/SIGTERM."""
    try:
        application.run_polling(
            allowed_updates=allowed_updates,
            drop_pending_updates=False,
            bootstrap_retries=BOOTSTRAP_RETRIES,
        )
    except InvalidToken:
        # PTB writes the token itself into this exception's text ("The token `…`
        # was rejected"); left uncaught, Python prints that traceback straight to
        # stderr → journald, past the logging redaction filter. `from None` drops
        # it from the chain; Django prints only this line and exits 1.
        raise CommandError(
            "Telegram rejected the bot token (InvalidToken) — check the token in .env"
        ) from None
