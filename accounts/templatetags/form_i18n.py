"""Let server-rendered form errors follow the client-side UI language.

The UI language lives only in the browser (static/js/i18n.js, `gc-lang` in
localStorage), so Django can't pick the language when it renders an error.
Instead a known error is tagged with a `data-i18n` key (and `data-i18n-args`
for its placeholders); i18n.js swaps the text in, exactly like any other
static string. The server-rendered message stays as the no-JS fallback.

Usage:
    {% load form_i18n %}
    <span class="field-error"{% error_i18n_attrs form.password.errors %}>{{ form.password.errors.0 }}</span>
"""

import json

from django import template
from django.contrib.auth.password_validation import (
    MinimumLengthValidator,
    get_default_password_validators,
)
from django.core.exceptions import ValidationError
from django.forms.utils import ErrorList
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

# ValidationError.code -> i18n.js key. The password_* codes are Django's own
# AUTH_PASSWORD_VALIDATORS codes; password_mismatch is raised by
# accounts.forms (RegistrationForm / ResetPasswordForm).
ERROR_I18N_KEYS = {
    "password_too_short": "err.password_too_short",
    "password_too_common": "err.password_too_common",
    "password_entirely_numeric": "err.password_entirely_numeric",
    "password_too_similar": "err.password_too_similar",
    "password_mismatch": "err.password_mismatch",
}

# Params passed through to i18n.js placeholders. Anything else (e.g. the
# similarity validator's English `verbose_name`) is left out — the translated
# strings don't interpolate it.
I18N_ARGS = ("min_length",)


def _configured_min_length():
    # Django 5.2's MinimumLengthValidator raises a pre-formatted message with
    # no `params`, so read min_length from the configured validator itself.
    for validator in get_default_password_validators():
        if isinstance(validator, MinimumLengthValidator):
            return validator.min_length
    return None


def _first_error(errors):
    if isinstance(errors, ValidationError):
        return errors
    if isinstance(errors, ErrorList):
        data = errors.as_data()
        return data[0] if data else None
    return None


@register.simple_tag
def error_i18n_attrs(errors):
    """` data-i18n="…"` (+ ` data-i18n-args="…"`) for the first error in
    `errors` (an ErrorList, e.g. `field.errors`, or one ValidationError), or
    "" when that error has no translation — it then renders as plain text."""
    error = _first_error(errors)
    key = ERROR_I18N_KEYS.get(getattr(error, "code", None))
    if not key:
        return ""
    attrs = format_html(' data-i18n="{}"', key)
    params = dict(error.params or {})
    if error.code == "password_too_short" and "min_length" not in params:
        min_length = _configured_min_length()
        if min_length is not None:
            params["min_length"] = min_length
    args = {name: params[name] for name in I18N_ARGS if name in params}
    if args:
        attrs += format_html(' data-i18n-args="{}"', json.dumps(args))
    return mark_safe(attrs)
