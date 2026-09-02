"""myapp data models.

Split into one module per domain area. Every public name is re-exported here so
existing imports keep working unchanged:

    from myapp.models import HelpRequest, OVERDUE_THRESHOLD, PRIORITY_CHOICES

New domain areas (emergency, donations, lost & found pets) get their own module
in this package plus an entry below.
"""

from .events import EVENT_REGION_CHOICES, Broadcast, Event
from .help_requests import (
    HELP_TYPE_CHOICES,
    OVERDUE_THRESHOLD,
    PRIORITY_CHOICES,
    PRIORITY_EMERGENCY,
    PRIORITY_HIGH,
    PRIORITY_NORMAL,
    STATUS_CHOICES,
    WORK_STAGE_ARRIVED,
    WORK_STAGE_ASSIGNED,
    WORK_STAGE_CHOICES,
    WORK_STAGE_EN_ROUTE,
    WORK_STAGE_IN_PROGRESS,
    WORK_STAGE_ORDER,
    HelpRequest,
)
from .photo_reports import PhotoReport
from .volunteer_applications import VolunteerApplication

__all__ = [
    # help_requests
    "HelpRequest",
    "HELP_TYPE_CHOICES",
    "STATUS_CHOICES",
    "PRIORITY_CHOICES",
    "PRIORITY_NORMAL",
    "PRIORITY_HIGH",
    "PRIORITY_EMERGENCY",
    "WORK_STAGE_CHOICES",
    "WORK_STAGE_ORDER",
    "WORK_STAGE_ASSIGNED",
    "WORK_STAGE_EN_ROUTE",
    "WORK_STAGE_ARRIVED",
    "WORK_STAGE_IN_PROGRESS",
    "OVERDUE_THRESHOLD",
    # events
    "Event",
    "Broadcast",
    "EVENT_REGION_CHOICES",
    # volunteer_applications
    "VolunteerApplication",
    # photo_reports
    "PhotoReport",
]
