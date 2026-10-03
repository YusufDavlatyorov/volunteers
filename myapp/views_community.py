"""Volunteer community (Telegram) — website side (Feature Pack v2, feature 5).

    POST /myapp/community/invite/                 fresh single-use invite links, shown once
    POST /myapp/crm/volunteers/<pk>/mentor/       staff: award the hand-given "Наставник" badge

Rules: myapp/services/community.py and services/achievements.award_manual.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from accounts.models import Users

from .services import achievements, community
from .views import role_required


@login_required
@require_POST
def community_invite_view(request):
    outcome = community.create_invites(request.user)
    status = 200 if outcome.ok else 400
    return render(request, "myapp/community_invite.html", {
        "outcome": outcome,
        "hours": getattr(settings, "COMMUNITY_INVITE_HOURS", 24),
        "general": community.GENERAL,
    }, status=status)


_MENTOR_MESSAGES = {
    "ok": ("success", "Значок «Наставник» выдан."),
    "already": ("info", "У волонтёра уже есть значок «Наставник»."),
    "invalid": ("error", "Значок можно выдать только активному волонтёру."),
    "forbidden": ("error", "У вас нет доступа к этой странице"),
}


@role_required("admin", "curator")
@require_POST
def crm_award_mentor_view(request, pk):
    volunteer = get_object_or_404(Users, pk=pk, is_volunteer=True)
    _, code = achievements.award_manual(request.user, volunteer, "mentor")
    level, text = _MENTOR_MESSAGES[code]
    getattr(messages, level)(request, text)
    return redirect("crm_volunteer_detail", pk=volunteer.pk)
