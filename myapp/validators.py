"""Shared field validators.

Tajik phone numbers: the one rule for every phone field (help requests, pet
reports — forms, model ``clean()`` and therefore the Django admin too).
Accepted input, after dropping spaces, dashes, dots and parentheses:

* ``+992`` followed by 9 digits   — ``+992 90 123 45 67``, ``(+992) 90-123-45-67``
* ``992`` followed by 9 digits    — the same without the plus
* exactly 9 digits                — the national number, ``90 123 45 67``

Everything is stored normalized as ``+992XXXXXXXXX``. The ``invalid_phone``
error code is translated client-side (``err.invalid_phone`` in i18n.js via
accounts/templatetags/form_i18n.py).
"""

import re

from django.core.exceptions import ValidationError

TJ_COUNTRY_CODE = "992"
TJ_NATIONAL_DIGITS = 9
PHONE_PLACEHOLDER = "+992 XX XXX XX XX"
PHONE_ERROR = "Введите номер в формате +992 XX XXX XX XX (9 цифр после +992)."

_SEPARATORS = re.compile(r"[\s\-().]")
_FORMS = (
    re.compile(r"^\+992(\d{9})$"),
    re.compile(r"^992(\d{9})$"),
    re.compile(r"^(\d{9})$"),
)


def normalize_tj_phone(value):
    """``+992XXXXXXXXX`` for any accepted form of a Tajik number; raises
    ValidationError(code="invalid_phone") otherwise. Empty stays empty (whether
    the field is required is the field's business, not this function's)."""
    raw = (value or "").strip()
    if not raw:
        return ""
    compact = _SEPARATORS.sub("", raw)
    for pattern in _FORMS:
        match = pattern.match(compact)
        if match:
            return f"+{TJ_COUNTRY_CODE}{match.group(1)}"
    raise ValidationError(PHONE_ERROR, code="invalid_phone")


def validate_tj_phone(value):
    """Model-field validator: the value must normalize (it runs on the raw
    input, before Model.clean() stores the normalized form)."""
    normalize_tj_phone(value)
