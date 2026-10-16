from django.urls import path
from django.views.generic import RedirectView
from .views_notifications import (
    notification_preferences_view,
    notification_read_view,
    notifications_bell_view,
    notifications_list_view,
    notifications_read_all_view,
    realtime_poll_view,
)
from .views_tracking import (
    task_location_view,
    tracking_consent_view,
    tracking_stop_view,
)
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
    donate_view,
    donation_detail_view,
    donation_update_view,
    donations_admin_view,
    my_assigned_donations_view,
    my_donations_view,
    my_pet_reports_view,
    pet_list_view,
    pet_report_create_view,
    pet_report_delete_view,
    pet_report_detail_view,
    pet_report_edit_view,
    pet_report_status_view,
    emergency_detail_view,
    emergency_list_view,
    emergency_report_view,
    emergency_update_view,
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
    photo_reports_view,
    task_advance_stage_view,
    task_assign_volunteer_view,
    task_notify_volunteer_view,
    task_recommendations_view,
    task_route_view,
    task_cancel_view,
    update_location_view,
    volunteer_application_view,
    volunteer_applications_view,
    volunteer_application_detail_view,
    volunteer_application_approve_view,
    volunteer_application_reject_view,
)

urlpatterns = [
    # The dashboard lives at /dashboard/ (server/urls.py); old bookmarks still land there.
    path('dashboard/', RedirectView.as_view(pattern_name='dashboard', query_string=True), name='dashboard_legacy'),
    path('panel/', admin_panel_view, name='admin_panel'),
    # Tasks
    path('tasks/', task_list_view, name='task_list'),
    path('tasks/<int:pk>/accept/', accept_task_view, name='accept_task'),
    path('tasks/<int:pk>/complete/', complete_task_view, name='complete_task'),
    path('tasks/<int:pk>/stage/', task_advance_stage_view, name='task_advance_stage'),
    path('tasks/<int:pk>/route/', task_route_view, name='task_route'),
    path('tasks/<int:pk>/location/', task_location_view, name='task_location'),
    path('tasks/<int:pk>/tracking/stop/', tracking_stop_view, name='tracking_stop'),
    path('tracking/consent/', tracking_consent_view, name='tracking_consent'),
    path('tasks/<int:pk>/cancel/', task_cancel_view, name='task_cancel'),
    path('tasks/<int:pk>/recommendations/', task_recommendations_view, name='task_recommendations'),
    path('tasks/<int:pk>/recommendations/notify/<int:volunteer_id>/', task_notify_volunteer_view, name='task_notify_volunteer'),
    path('tasks/<int:pk>/recommendations/assign/<int:volunteer_id>/', task_assign_volunteer_view, name='task_assign_volunteer'),
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

    # Emergency / SOS
    path('emergency/', emergency_list_view, name='emergency_list'),
    path('emergency/report/<int:task_pk>/', emergency_report_view, name='emergency_report'),
    path('emergency/<int:pk>/', emergency_detail_view, name='emergency_detail'),
    path('emergency/<int:pk>/update/', emergency_update_view, name='emergency_update'),

    # Map
    path('map/', map_view, name='map'),
    path('map/data/', map_data_view, name='map_data'),
    path('location/update/', update_location_view, name='update_location'),

    # Lost & Found pets
    path('pets/', pet_list_view, name='pet_list'),
    path('pets/new/', pet_report_create_view, name='pet_report_create'),
    path('pets/mine/', my_pet_reports_view, name='my_pet_reports'),
    path('pets/<int:pk>/', pet_report_detail_view, name='pet_report_detail'),
    path('pets/<int:pk>/edit/', pet_report_edit_view, name='pet_report_edit'),
    path('pets/<int:pk>/delete/', pet_report_delete_view, name='pet_report_delete'),
    path('pets/<int:pk>/status/', pet_report_status_view, name='pet_report_status'),

    # In-kind donations — material assistance (no money)
    path('donate/', donate_view, name='donate'),
    path('donations/', my_donations_view, name='my_donations'),
    path('donations/assigned/', my_assigned_donations_view, name='assigned_donations'),
    path('donations/manage/', donations_admin_view, name='donations_admin'),
    path('donations/<int:pk>/', donation_detail_view, name='donation_detail'),
    path('donations/<int:pk>/update/', donation_update_view, name='donation_update'),

    # Notifications (Feature Pack v2, feature 11) + realtime polling fallback (12)
    path('notifications/', notifications_list_view, name='notifications'),
    path('notifications/bell/', notifications_bell_view, name='notifications_bell'),
    path('notifications/read-all/', notifications_read_all_view, name='notifications_read_all'),
    path('notifications/preferences/', notification_preferences_view, name='notification_preferences'),
    path('notifications/<int:pk>/read/', notification_read_view, name='notification_read'),
    path('realtime/poll/', realtime_poll_view, name='realtime_poll'),
]

# Client reviews (Feature Pack v2, feature 9) — myapp/views_feedback.py
from .views_feedback import crm_reviews_view, my_reviews_view, review_hide_view, task_review_view  # noqa: E402

urlpatterns += [
    path('tasks/<int:pk>/review/', task_review_view, name='task_review'),
    path('reviews/mine/', my_reviews_view, name='my_reviews'),
    path('reviews/<int:pk>/hide/', review_hide_view, name='review_hide'),
    path('crm/reviews/', crm_reviews_view, name='crm_reviews'),
]

# Client <-> volunteer chat (Feature Pack v2, feature 8) — myapp/views_chat.py
from .views_chat import (  # noqa: E402
    chat_message_hide_view,
    chat_message_image_view,
    chat_message_report_view,
    chat_message_review_view,
    task_chat_messages_view,
    task_chat_read_view,
    task_chat_send_view,
    task_chat_upload_view,
    task_chat_view,
)

urlpatterns += [
    path('tasks/<int:pk>/chat/', task_chat_view, name='task_chat'),
    path('tasks/<int:pk>/chat/messages/', task_chat_messages_view, name='task_chat_messages'),
    path('tasks/<int:pk>/chat/send/', task_chat_send_view, name='task_chat_send'),
    path('tasks/<int:pk>/chat/upload/', task_chat_upload_view, name='task_chat_upload'),
    path('tasks/<int:pk>/chat/read/', task_chat_read_view, name='task_chat_read'),
    path('chat/messages/<int:message_id>/report/', chat_message_report_view, name='chat_message_report'),
    path('chat/messages/<int:message_id>/hide/', chat_message_hide_view, name='chat_message_hide'),
    path('chat/messages/<int:message_id>/review/', chat_message_review_view, name='chat_message_review'),
    path('chat/messages/<int:message_id>/image/<str:variant>/', chat_message_image_view, name='chat_message_image'),
]

# Feature Pack v2 milestone 4 — CRM analytics (admin + curator)
from .views_analytics import crm_analytics_csv_view, crm_analytics_heat_view, crm_analytics_view  # noqa: E402

urlpatterns += [
    path('crm/analytics/', crm_analytics_view, name='crm_analytics'),
    path('crm/analytics/heat.json', crm_analytics_heat_view, name='crm_analytics_heat'),
    path('crm/analytics/<slug:name>.csv', crm_analytics_csv_view, name='crm_analytics_csv'),
]

# Feature Pack v2 milestone 5 — volunteer community (Telegram)
from .views_community import community_invite_view, crm_award_mentor_view  # noqa: E402

urlpatterns += [
    path('community/invite/', community_invite_view, name='community_invite'),
    path('crm/volunteers/<int:pk>/mentor/', crm_award_mentor_view, name='crm_award_mentor'),
]
