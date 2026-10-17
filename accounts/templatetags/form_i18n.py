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
    # myapp/validators.py (phone fields)
    "invalid_phone": "err.invalid_phone",
    # Django's MaxLengthValidator (username / full_name limits, any too-long field)
    "max_length": "err.max_length",
    # accounts/forms.py ProfileForm.clean_away_until (do-not-disturb end date)
    "away_until_past": "err.away_until_past",
    # accounts/forms.py (registration / profile)
    "username_too_short": "err.username_too_short",
    "username_taken": "err.username_taken",
    "email_taken": "err.email_taken",
    # map location pickers (HelpRequestForm / PetReportForm / ProfileForm)
    "coords_pair": "err.coords_pair",
    "coords_invalid": "err.coords_invalid",
    # myapp/forms.py DonationForm
    "donation_item_required": "err.donation_item_required",
    "organization_required": "err.organization_required",
    # myapp/forms.py DonationGoodsForm (the "Donate goods" page)
    "photos_too_many": "err.photos_too_many",
    "photo_too_large": "err.photo_too_large",
    "photo_format": "err.photo_format",
    "address_required": "err.address_required",
    # accounts/views.py login_view
    "login_failed": "err.login_failed",
    "login_throttled": "err.login_throttled",
    # Django's own field errors (rendered in English — LANGUAGE_CODE is en-us)
    "required": "err.required",
    "invalid": "err.invalid",
    "invalid_choice": "err.invalid_choice",
    "min_value": "err.min_value",
    "max_value": "err.max_value",
    "invalid_image": "err.invalid_image",
}

# Params passed through to i18n.js placeholders. Anything else (e.g. the
# similarity validator's English `verbose_name`) is left out — the translated
# strings don't interpolate it.
I18N_ARGS = ("min_length", "limit_value", "show_value")


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


@register.filter
def i18n_args(value, name):
    """JSON for data-i18n-args with one named value, e.g. an alt text that
    embeds a title: ``data_i18n_args=report.title|i18n_args:"title"``."""
    return json.dumps({name: str(value)}, ensure_ascii=False)


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


@register.filter
def described_widget(field):
    """Render a bound field's widget tied to its hint / error for assistive
    tech: aria-describedby → the ``<id>_hint`` / ``<id>_error`` elements that
    partials/_field.html renders, and aria-invalid when the field has errors."""
    ids, attrs = [], {}
    if field.help_text:
        ids.append(f"{field.auto_id}_hint")
    if field.errors:
        ids.append(f"{field.auto_id}_error")
        attrs["aria-invalid"] = "true"
    if ids:
        attrs["aria-describedby"] = " ".join(ids)
    return field.as_widget(attrs=attrs) if field.auto_id and attrs else str(field)
