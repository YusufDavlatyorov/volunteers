"""Numbers in the server-rendered (Russian) page: ``{{ value|ru_num:1 }}`` →
"1 234,5" — the same output as ``Intl.NumberFormat('ru')``, so nothing visibly
changes when static/js/charts-theme.js re-formats them in the visitor's
language. ``None`` renders as "—"."""

from django import template

register = template.Library()

NBSP = " "


@register.filter
def ru_num(value, decimals=0):
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
        decimals = int(decimals)
    except (TypeError, ValueError):
        return value
    text = f"{number:,.{decimals}f}" if decimals else f"{round(number):,}"
    return text.replace(",", NBSP).replace(".", ",")
