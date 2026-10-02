"""CRM analytics — HTTP views (Feature Pack v2, feature 10). Admin + curator.

All numbers come from ``myapp/services/analytics_v2.py`` (aggregate queries,
cached 5 min); these views are thin transports.

    GET /myapp/crm/analytics/              the page (?days=7|30|90 | ?from=&to=, &region=)
    GET /myapp/crm/analytics/<chart>.csv   one chart's data as CSV (same filters, &lang=)
    GET /myapp/crm/analytics/heat.json     heatmap points (rounded, capped, no PII)
"""

from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.utils import timezone

from accounts.models import REGION_CHOICES
from .services import analytics_v2
from .services.notify_texts import LANGS
from .views import role_required

WIDE_CHARTS = {"requests_timeline", "delays", "registrations", "top_volunteers"}


@role_required("admin", "curator")
def crm_analytics_view(request):
    filters = analytics_v2.parse_filters(request.GET)
    charts = analytics_v2.all_charts(filters)
    funnel_kpis = charts["funnel"].get("kpis", {})
    ratings = charts["ratings"]["series"][0]["values"]
    rated = sum(ratings)
    avg_rating = round(sum((i + 1) * n for i, n in enumerate(ratings)) / rated, 2) if rated else None
    layout = [{"name": name, "wide": name in WIDE_CHARTS} for name in analytics_v2.CHARTS if name != "top_volunteers"]
    return render(request, "myapp/crm_analytics.html", {
        "filters": filters,
        "presets": analytics_v2.PRESETS,
        "regions": REGION_CHOICES,
        "charts": charts,
        "chart_layout": layout,
        "kpis": funnel_kpis,
        "avg_rating": avg_rating,
        "sos_minutes": charts["emergencies"].get("kpis", {}).get("minutes_to_ack"),
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
