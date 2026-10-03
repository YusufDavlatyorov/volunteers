"""Telegram bots (Feature Pack v2, features 4 and 5).

* ``api`` — the sync Bot API client used from Django code (views, services,
  Celery): never raises, never logs a token.
* ``i18n`` — RU / TJ / EN bot texts.
* ``main_bot`` — the role-aware platform bot (``run_telegram_bot``).
* ``community_bot`` — the volunteer community bot (``run_community_bot``).

Both bots keep their logic in a transport-agnostic ``core`` (plain Python +
Django services, unit-tested without Telegram) and a thin python-telegram-bot
adapter (``app``) that turns updates into core calls and replies into API calls.
"""
