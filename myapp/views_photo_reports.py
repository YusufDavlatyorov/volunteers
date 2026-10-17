"""Photo-report review in the CRM — HTTP views. Rules live in
``myapp/services/photo_reports.py``; these are thin transports.

    GET  /myapp/crm/photo-reports/              list (?status=pending default, from, to, volunteer, task)
    GET  /myapp/crm/photo-reports/<pk>/         the report: large photo, task, volunteer, client
    POST /myapp/crm/photo-reports/<pk>/review/  action=approve | reject (+ reason, required)
    POST /myapp/crm/photo-reports/<pk>/delete/  any status; an approved report's reward is revoked
    POST /myapp/crm/photo-reports/delete/       ids=<pk>&ids=<pk>… — the list's "Delete selected"

Curator + admin only.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .forms import PhotoReportFilterForm
from .models import REJECTION_REASON_MAX_LENGTH, PhotoReport
from .services import photo_reports
from .views import role_required

CRM_PHOTO_REPORTS_PAGE_SIZE = 20


@login_required
@role_required("admin", "curator")
def crm_photo_reports_view(request):
    data = request.GET.copy()
    data.setdefault("status", photo_reports.DEFAULT_STATUS_FILTER)
    form = PhotoReportFilterForm(data, authors=photo_reports.report_authors())
    filters = form.cleaned_data if form.is_valid() else {"status": photo_reports.DEFAULT_STATUS_FILTER}
    qs = photo_reports.staff_queryset(filters)
    page_obj = Paginator(qs, CRM_PHOTO_REPORTS_PAGE_SIZE).get_page(request.GET.get("page"))
    query = request.GET.copy()
    query.pop("page", None)
    return render(request, "myapp/crm_photo_reports.html", {
        "filter_form": form,
        "page_obj": page_obj,
        "querystring": query.urlencode(),
        "status_filter": filters.get("status") or photo_reports.DEFAULT_STATUS_FILTER,
        "pending_count": photo_reports.pending_count(),
    })


@login_required
@role_required("admin", "curator")
def crm_photo_report_detail_view(request, pk):
    report = get_object_or_404(
        PhotoReport.objects.select_related(
            "author", "author__profile", "help_request", "help_request__client", "reviewed_by", "event",
        ),
        pk=pk,
    )
    task = report.help_request
    return render(request, "myapp/crm_photo_report_detail.html", {
        "report": report,
        "task": task,
        "author_is_task_volunteer": bool(task and task.volunteer_id and task.volunteer_id == report.author_id),
        "reason_max_length": REJECTION_REASON_MAX_LENGTH,
    })


@login_required
@role_required("admin", "curator")
@require_POST
def crm_photo_report_review_view(request, pk):
    report = get_object_or_404(PhotoReport.objects.select_related("author", "help_request"), pk=pk)
    action = request.POST.get("action")
    if action == "approve":
        outcome = photo_reports.approve(report, actor=request.user)
        if outcome.ok:
            messages.success(request, "Фотоотчёт одобрен.")
    elif action == "reject":
        outcome = photo_reports.reject(report, actor=request.user, reason=request.POST.get("reason", ""))
        if outcome.code == photo_reports.REASON_REQUIRED:
            messages.error(request, "Укажите причину отклонения.")
            return redirect("crm_photo_report_detail", pk=report.pk)
        if outcome.ok:
            messages.success(request, "Фотоотчёт отклонён, волонтёр получил уведомление.")
    else:
        messages.error(request, "Неизвестное действие.")
        return redirect("crm_photo_report_detail", pk=report.pk)
    if outcome.code == photo_reports.ALREADY_REVIEWED:
        messages.info(request, "Этот фотоотчёт уже проверен.")
        return redirect("crm_photo_report_detail", pk=report.pk)
    return redirect("crm_photo_reports")


@login_required
@role_required("admin", "curator")
@require_POST
def crm_photo_report_delete_view(request, pk):
    report = get_object_or_404(PhotoReport, pk=pk)
    outcome = photo_reports.delete(report, actor=request.user)
    if outcome.code == photo_reports.NOT_FOUND:
        messages.info(request, "Этот фотоотчёт уже удалён.")
    elif outcome.reward_revoked:
        messages.success(request, "Фотоотчёт удалён. Баллы за запрос отменены — он снова ждёт фотоотчёт.")
    else:
        messages.success(request, "Фотоотчёт удалён.")
    return _back_to_list(request)


def _back_to_list(request):
    # Back to the list with its filters (from the list's own form) — only a
    # path of the list itself, never an arbitrary URL.
    back = request.POST.get("next", "")
    if back.startswith(reverse("crm_photo_reports")) and url_has_allowed_host_and_scheme(back, allowed_hosts=None):
        return redirect(back)
    return redirect("crm_photo_reports")


@login_required
@role_required("admin", "curator")
@require_POST
def crm_photo_reports_bulk_delete_view(request):
    ids = [pk for pk in request.POST.getlist("ids") if pk.isdigit()]
    if not ids:
        messages.error(request, "Выберите хотя бы один фотоотчёт.")
        return _back_to_list(request)
    outcome = photo_reports.delete_many(ids, actor=request.user)
    messages.success(
        request, f"Удалено фотоотчётов: {outcome.deleted}. Баллы отменены по запросам: {outcome.revoked}."
    )
    return _back_to_list(request)
