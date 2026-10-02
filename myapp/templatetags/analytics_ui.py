"""``{% sparkline trend %}`` — the server-rendered SVG "requests, last 14 days"
card on the curator/admin dashboard (no chart library on the dashboard).
``trend`` = ``services.analytics_v2.dashboard_trend()``."""

from django import template

register = template.Library()

WIDTH, HEIGHT, PAD = 280, 64, 4


def _points(values, top):
    if not values:
        return ""
    step = (WIDTH - 2 * PAD) / max(len(values) - 1, 1)
    scale = (HEIGHT - 2 * PAD) / top
    return " ".join(
        f"{PAD + i * step:.1f},{HEIGHT - PAD - (v or 0) * scale:.1f}" for i, v in enumerate(values)
    )


@register.inclusion_tag("myapp/dashboard/_sparkline.html")
def sparkline(trend):
    created, completed = trend.get("created", []), trend.get("completed", [])
    top = max([1, *created, *completed])
    return {
        "trend": trend,
        "width": WIDTH,
        "height": HEIGHT,
        "created_points": _points(created, top),
        "completed_points": _points(completed, top),
    }
