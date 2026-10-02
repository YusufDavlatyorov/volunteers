"""Quiet safety flags for chat messages (Feature Pack v2, milestone 3 follow-up).

A party message (client / assigned volunteer — never staff, never system) is
scanned for the patterns behind the common scams on elderly people:

* ``sms_code``     — someone asks for / mentions a code from an SMS, a PIN, CVV;
* ``money``        — money, transfers, bank cards, wallets, a card number;
* ``off_platform`` — moving the conversation to another messenger / social
  network (WhatsApp, Telegram, Viber, imo, @handles, t.me / wa.me links…);
* ``phone``        — a phone number in the text (9–12 digits once separators
  are dropped; 13–19 digits read as a card number → ``money``).

Nothing is blocked or changed: the message is delivered as usual, the codes are
stored on ``ChatMessage.flags`` (visible to staff only) and curators get one
quiet, in-app-only ``chat_flagged`` notification per task × category × day
(``services/chat.py::_flag``). Ordinary conversation never reaches staff.

Pure functions, no ORM — unit-tested in ``myapp/tests_chat_flags.py``. The
patterns lean towards recall (a curator glances at a false positive; a missed
"скажите код из смс" costs an elderly client their savings) but avoid the
obvious everyday false positives ("на карте" = on the map, dates, house
numbers, "переводчик" is still money-ish and acceptable).
"""

from __future__ import annotations

import re

SMS_CODE = "sms_code"
MONEY = "money"
OFF_PLATFORM = "off_platform"
PHONE = "phone"
# Priority order: the first matching category names the staff notification.
CATEGORIES = (SMS_CODE, MONEY, OFF_PLATFORM, PHONE)

_FLAGS = re.IGNORECASE | re.UNICODE


def _rx(*parts):
    return re.compile("|".join(parts), _FLAGS)


# --- SMS / PIN codes -------------------------------------------------------- #
_SMS_WORD = r"(?:смс|sms|эсэмэс|сообщени\w*|паём\w*)"
_SMS_CODE_RE = _rx(
    r"\bкод\w*\b[^.!?\n]{0,30}\b" + _SMS_WORD,       # "код из смс", "код, который пришёл в сообщении"
    _SMS_WORD + r"[\s-]*код\w*",                    # "смс-код", "sms code"
    r"\bcode\b[^.!?\n]{0,20}\b(?:sms|text|message)\b",
    r"\b(?:sms|text|verification|confirmation)\s+code\b",
    r"\bкод\w*\s+подтвержд\w*",                      # "код подтверждения"
    r"\bпин[\s-]?код\w*", r"\bpin\b", r"\bcvv\b", r"\bcvc\b",
    r"\bрамз\w*",                                     # tj: "рамз" = code / PIN
    r"\bкод\w*\s+(?:аз|бо)\s+(?:смс|sms)",          # tj: "код аз смс"
)

# --- money / transfers / cards --------------------------------------------- #
_MONEY_RE = _rx(
    r"\bденьг\w*", r"\bденег\b", r"\bденеж\w*",
    r"\bперевод\w*", r"\bперевед\w*", r"\bперевест\w*", r"\bперечисл\w*", r"\bперекин\w*",
    r"\bоплат\w*", r"\bзаплат\w*", r"\bпредоплат\w*",
    r"\bв\s+долг\b", r"\bодолж\w*", r"\bзайм\w*", r"\bза[её]м\w*", r"\bзанять\b", r"\bкредит\w*",
    r"\bналичн\w*", r"\bкошел[её]к\w*", r"\bкошельк\w*",
    r"\bсомони\b", r"\bсомон\w*", r"\bдоллар\w*", r"\bрубл\w*", r"\bевро\b",
    # bank cards — "на карту / картой / номер карты", not "на карте" (on the map)
    r"\bномер\w*\s+карт\w*", r"\bна\s+карт(?:у|очку)\b", r"\bкартой\b", r"\bкарточк\w*",
    r"\bбанковск\w*", r"\bvisa\b", r"\bmaster\s?card\b", r"\bкорти\s+милл\w*",
    r"\bkorti\s+milli\b", r"\bкорт(?:и|ро)\b",
    r"\bqiwi\b", r"\bкиви\b", r"\balif\b", r"\bалиф\b",
    # tj: "пул" (money), "интиқол" (transfer), "қарз" (debt), "пардохт" (payment)
    r"\bпул(?:и|ро|ҳо|ат|еро)?\b", r"\bинтиқол\w*", r"\bқарз\w*", r"\bпардохт\w*",
    r"\bmoney\b", r"\btransfer\w*", r"\bpay(?:ment|ing)?\b", r"\bcash\b", r"\bloan\b",
    r"\bcard\s+number\b", r"\bbank\s+card\b", r"\bwallet\b",
    r"\$\s?\d", r"\d\s?\$", r"\busd\b", r"\btjs\b",
)

# --- leaving the platform --------------------------------------------------- #
_OFF_PLATFORM_RE = _rx(
    r"whats\s?app", r"\bв[ао]т[сц]?[ас]п\w*", r"\bвацап\w*", r"\bватсап\w*", r"\bвотсап\w*",
    r"telegram", r"\bтелеграм\w*", r"\bтелег(?:а|у|е|ой)\b", r"\bтг\b", r"\btg\b",
    r"viber", r"\bвайбер\w*", r"\bвибер\w*", r"\bimo\b", r"\bимо\b",
    r"instagram", r"\bинстаграм\w*", r"\bинст(?:а|у|е)\b", r"facebook", r"\bфейсбук\w*",
    r"\bвконтакт\w*", r"\bвк\b", r"\bодноклассник\w*", r"\bsignal\b", r"\bskype\b", r"\bскайп\w*",
    r"\bмессенджер\w*", r"\bв\s+личк\w*", r"\bв\s+лс\b", r"\bнапиши(?:те)?\s+мне\s+в\b",
    r"t\.me/", r"wa\.me/", r"vk\.com", r"(?<![\w.])@[A-Za-z][A-Za-z0-9_]{3,}",
)

# Digit runs: digits joined by spaces / dashes / dots / parentheses / a leading +.
_DIGIT_RUN_RE = re.compile(r"\+?\d[\d\s().-]*\d")
_PHONE_DIGITS = range(9, 13)   # +992 XX XXX XX XX = 12, local = 9
_CARD_DIGITS = range(13, 20)   # PAN length


def _digit_categories(text):
    found = set()
    for match in _DIGIT_RUN_RE.finditer(text):
        run = match.group()
        digits = sum(ch.isdigit() for ch in run)
        if digits in _CARD_DIGITS:
            found.add(MONEY)
        elif digits in _PHONE_DIGITS:
            # A dd.mm.yyyy hh:mm date is 12 digits too — dots/colon dates are not phones.
            if re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{2,4}(?:\s+\d{1,2})?", run.strip()):
                continue
            found.add(PHONE)
    return found


def detect(text) -> list[str]:
    """Flag categories present in ``text`` (in ``CATEGORIES`` order); ``[]``
    for ordinary conversation or a non-string."""
    if not isinstance(text, str) or not text.strip():
        return []
    normalized = text.replace("ё", "е").replace("Ё", "Е")
    found = _digit_categories(normalized)
    if _SMS_CODE_RE.search(normalized):
        found.add(SMS_CODE)
    if _MONEY_RE.search(normalized):
        found.add(MONEY)
    if _OFF_PLATFORM_RE.search(normalized):
        found.add(OFF_PLATFORM)
    return [code for code in CATEGORIES if code in found]
