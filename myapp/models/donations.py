"""In-kind donations — material assistance, not money.

Donors (individuals or local businesses — bakeries, shops, bazaars, pharmacies)
offer **physical goods**: food, bakery products, clothing, footwear, hygiene
kits, blankets, school supplies, water, household items. Generation Connect
routes an offer through a curator to a volunteer and on to a client or a help
request.

There is **no payment provider and no money anywhere in this module.** A
``Donation`` is an *offer of goods* with a physical-handover lifecycle:

    pending -> approved -> ready -> received -> distributed
                    \\----------- cancelled -----------/   (from any open state)

Curators and admins review offers (``approve`` / ``cancel``), a volunteer can be
assigned to collect or deliver them, and the volunteer or staff advance
``ready -> received -> distributed`` as the goods change hands.

``Product`` is a small catalogue of *needed* items — what the organisation is
asking people to donate — not a priced store: no ``price``, no ``currency``, no
stock counts. Add those only if a real inventory need appears.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users

# What a donor can offer. Shared by Product.category and Donation.category.
DONATION_CATEGORY_CHOICES = [
    ("food", "Продукты питания"),
    ("bakery", "Хлеб и выпечка"),
    ("clothing", "Одежда"),
    ("footwear", "Обувь"),
    ("hygiene", "Гигиена"),
    ("medicine", "Медикаменты"),
    ("school", "Школьные принадлежности"),
    ("blankets", "Пледы и постельное"),
    ("water", "Питьевая вода"),
    ("household", "Хозяйственные товары"),
    ("other", "Другое"),
]

DONOR_TYPE_INDIVIDUAL = "individual"
DONOR_TYPE_BUSINESS = "business"
DONATION_DONOR_TYPE_CHOICES = [
    (DONOR_TYPE_INDIVIDUAL, "Частное лицо"),
    (DONOR_TYPE_BUSINESS, "Организация / бизнес"),
]

FULFILMENT_PICKUP = "pickup"
FULFILMENT_DROPOFF = "dropoff"
DONATION_FULFILMENT_CHOICES = [
    (FULFILMENT_PICKUP, "Забрать у донора"),
    (FULFILMENT_DROPOFF, "Донор привезёт сам"),
]

# Generous but finite — a guard rail for a foundation with no approval gate on
# creation. "50 loaves", "30 packages" — five digits is plenty.
MAX_DONATION_QUANTITY = 100000

DONATION_PENDING = "pending"
DONATION_APPROVED = "approved"
DONATION_READY = "ready"
DONATION_RECEIVED = "received"
DONATION_DISTRIBUTED = "distributed"
DONATION_CANCELLED = "cancelled"
DONATION_STATUS_CHOICES = [
    (DONATION_PENDING, "Ожидает проверки"),
    (DONATION_APPROVED, "Одобрено"),
    (DONATION_READY, "Готово к передаче"),
    (DONATION_RECEIVED, "Получено"),
    (DONATION_DISTRIBUTED, "Распределено"),
    (DONATION_CANCELLED, "Отменено"),
]
# Non-terminal — an offer that still needs coordination.
DONATION_OPEN_STATUSES = (
    DONATION_PENDING,
    DONATION_APPROVED,
    DONATION_READY,
    DONATION_RECEIVED,
)

# Forward-only. distributed / cancelled are terminal. Mirrors EmergencyReport.
DONATION_ALLOWED_TRANSITIONS = {
    DONATION_PENDING: {DONATION_APPROVED, DONATION_CANCELLED},
    DONATION_APPROVED: {DONATION_READY, DONATION_CANCELLED},
    DONATION_READY: {DONATION_RECEIVED, DONATION_CANCELLED},
    DONATION_RECEIVED: {DONATION_DISTRIBUTED},
    DONATION_DISTRIBUTED: set(),
    DONATION_CANCELLED: set(),
}
# Statuses from which a volunteer can be assigned to collect/deliver the goods.
DONATION_ASSIGNABLE_STATUSES = (DONATION_APPROVED, DONATION_READY)


class Product(models.Model):
    """A catalogue of items the organisation currently needs donated. Not an
    inventory system — no stock counts, SKUs or variants; add those only when a
    real store need appears."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    category = models.CharField(
        max_length=20, choices=DONATION_CATEGORY_CHOICES, default="other"
    )
    # Free text: "loaves", "kits", "kg", "packages", "pairs".
    unit = models.CharField(max_length=40, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Нужный товар"
        verbose_name_plural = "Нужные товары"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Donation(models.Model):
    PENDING = DONATION_PENDING
    APPROVED = DONATION_APPROVED
    READY = DONATION_READY
    RECEIVED = DONATION_RECEIVED
    DISTRIBUTED = DONATION_DISTRIBUTED
    CANCELLED = DONATION_CANCELLED
    OPEN_STATUSES = DONATION_OPEN_STATUSES

    # SET_NULL (not CASCADE/PROTECT): users are deactivated, not deleted, but if
    # one ever is removed the coordination record must survive.
    donor = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="donations"
    )
    donor_type = models.CharField(
        max_length=20, choices=DONATION_DONOR_TYPE_CHOICES, default=DONOR_TYPE_INDIVIDUAL
    )
    # The bakery / shop / bazaar name when donor_type == business.
    organization_name = models.CharField(max_length=200, blank=True)

    # Either a catalogue item…
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="donations"
    )
    # …or a free-text description of the goods.
    item_name = models.CharField(max_length=200, blank=True)
    category = models.CharField(
        max_length=20, choices=DONATION_CATEGORY_CHOICES, default="other"
    )
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(MAX_DONATION_QUANTITY)]
    )
    unit = models.CharField(max_length=40, blank=True)
    description = models.TextField(blank=True)

    fulfilment = models.CharField(
        max_length=20, choices=DONATION_FULFILMENT_CHOICES, default=FULFILMENT_PICKUP
    )
    location = models.CharField(max_length=255, blank=True)
    region = models.CharField(max_length=100, choices=REGION_CHOICES, blank=True)

    status = models.CharField(
        max_length=20, choices=DONATION_STATUS_CHOICES, default=DONATION_PENDING, db_index=True
    )
    message = models.TextField(blank=True)

    # The volunteer collecting / delivering the goods (optional).
    assigned_volunteer = models.ForeignKey(
        Users,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_donations",
    )
    # The curator / admin who last reviewed or advanced the offer.
    reviewed_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_donations"
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    distributed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Предложение помощи"
        verbose_name_plural = "Предложения помощи"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        who = self.donor.username if self.donor else "—"
        return f"Предложение #{self.pk} — {self.item_label} ×{self.quantity} ({who})"

    def clean(self):
        if self.quantity is None or self.quantity < 1 or self.quantity > MAX_DONATION_QUANTITY:
            raise ValidationError({"quantity": "Некорректное количество."})
        if not (self.item_name or "").strip() and self.product_id is None:
            raise ValidationError("Укажите товар из каталога или название предмета.")
        if self.donor_type == DONOR_TYPE_BUSINESS and not (self.organization_name or "").strip():
            raise ValidationError({"organization_name": "Укажите название организации."})

    # --- display helpers ---------------------------------------------------

    @property
    def item_label(self):
        if self.item_name:
            return self.item_name
        return self.product.name if self.product else "—"

    @property
    def donor_label(self):
        """Who offered this — the organisation name for a business, else the
        donor's display name. Never contact details."""
        if self.donor_type == DONOR_TYPE_BUSINESS and self.organization_name:
            return self.organization_name
        return self.donor.username if self.donor else "—"

    @property
    def is_open(self):
        return self.status in DONATION_OPEN_STATUSES

    # --- lifecycle -------------------------------------------------------

    def can_transition_to(self, target):
        return target in DONATION_ALLOWED_TRANSITIONS.get(self.status, set())

    def _apply_transition(self, target, actor, at_field):
        if not self.can_transition_to(target):
            raise ValueError(f"donation #{self.pk}: {self.status} -> {target} is not allowed")

        # Conditional UPDATE, not fetch-then-save: the WHERE clause is evaluated
        # by the database as one atomic statement, so two concurrent staff
        # actions on the same offer (e.g. one approving, one cancelling) can't
        # both win — the loser sees 0 rows affected instead of silently
        # overwriting the winner. Same idiom as accept_task_view and the
        # overdue/stale sweep claims.
        now = timezone.now()
        updated = Donation.objects.filter(pk=self.pk, status=self.status).update(
            status=target, reviewed_by=actor, updated_at=now, **{at_field: now}
        )
        if not updated:
            current_status = (
                Donation.objects.filter(pk=self.pk).values_list("status", flat=True).first()
            )
            raise ValueError(
                f"donation #{self.pk}: {self.status} -> {target} is not allowed "
                f"(status already changed to {current_status})"
            )

        self.status = target
        setattr(self, at_field, now)
        self.reviewed_by = actor
        self.updated_at = now

    def approve(self, actor):
        self._apply_transition(DONATION_APPROVED, actor, "approved_at")

    def mark_ready(self, actor):
        self._apply_transition(DONATION_READY, actor, "ready_at")

    def mark_received(self, actor):
        self._apply_transition(DONATION_RECEIVED, actor, "received_at")

    def mark_distributed(self, actor):
        self._apply_transition(DONATION_DISTRIBUTED, actor, "distributed_at")

    def cancel(self, actor):
        self._apply_transition(DONATION_CANCELLED, actor, "cancelled_at")

    def assign_volunteer(self, volunteer, actor):
        """Assign (or clear, with volunteer=None) the volunteer who will collect
        or deliver the goods. Not a status change — only allowed while the offer
        is approved or ready. Conditional on status so it can't race a cancel."""
        now = timezone.now()
        updated = Donation.objects.filter(
            pk=self.pk, status__in=DONATION_ASSIGNABLE_STATUSES
        ).update(assigned_volunteer=volunteer, reviewed_by=actor, updated_at=now)
        if not updated:
            raise ValueError(
                f"donation #{self.pk}: cannot assign a volunteer in status {self.status}"
            )
        self.assigned_volunteer = volunteer
        self.reviewed_by = actor
        self.updated_at = now
