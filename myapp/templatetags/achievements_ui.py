"""Template helpers for volunteer achievements (Feature Pack v2, feature 6).

Load with ``{% load achievements_ui %}``.

* ``{% achievement_grid items %}`` — the profile grid + the one ``<dialog>``
  (items = ``services.achievements.progress_for(user)``). Titles, progress and
  the earned date are in the markup, so the grid reads fine without JS; the
  modal (static/js/achievements.js) adds the description and the rules.
* ``{% badge_row badges %}`` — up to 3 small earned badges for a public card
  (badges = a list of ``Achievement`` from ``achievements.top_badges``). Only
  the badge itself — never progress, counts or dates.
* ``{{ mapping|badges_for:user_id }}`` — the list for one user from the
  ``top_badges`` dict (``[]`` when missing).
"""

from django import template

register = template.Library()


def _title_ru(code):
    from myapp.services import notify_texts

    return notify_texts.LABELS["achievement"]["ru"].get(code, code)


@register.inclusion_tag("myapp/achievements/_grid.html")
def achievement_grid(items, dom_id="achievements", own=True):
    for item in items:
        item["title_ru"] = _title_ru(item["code"])
    return {"items": items, "dom_id": dom_id, "own": own, "earned_count": sum(1 for i in items if i["earned"])}


@register.inclusion_tag("myapp/achievements/_badge_row.html")
def badge_row(badges):
    return {"badges": [{"code": a.code, "tier": a.tier, "title_ru": _title_ru(a.code),
                        "icon_path": a.icon_path} for a in (badges or [])]}


@register.filter
def badges_for(mapping, user_id):
    try:
        return mapping.get(user_id, []) if mapping else []
    except AttributeError:
        return []
