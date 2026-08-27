from django.urls import path
from .views import (
    admin_panel_view,
    task_list_view,
    accept_task_view,
    broadcast_list_view,
    broadcast_view,
    complete_task_view,
    create_request_view,
    crm_tasks_view,
    crm_volunteer_detail_view,
    task_detail_view,
    completed_tasks_view,
    event_list_view,
    map_data_view,
    map_view,
    people_list_view,
    rating_view,
    about_view,
    ai_assistant_view,
    ai_chat_view,
    check_overdue_view,
    create_event_view,
    dashboard_view,
    photo_reports_view,
    task_notify_volunteer_view,
    task_recommendations_view,
    task_route_view,
    update_location_view,
    volunteer_application_view,
    volunteer_applications_view,
    volunteer_application_detail_view,
    volunteer_application_approve_view,
    volunteer_application_reject_view,
)

urlpatterns = [
    path('dashboard/', dashboard_view, name='dashboard'),
    path('panel/', admin_panel_view, name='admin_panel'),
    # Tasks
    path('tasks/', task_list_view, name='task_list'),
    path('tasks/<int:pk>/accept/', accept_task_view, name='accept_task'),
    path('tasks/<int:pk>/complete/', complete_task_view, name='complete_task'),
    path('tasks/<int:pk>/route/', task_route_view, name='task_route'),
    path('tasks/<int:pk>/recommendations/', task_recommendations_view, name='task_recommendations'),
    path('tasks/<int:pk>/recommendations/notify/<int:volunteer_id>/', task_notify_volunteer_view, name='task_notify_volunteer'),
    path('tasks/<int:pk>/', task_detail_view, name='task_detail'),

    # Client
    path('request/create/', create_request_view, name='create_request'),

    # Volunteer application (applicant's own status)
    path('volunteer/application/', volunteer_application_view, name='volunteer_application'),

    # Volunteer applications (admin review section)
    path('volunteer-applications/', volunteer_applications_view, name='volunteer_applications'),
    path('volunteer-applications/<int:pk>/', volunteer_application_detail_view, name='volunteer_application_detail'),
    path('volunteer-applications/<int:pk>/approve/', volunteer_application_approve_view, name='volunteer_application_approve'),
    path('volunteer-applications/<int:pk>/reject/', volunteer_application_reject_view, name='volunteer_application_reject'),

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
    path('events/', event_list_view, name='event_list'),
    path('events/create/', create_event_view, name='create_event'),
    path('broadcast/', broadcast_view, name='broadcast'),
    path('broadcasts/', broadcast_list_view, name='broadcast_list'),
    path('reports/', photo_reports_view, name='photo_reports'),

    # Admin/curator people directory
    path('people/<str:role>/', people_list_view, name='people_list'),

    # CRM
    path('crm/tasks/', crm_tasks_view, name='crm_tasks'),
    path('crm/volunteers/<int:pk>/', crm_volunteer_detail_view, name='crm_volunteer_detail'),

    # Map
    path('map/', map_view, name='map'),
    path('map/data/', map_data_view, name='map_data'),
    path('location/update/', update_location_view, name='update_location'),
]
