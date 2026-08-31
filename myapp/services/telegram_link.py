"""Secure Telegram account-linking.

Replaces the old ``/link <username>`` bot command, which bound the sender's
Telegram chat to *any* account given only a (publicly guessable) username — no
authentication, no confirmation, no proof the sender owned that account.

Linking now needs a one-time, high-entropy code that the account holder
generates **while authenticated** in the web UI
(:func:`accounts.views.telegram_link_view`) and then sends to the bot *from the
Telegram chat they want bound*. That proves control of both sides:

* the app account — they were logged in when the code was minted;
* the Telegram chat — the redeeming message physically originates from it.

Only the SHA-256 hash of the code is stored (``accounts.models.hash_token``),
the code is single-use and short-lived (``TELEGRAM_LINK_TOKEN_TTL``), and
redemption attempts are rate-limited per chat id so the code space cannot be
brute-forced.
"""

from collections import namedtuple

from django.core.cache import cache
from django.db import IntegrityError, transaction

from accounts.models import Users, hash_token


# Per-chat brute-force ceiling. The bot is a single long-lived process that
# handles every Telegram message sequentially, so a process-local cache counter
# is an effective throttle here (unlike the multi-process web tier). Mirrors the
# login throttle in accounts.views.
LINK_ATTEMPT_LIMIT = 5
LINK_ATTEMPT_WINDOW_SECONDS = 15 * 60

# ok:               did the chat get linked?
# code:             machine-readable status ("linked" / "invalid" / "throttled" / "chat_in_use")
# message:          user-facing text the bot relays verbatim
# user:             the linked Users row (only when ok)
# previous_chat_id: the chat this account was bound to before, if it changed
LinkOutcome = namedtuple("LinkOutcome", ["ok", "code", "message", "user", "previous_chat_id"])


def _throttle_key(chat_id):
    return f"telegram_link_attempts:chat:{chat_id}"


def _register_failed_attempt(key):
    # cache.set (not incr) so a missing key starts cleanly at 1; the sliding
    # window is refreshed on every failure, matching the login limiter.
    cache.set(key, cache.get(key, 0) + 1, LINK_ATTEMPT_WINDOW_SECONDS)


def redeem_link_code(chat_id, raw_code):
    """Bind ``chat_id`` to the account that minted ``raw_code``.

    Returns a :data:`LinkOutcome`. Never raises for an expected failure
    (bad / expired / already-used code, throttled, chat already bound to a
    different account) so the caller can simply relay ``outcome.message``.
    """
    key = _throttle_key(chat_id)
    if cache.get(key, 0) >= LINK_ATTEMPT_LIMIT:
        return LinkOutcome(
            False, "throttled",
            "Слишком много попыток привязки. Попробуйте снова через 15 минут.",
            None, None,
        )

    raw_code = (raw_code or "").strip()
    if not raw_code:
        _register_failed_attempt(key)
        return LinkOutcome(
            False, "invalid",
            "Неверный код. Получите новый код на сайте: Профиль → «Привязать Telegram».",
            None, None,
        )

    token_hash = hash_token(raw_code)
    user = Users.objects.filter(telegram_link_token=token_hash).first()
    if user is None or not user.telegram_link_token_is_valid():
        _register_failed_attempt(key)
        return LinkOutcome(
            False, "invalid",
            "Неверный или просроченный код. Сгенерируйте новый код в профиле на сайте.",
            None, None,
        )

    # This Telegram chat is already bound to a *different* account. Refuse
    # rather than silently move the binding — the person here has not proven
    # control of that other account. (telegram_id is unique=True, so the
    # conditional UPDATE below would also fail; this is the friendly path.)
    if Users.objects.filter(telegram_id=chat_id).exclude(pk=user.pk).exists():
        _register_failed_attempt(key)
        return LinkOutcome(
            False, "chat_in_use",
            "Этот Telegram уже привязан к другому аккаунту. "
            "Сначала отвяжите его в настройках того аккаунта.",
            None, None,
        )

    previous_chat_id = user.telegram_id if (user.telegram_id and user.telegram_id != chat_id) else None

    # Single conditional UPDATE: the token is consumed in the same statement
    # that checks it is still present, so a replayed or concurrently-redeemed
    # code matches zero rows the second time instead of linking twice. Wrapped
    # in a savepoint so a unique-collision on telegram_id (a race with another
    # account claiming the same chat) is catchable without poisoning an outer
    # transaction.
    try:
        with transaction.atomic():
            updated = Users.objects.filter(
                pk=user.pk, telegram_link_token=token_hash
            ).update(
                telegram_id=chat_id,
                telegram_link_token=None,
                telegram_link_token_created_at=None,
            )
    except IntegrityError:
        _register_failed_attempt(key)
        return LinkOutcome(
            False, "chat_in_use",
            "Этот Telegram уже привязан к другому аккаунту.",
            None, None,
        )

    if not updated:
        _register_failed_attempt(key)
        return LinkOutcome(
            False, "invalid",
            "Код уже был использован. Сгенерируйте новый код в профиле.",
            None, None,
        )

    cache.delete(key)
    user.refresh_from_db(
        fields=["telegram_id", "telegram_link_token", "telegram_link_token_created_at"]
    )
    return LinkOutcome(
        True, "linked",
        f"Готово! Telegram привязан к аккаунту {user.username}. "
        "Теперь уведомления будут приходить сюда.",
        user, previous_chat_id,
    )
