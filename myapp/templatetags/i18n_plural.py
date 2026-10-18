"""Counted words in the right grammatical form: "1 заявка / 3 заявки / 5 заявок".

``{% plural_i18n "rating.n_tasks" n %}`` renders one ``<span>`` whose
``data-i18n`` is the Russian form for ``n`` (``<base>.one|few|many``, CLDR —
the server pre-renders Russian) plus ``data-i18n-plural="<base>"``: on a
language switch i18n.js picks the form for the new language with
``Intl.PluralRules`` (``<base>.<category>``, falling back to ``.other``).
Every ``<base>.one/few/many/other`` key exists in all three languages
(Tajik/English simply repeat a form). Text placeholder: ``{n}``.
"""

import json

from django import template
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def plural_i18n(base, n, css_class=""):
    from myapp.services.activity import plural_category_ru
    from server.i18n_prerender import translation_table

    n = int(n or 0)
    key = f"{base}.{plural_category_ru(n)}"
    text = translation_table("ru").get(key, "{n}").replace("{n}", str(n))
    return format_html(
        '<span{} data-i18n="{}" data-i18n-plural="{}" data-i18n-args="{}">{}</span>',
        format_html(' class="{}"', css_class) if css_class else "",
        key, base, json.dumps({"n": n}), text,
    )


# (seconds in the unit, i18n base) — largest unit first; under a minute is "just now".
_AGO_UNITS = (
    (365 * 86400, "ago.years"),
    (30 * 86400, "ago.months"),
    (86400, "ago.days"),
    (3600, "ago.hours"),
    (60, "ago.minutes"),
)


@register.simple_tag
def time_ago(value):
    """"3 часа назад" in the UI language — replaces ``{{ dt|timesince }} ago``,
    which Django renders in English (LANGUAGE_CODE) next to a translated
    suffix ("3 hours, 20 minutes назад"). One unit, the largest that fits, as
    a ``plural_i18n`` span inside ``<time>`` (exact moment in ``datetime``)."""
    from django.utils import timezone

    if not value:
        return "—"
    seconds = max(0, int((timezone.now() - value).total_seconds()))
    for size, base in _AGO_UNITS:
        if seconds >= size:
            inner = plural_i18n(base, seconds // size)
            break
    else:
        inner = format_html('<span data-i18n="ago.just_now">только что</span>')
    return format_html('<time datetime="{}">{}</time>', value.isoformat(), inner)


@register.simple_tag
def local_date(value):
    """A calendar day in the UI language: "18 октября 2026" / "18 октябри 2026" /
    "October 18, 2026". Rendered with ``|date:"j E Y"`` under the server-render
    language (Russian — Django's own active language is always en-us here, the
    UI language lives in the browser); ``data-i18n-date`` lets i18n.js re-label
    it with the month names of the chosen language (``act.mg*``)."""
    from django.conf import settings
    from django.template.defaultfilters import date as date_filter
    from django.utils import translation

    if not value:
        return "—"
    with translation.override(settings.SERVER_RENDER_LANGUAGE):
        text = date_filter(value, "j E Y")
    iso = value.isoformat()
    return format_html('<time datetime="{}" data-i18n-date="{}">{}</time>', iso, iso, text)
