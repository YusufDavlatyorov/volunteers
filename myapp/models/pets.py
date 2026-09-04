"""Lost & Found pets — a small community board for reuniting lost animals with
their owners and the people who find them.

Deliberately minimal: one ``PetReport`` model with a lost/found flag, an optional
photo and location, and a short lifecycle. This is **not** an animal-shelter or
pet-management system — no medical records, adoption, microchip registry, etc.

Privacy: a report carries the reporter's identity and an optional contact phone.
Neither is ever exposed on the public board or the map — only the reporter and
staff (curator/admin) see them. ``myapp.services.pets`` owns the safe-field
serialisation and the deterministic "possible matches" suggestion; state
transitions are the model methods here (same pattern as ``EmergencyReport`` /
``Donation``).
"""

from django.db import models
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users, validate_file_size


REPORT_LOST = "lost"
REPORT_FOUND = "found"
PET_REPORT_TYPE_CHOICES = [
    (REPORT_LOST, "Потерян"),
    (REPORT_FOUND, "Найден"),
]

SPECIES_DOG = "dog"
SPECIES_CAT = "cat"
SPECIES_BIRD = "bird"
SPECIES_OTHER = "other"
PET_SPECIES_CHOICES = [
    (SPECIES_DOG, "Собака"),
    (SPECIES_CAT, "Кошка"),
    (SPECIES_BIRD, "Птица"),
    (SPECIES_OTHER, "Другое"),
]

STATUS_OPEN = "open"
STATUS_MATCHED = "matched"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"
PET_STATUS_CHOICES = [
    (STATUS_OPEN, "Открыт"),
    (STATUS_MATCHED, "Есть совпадение"),
    (STATUS_RESOLVED, "Воссоединён"),
    (STATUS_CLOSED, "Закрыт"),
]

# Reports that still belong on the active board / map.
PET_OPEN_STATUSES = (STATUS_OPEN, STATUS_MATCHED)

# Forward-mostly. resolved / closed are terminal; a "matched" report can fall
# back to "open" if the lead did not pan out.
PET_ALLOWED_TRANSITIONS = {
    STATUS_OPEN: {STATUS_MATCHED, STATUS_RESOLVED, STATUS_CLOSED},
    STATUS_MATCHED: {STATUS_OPEN, STATUS_RESOLVED, STATUS_CLOSED},
    STATUS_RESOLVED: set(),
    STATUS_CLOSED: set(),
}

# The transitions a report's own reporter may drive from the UI. Everything else
# (mark matched, reopen) is staff-only — enforced in the view, mirrored here so a
# future caller can check without re-deriving the rule.
PET_OWNER_TRANSITIONS = (STATUS_RESOLVED, STATUS_CLOSED)


class PetReport(models.Model):
    REPORT_LOST = REPORT_LOST
    REPORT_FOUND = REPORT_FOUND
    STATUS_OPEN = STATUS_OPEN
    STATUS_MATCHED = STATUS_MATCHED
    STATUS_RESOLVED = STATUS_RESOLVED
    STATUS_CLOSED = STATUS_CLOSED
    OPEN_STATUSES = PET_OPEN_STATUSES
    OWNER_TRANSITIONS = PET_OWNER_TRANSITIONS

    # CASCADE (like HelpRequest.client): the report holds the reporter's own
    # contact details and there is nobody to manage it once the account is gone.
    reporter = models.ForeignKey(
        Users, on_delete=models.CASCADE, related_name="pet_reports"
    )
    report_type = models.CharField(
        max_length=10, choices=PET_REPORT_TYPE_CHOICES, db_index=True
    )
    pet_name = models.CharField(max_length=120, blank=True)
    species = models.CharField(max_length=20, choices=PET_SPECIES_CHOICES, default=SPECIES_OTHER)
    breed = models.CharField(max_length=120, blank=True)
    description = models.TextField()
    region = models.CharField(max_length=100, choices=REGION_CHOICES, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    # Optional faster channel for staff to reach the reporter. NEVER rendered on
    # the public board or the map JSON — reporter + staff only (services.pets).
    contact_phone = models.CharField(max_length=30, blank=True)
    image = models.ImageField(upload_to="pets/", blank=True, validators=[validate_file_size])
    status = models.CharField(
        max_length=20, choices=PET_STATUS_CHOICES, default=STATUS_OPEN, db_index=True
    )
    # The last account to change the status (staff moderator, or the reporter
    # closing their own report). Shown to staff only.
    reviewed_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_pet_reports"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    matched_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Объявление о животном"
        verbose_name_plural = "Объявления о животных"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "report_type", "-created_at"])]

    def __str__(self):
        return f"{self.get_report_type_display()}: {self.pet_name or self.get_species_display()} (#{self.pk})"

    @property
    def has_location(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def is_open(self):
        return self.status in PET_OPEN_STATUSES

    @property
    def display_name(self):
        return self.pet_name or self.get_species_display()

    def can_transition_to(self, target):
        return target in PET_ALLOWED_TRANSITIONS.get(self.status, set())

    _STATUS_STAMP = {
        STATUS_MATCHED: "matched_at",
        STATUS_RESOLVED: "resolved_at",
        STATUS_CLOSED: "closed_at",
    }

    def _apply(self, target, actor):
        if not self.can_transition_to(target):
            raise ValueError(f"pet report #{self.pk}: {self.status} -> {target} is not allowed")
        self.status = target
        fields = ["status", "updated_at"]
        stamp = self._STATUS_STAMP.get(target)
        if stamp and getattr(self, stamp) is None:
            setattr(self, stamp, timezone.now())
            fields.append(stamp)
        if actor is not None:
            self.reviewed_by = actor
            fields.append("reviewed_by")
        self.save(update_fields=fields)

    def mark_matched(self, actor):
        """Staff: a plausible counterpart report has been found."""
        self._apply(STATUS_MATCHED, actor)

    def reopen(self, actor):
        """Staff: the match fell through — back onto the active board."""
        self._apply(STATUS_OPEN, actor)

    def resolve(self, actor):
        """Reporter or staff: the pet and its people are back together."""
        self._apply(STATUS_RESOLVED, actor)

    def close(self, actor):
        """Reporter or staff: the report is no longer needed (not a reunion)."""
        self._apply(STATUS_CLOSED, actor)
