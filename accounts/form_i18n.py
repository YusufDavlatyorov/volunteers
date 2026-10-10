"""Tag Django form fields with static/js/i18n.js keys, so a form follows the UI
language like every other string on the site.

The UI language lives only in the browser (i18n.js, `gc-lang` in localStorage)
and switches without a reload, so Django can't translate a form when it renders
it. Instead each field carries i18n.js keys: the server still renders the
Russian text (the no-JS fallback, also what ServerSideI18nMiddleware writes for
crawlers), and i18n.js swaps the language in the browser.

    localize(form, {
        "region": {"label": "field.region", "choices": "region.", "empty": "choice.any_region"},
        "address": {"label": "form.address", "ph": "form.address_ph"},
    })

- label / help → field.i18n_label / i18n_help, rendered by partials/_field.html;
- ph → the widget's data-i18n-ph (placeholder);
- choices → each <option> gets data-i18n=<prefix><value> (or a {value: key}
  dict); "empty" is the key for the blank option ("" value). Checkbox / radio
  options get the key on a <span> around their label text, since i18n.js
  replaces an element's whole text and the <label> also holds the <input>.
"""

from django import forms
from django.utils.html import format_html


class _OptionKeysMixin:
    """Adds data-i18n to each rendered <option> from self.option_keys."""

    option_keys = None

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        key = (self.option_keys or {}).get(str(value))
        if key:
            option["attrs"] = {**option["attrs"], "data-i18n": key}
        return option


def _option_keys(field, choices, empty):
    if not choices:
        # Only the blank option — don't iterate field.choices (for a
        # ModelChoiceField that would be an extra query).
        return {"": empty} if empty else {}
    keys = {}
    for value, _label in field.choices:
        value = str(value)
        if value == "":
            if empty:
                keys[value] = empty
        elif isinstance(choices, dict):
            if value in choices:
                keys[value] = choices[value]
        elif choices:
            keys[value] = f"{choices}{value}"
    return keys


_I18N_WIDGET_CLASSES = {}


def _i18n_widget_class(cls):
    if cls not in _I18N_WIDGET_CLASSES:
        _I18N_WIDGET_CLASSES[cls] = type(f"I18n{cls.__name__}", (_OptionKeysMixin, cls), {})
    return _I18N_WIDGET_CLASSES[cls]


def _localize_choices(field, choices, empty):
    keys = _option_keys(field, choices, empty)
    widget = field.widget
    if isinstance(widget, (forms.CheckboxSelectMultiple, forms.RadioSelect)):
        # The option label sits next to the <input> inside one <label>: wrap the
        # text in its own element so i18n.js can replace just that.
        field.choices = [
            (value, format_html('<span data-i18n="{}">{}</span>', keys[str(value)], label) if str(value) in keys else label)
            for value, label in field.choices
        ]
        return
    if isinstance(widget, forms.Select) and not isinstance(widget, _OptionKeysMixin):
        widget.__class__ = _i18n_widget_class(type(widget))
    widget.option_keys = keys


def localize(form, spec):
    """Apply {field_name: {"label", "help", "ph", "choices", "empty"}} to the
    fields present on `form` (fields a form removed for some users are skipped)."""
    for name, keys in spec.items():
        field = form.fields.get(name)
        if field is None:
            continue
        if "label" in keys:
            field.i18n_label = keys["label"]
        if "help" in keys:
            field.i18n_help = keys["help"]
        if "ph" in keys:
            field.widget.attrs["data-i18n-ph"] = keys["ph"]
        if "choices" in keys or "empty" in keys:
            _localize_choices(field, keys.get("choices"), keys.get("empty"))
