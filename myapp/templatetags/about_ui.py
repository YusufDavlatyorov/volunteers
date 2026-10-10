"""Small formatting helpers for the About page. Load with ``{% load about_ui %}``.

The page is rendered in Russian on the server (server/i18n_prerender.py) and
re-labelled in the visitor's language by static/js/about.js, so these produce
the Russian shape; the JS redoes plural keys and number formats per language.

* ``n|plural_ru`` — the CLDR Russian plural category of n ("one" / "few" /
  "many"), for keys like ``ab.vol.{{ n|plural_ru }}``.
* ``n|group_digits`` — 1248 → "1 248" (no-break space, the Russian grouping).
* ``x|decimal_ru`` — 4.9 → "4,9".
"""

from django import template

from myapp.services.activity import plural_category_ru

register = template.Library()


@register.filter
def plural_ru(value):
    try:
        return plural_category_ru(int(value))
    except (TypeError, ValueError):
        return "many"


@register.filter
def group_digits(value):
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return value


@register.filter
def decimal_ru(value):
    try:
        return f"{float(value):.1f}".replace(".", ",")
    except (TypeError, ValueError):
        return value
