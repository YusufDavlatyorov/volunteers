"""CRM analytics — HTTP views (Feature Pack v2, feature 10). Admin + curator.

All numbers come from ``myapp/services/analytics_v2.py`` (aggregate queries,
cached 5 min); these views are thin transports.

    GET /myapp/crm/analytics/              the page (?period=7d|30d|3m|1y|custom&from=&to=&region=)
    GET /myapp/crm/analytics/<chart>.csv   one chart's data as CSV (same filters, &lang=)
    GET /myapp/crm/analytics/heat.json     heatmap points (rounded, capped, no PII)
"""

from django.conf import settings
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.models import REGION_CHOICES
from server.i18n_prerender import translation_table
from .services import analytics_v2
from .services.notify_texts import LANGS
from .views import role_required

WIDE_CHARTS = {"requests_timeline", "delays", "registrations", "top_volunteers",
               "chat_flags_topics", "chat_flags_help_types"}

# How each card draws its data (static/js/crm-analytics.js reads data-display).
# Lines and date bars by default; ordinal categories (stages, buckets, stars)
# keep their order; nominal ones are ranked by value as horizontal bars.
# "share" = parts of one whole: a doughnut up to SHARE_MAX_SLICES slices,
# otherwise ranked bars. Flag topics are not a share (one message can carry
# several), so they rank.
DISPLAY = {
    "funnel": "bars", "workload": "bars", "ratings": "bars", "emergencies": "bars",
    "priorities": "share", "availability": "share",
    "help_types": "rank", "regions": "rank", "donations_status": "rank",
    "donations_category": "rank", "chat_flags_topics": "rank",
    "chat_flags_help_types": "stack",
}
SHARE_MAX_SLICES = 5
ROW_PX = 30          # one ranked bar (18 px bar + gap)
GROUP_ROW_PX = 58    # one category of a grouped two-series ranking (labels never touch)
LEGEND_PX = 34


def _display(name, data):
    display = DISPLAY.get(name)
    if display == "share":
        return "doughnut" if len(data["labels"]) <= SHARE_MAX_SLICES else "rank"
    if display:
        return display
    return "line" if data["kind"] == "line" else "bars"


def _box_height(display, data):
    """Pixel height of a horizontal chart, from its row count — rendered by the
    server so the page doesn't jump when the chart is drawn."""
    rows = len(data["labels"])
    if display == "rank":
        if len(data["series"]) > 1:
            return rows * GROUP_ROW_PX + LEGEND_PX
        return rows * ROW_PX + 8
    if display == "stack":
        return rows * ROW_PX + LEGEND_PX
    return None


def _is_empty(data):
    return not data["series"] or all(not v for s in data["series"] for v in s["values"])


def _delta_texts(table, head):
    """Russian server render of the change chip (charts-theme.js re-labels it)."""
    change, direction = head["change"], head["direction"]
    if change is None:
        return None, None
    arrow = {"up": "▲", "down": "▼"}.get(direction, "=")
    percent = f"{abs(change)}\u00a0%"
    key = {"up": "an.delta_up", "down": "an.delta_down"}.get(direction, "an.delta_same")
    return f"{arrow} {percent}", table.get(key, "").replace("{p}", percent)


def _date_text(table, day):
    """"5 октября 2026" — the activity calendar's date format (act.date / act.mgN);
    static/js/analytics-filters.js re-labels it in the visitor's language."""
    month = table.get(f"act.mg{day.month}", str(day.month))
    return table.get("act.date", "{d} {m} {y}").replace("{d}", str(day.day)).replace("{m}", month).replace("{y}", str(day.year))


@role_required("admin", "curator")
def crm_analytics_view(request):
    filters = analytics_v2.parse_filters(request.GET)
    charts = analytics_v2.all_charts(filters)
    funnel_kpis = charts["funnel"].get("kpis", {})
    ratings = charts["ratings"]["series"][0]["values"]
    rated = sum(ratings)
    avg_rating = round(sum((i + 1) * n for i, n in enumerate(ratings)) / rated, 2) if rated else None
    table = translation_table(getattr(settings, "SERVER_RENDER_LANGUAGE", "ru"))
    heads = analytics_v2.headlines(filters)
    layout = []
    for name in analytics_v2.CHARTS:
        if name == "top_volunteers":
            continue
        data = charts[name]
        display = _display(name, data)
        head = dict(heads[name])
        head["delta_text"], head["delta_sr"] = _delta_texts(table, head)
        horizontal = display in ("rank", "stack")
        layout.append({
            "name": name, "wide": name in WIDE_CHARTS, "display": display,
            "height": _box_height(display, data), "empty": _is_empty(data), "head": head,
            # Half-width cards keep one 18rem box; a long ranking scrolls inside it
            # (analytics.css sizes the plot from --rows).
            "rows": len(data["labels"]) if horizontal else None,
            "grouped": display == "rank" and len(data["series"]) > 1,
            "legend": horizontal and len(data["series"]) > 1,
        })
    return render(request, "myapp/crm_analytics.html", {
        "filters": filters,
        "periods": list(analytics_v2.PERIODS) + [analytics_v2.CUSTOM],
        "today": analytics_v2.local_today(),
        "range_from": _date_text(table, filters.start_date),
        "range_to": _date_text(table, filters.end_date),
        "regions": REGION_CHOICES,
        "charts": charts,
        "chart_layout": layout,
        "kpis": funnel_kpis,
        "avg_rating": avg_rating,
        "sos_minutes": charts["emergencies"].get("kpis", {}).get("minutes_to_ack"),
        "flag_kpis": charts["chat_flags_topics"].get("kpis", {}),
        "top_volunteers": charts["top_volunteers"]["rows"],
        "payload": {"charts": charts, "weekly": filters.weekly, "qs": filters.querystring()},
        "generated_at": timezone.now(),
        "max_range_days": analytics_v2.MAX_RANGE_DAYS,
    })


@role_required("admin", "curator")
def crm_analytics_csv_view(request, name):
    if name not in analytics_v2.CHARTS:
        raise Http404("unknown chart")
    filters = analytics_v2.parse_filters(request.GET)
    lang = request.GET.get("lang") or getattr(getattr(request.user, "profile", None), "language", "ru")
    lang = lang if lang in LANGS else "ru"
    body = analytics_v2.to_csv(name, analytics_v2.chart(name, filters), lang)
    response = HttpResponse(body, content_type="text/csv; charset=utf-8")
    suffix = f"{filters.start_date}_{filters.end_date}" + (f"_{filters.region}" if filters.region else "")
    response["Content-Disposition"] = f'attachment; filename="khayrkhoh-{name}-{suffix}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response


@role_required("admin", "curator")
def crm_analytics_heat_view(request):
    filters = analytics_v2.parse_filters(request.GET)
    points = analytics_v2.heat_points(filters)
    response = JsonResponse({"points": points, "cap": analytics_v2.HEAT_POINT_CAP})
    response["Cache-Control"] = "private, no-store"
    return response
