"""myapp data models.

Split into one module per domain area. Every public name is re-exported here so
existing imports keep working unchanged:

    from myapp.models import HelpRequest, OVERDUE_THRESHOLD, PRIORITY_CHOICES

New domain areas get their own module in this package plus an entry below.
"""

from .donations import (
    DONATION_ALLOWED_TRANSITIONS,
    DONATION_ASSIGNABLE_STATUSES,
    DONATION_CATEGORY_CHOICES,
    DONATION_DONOR_TYPE_CHOICES,
    DONATION_FULFILMENT_CHOICES,
    DONATION_OPEN_STATUSES,
    DONATION_STATUS_CHOICES,
    MAX_DONATION_QUANTITY,
    Donation,
    Product,
)
from .emergency import (
    ALLOWED_TRANSITIONS as EMERGENCY_ALLOWED_TRANSITIONS,
    EmergencyReport,
    OPEN_STATUSES as EMERGENCY_OPEN_STATUSES,
)
from .events import EVENT_REGION_CHOICES, Broadcast, Event
from .help_requests import (
    HELP_TYPE_CHOICES,
    OVERDUE_THRESHOLD,
    STALE_PENDING_THRESHOLD,
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
from .pets import (
    PET_ALLOWED_TRANSITIONS,
    PET_OPEN_STATUSES,
    PET_OWNER_TRANSITIONS,
    PET_REPORT_TYPE_CHOICES,
    PET_SPECIES_CHOICES,
    PET_STATUS_CHOICES,
    PetReport,
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
    "STALE_PENDING_THRESHOLD",
    # events
    "Event",
    "Broadcast",
    "EVENT_REGION_CHOICES",
    # volunteer_applications
    "VolunteerApplication",
    # pets (lost & found)
    "PetReport",
    "PET_REPORT_TYPE_CHOICES",
    "PET_SPECIES_CHOICES",
    "PET_STATUS_CHOICES",
    "PET_OPEN_STATUSES",
    "PET_ALLOWED_TRANSITIONS",
    "PET_OWNER_TRANSITIONS",
    # photo_reports
    "PhotoReport",
    # emergency
    "EmergencyReport",
    "EMERGENCY_OPEN_STATUSES",
    "EMERGENCY_ALLOWED_TRANSITIONS",
    # donations (in-kind / material assistance)
    "Product",
    "Donation",
    "DONATION_STATUS_CHOICES",
    "DONATION_OPEN_STATUSES",
    "DONATION_ALLOWED_TRANSITIONS",
    "DONATION_ASSIGNABLE_STATUSES",
    "DONATION_CATEGORY_CHOICES",
    "DONATION_DONOR_TYPE_CHOICES",
    "DONATION_FULFILMENT_CHOICES",
    "MAX_DONATION_QUANTITY",
]
