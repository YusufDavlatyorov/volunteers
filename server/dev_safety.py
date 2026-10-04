"""Loud Terminal warning when a DEBUG (local) run holds real delivery keys.

On the owner's Mac the local `.env` must not carry the production SMTP login or
the Telegram bot tokens: a local site would mail the demo users through the
real mailbox, and a local bot would steal updates from the production bot.
`settings.py` calls `warn_if_real_keys()`; it never prints a value, only names.
"""

import os
import sys

# Variables that make a local run talk to the outside world as production.
DELIVERY_KEYS = (
    'SMTP_USER',
    'SMTP_PASSWORD',
    'TELEGRAM_BOT_TOKEN',
    'TELEGRAM_COMMUNITY_BOT_TOKEN',
)

# The runserver autoreloader re-imports settings in a child process that
# inherits the environment — this marker keeps the banner to one copy.
_WARNED_MARKER = '_KHAYRKHOH_REAL_KEYS_WARNED'


def real_keys_present(env):
    """Names of the delivery keys that have a non-empty value in `env`."""
    return [name for name in DELIVERY_KEYS if (env.get(name) or '').strip()]


def warning_text(names):
    lines = [
        '',
        '!' * 72,
        '!!  WARNING / ВНИМАНИЕ: DEBUG mode with delivery keys in .env:',
        '!!  ' + ', '.join(names),
        '!!  If these are the PRODUCTION keys, this local copy sends REAL',
        '!!  e-mails / Telegram messages and steals the production bot\'s updates.',
        '!!  Если это ключи боевой почты или боевого бота — локальный сайт',
        '!!  отправит настоящие письма и сообщения. Очистите эти строки в .env.',
        '!!  Токен ТЕСТОВОГО бота (docs/TEST_BOT.md) — можно, это напоминание.',
        '!' * 72,
        '',
    ]
    return '\n'.join(lines)


def warn_if_real_keys(*, debug, env=None, argv=None, stream=None):
    """Print the banner once per process tree; return the offending names."""
    env = os.environ if env is None else env
    argv = sys.argv if argv is None else argv
    if not debug or (len(argv) > 1 and argv[1] == 'test'):
        return []
    names = real_keys_present(env)
    if not names or env.get(_WARNED_MARKER):
        return names
    env[_WARNED_MARKER] = '1'
    print(warning_text(names), file=stream or sys.stderr)
    return names
