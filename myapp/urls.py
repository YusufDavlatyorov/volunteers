from django.urls import path
from .views import (
    admin_panel_view,
    task_list_view,
    accept_task_view,
    broadcast_view,
    complete_task_view,
    create_request_view,
    task_detail_view,
    completed_tasks_view,
    rating_view,
    about_view,
    ai_assistant_view,
    ai_chat_view,
    check_overdue_view,
    create_event_view,
    dashboard_view,
    photo_reports_view,
)

urlpatterns = [
    path('dashboard/', dashboard_view, name='dashboard'),
    path('panel/', admin_panel_view, name='admin_panel'),
    # Tasks
    path('tasks/', task_list_view, name='task_list'),
    path('tasks/<int:pk>/accept/', accept_task_view, name='accept_task'),
    path('tasks/<int:pk>/complete/', complete_task_view, name='complete_task'),
    path('tasks/<int:pk>/', task_detail_view, name='task_detail'),

    # Client
    path('request/create/', create_request_view, name='create_request'),

    # Archive
    path('archive/', completed_tasks_view, name='completed_tasks'),
    path('archive/check-overdue/', check_overdue_view, name='check_overdue'),

    # Rating & About
    path('rating/', rating_view, name='rating'),
    path('about/', about_view, name='about'),

    # AI
    path('ai/', ai_assistant_view, name='ai_assistant'),
    path('ai/chat/', ai_chat_view, name='ai_chat'),

    # Events (curator)
    path('events/create/', create_event_view, name='create_event'),
    path('broadcast/', broadcast_view, name='broadcast'),
    path('reports/', photo_reports_view, name='photo_reports'),
]
