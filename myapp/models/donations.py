"""Donations & store foundation.

Deliberately minimal: a ``Product`` catalogue (things the organisation needs
funded) and a ``Donation`` record. There is **no payment provider** — a donation
is created ``pending`` and an admin marks it ``confirmed`` once the transfer is
verified out of band. The money seam is clean: a future payment service only
has to flip status and stamp a reference.

Money rules (enforced by the service in ``myapp.services.donations`` and backed
up by ``clean()`` here):
  * ``DecimalField`` only, never float.
  * A product-linked donation's ``amount`` is computed server-side from
    ``unit_price_snapshot * quantity`` — a client-submitted amount is ignored.
  * ``unit_price_snapshot`` freezes the product price at donation time, so a
    later ``Product.price`` change never rewrites history.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from accounts.models import Users

CURRENCY_CHOICES = [
    ("TJS", "Сомони (TJS)"),
    ("USD", "USD"),
    ("EUR", "EUR"),
]
DEFAULT_CURRENCY = "TJS"

# Guard rails for a foundation with no payment gate — generous but finite.
MIN_MONEY = Decimal("0.01")
MAX_DONATION_AMOUNT = Decimal("1000000.00")
MAX_DONATION_QUANTITY = 999

DONATION_PENDING = "pending"
DONATION_CONFIRMED = "confirmed"
DONATION_FULFILLED = "fulfilled"
DONATION_CANCELLED = "cancelled"
DONATION_STATUS_CHOICES = [
    (DONATION_PENDING, "Ожидает подтверждения"),
    (DONATION_CONFIRMED, "Подтверждена"),
    (DONATION_FULFILLED, "Исполнена"),
    (DONATION_CANCELLED, "Отменена"),
]
DONATION_OPEN_STATUSES = (DONATION_PENDING, DONATION_CONFIRMED)

# Forward-only. fulfilled / cancelled are terminal. Mirrors EmergencyReport.
DONATION_ALLOWED_TRANSITIONS = {
    DONATION_PENDING: {DONATION_CONFIRMED, DONATION_CANCELLED},
    DONATION_CONFIRMED: {DONATION_FULFILLED, DONATION_CANCELLED},
    DONATION_FULFILLED: set(),
    DONATION_CANCELLED: set(),
}


class Product(models.Model):
    """A catalogue item a donor can fund. Not an inventory system — no stock
    counts, SKUs or variants; add those only when a real store need appears."""

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    price = models.DecimalField(
        max_digits=10, decimal_places=2, validators=[MinValueValidator(MIN_MONEY)]
    )
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default=DEFAULT_CURRENCY)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Товар"
        verbose_name_plural = "Товары"
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.price} {self.currency})"

    def clean(self):
        if self.price is not None and self.price < MIN_MONEY:
            raise ValidationError({"price": "Цена должна быть больше нуля."})


class Donation(models.Model):
    PENDING = DONATION_PENDING
    CONFIRMED = DONATION_CONFIRMED
    FULFILLED = DONATION_FULFILLED
    CANCELLED = DONATION_CANCELLED
    OPEN_STATUSES = DONATION_OPEN_STATUSES

    # SET_NULL (not CASCADE/PROTECT): users are deactivated, not deleted, but if
    # one ever is removed the financial record must survive.
    donor = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="donations"
    )
    product = models.ForeignKey(
        Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="donations"
    )
    quantity = models.PositiveIntegerField(
        default=1, validators=[MinValueValidator(1), MaxValueValidator(MAX_DONATION_QUANTITY)]
    )
    # Frozen copy of Product.price at creation — historical amounts never move.
    unit_price_snapshot = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    # Field-level bounds in addition to clean() and the service/form checks, so
    # the money rule holds on any path that runs validators — including a
    # Product/Donation created straight through the Django admin or a shell
    # full_clean(), not just the donate flow.
    amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(MIN_MONEY), MaxValueValidator(MAX_DONATION_AMOUNT)],
    )
    currency = models.CharField(max_length=3, choices=CURRENCY_CHOICES, default=DEFAULT_CURRENCY)
    status = models.CharField(
        max_length=20, choices=DONATION_STATUS_CHOICES, default=DONATION_PENDING, db_index=True
    )
    message = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        Users, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_donations"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Пожертвование"
        verbose_name_plural = "Пожертвования"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    def __str__(self):
        who = self.donor.username if self.donor else "—"
        return f"Пожертвование #{self.pk} — {self.amount} {self.currency} ({who})"

    def clean(self):
        if self.amount is None or self.amount < MIN_MONEY:
            raise ValidationError({"amount": "Сумма пожертвования должна быть больше нуля."})
        if self.amount > MAX_DONATION_AMOUNT:
            raise ValidationError({"amount": "Сумма слишком велика."})
        if self.quantity < 1 or self.quantity > MAX_DONATION_QUANTITY:
            raise ValidationError({"quantity": "Некорректное количество."})

    @property
    def is_open(self):
        return self.status in DONATION_OPEN_STATUSES

    def can_transition_to(self, target):
        return target in DONATION_ALLOWED_TRANSITIONS.get(self.status, set())

    def _apply_transition(self, target, actor, at_field):
        if not self.can_transition_to(target):
            raise ValueError(f"donation #{self.pk}: {self.status} -> {target} is not allowed")

        # Conditional UPDATE, not fetch-then-save: the WHERE clause is
        # evaluated by the database as part of one atomic statement, so two
        # concurrent admin actions on the same donation (e.g. one confirming,
        # one cancelling) can't both win — the loser sees 0 rows affected
        # instead of silently overwriting the winner's transition. Same idiom
        # as accept_task_view and the overdue/stale sweep claims.
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

    def confirm(self, actor):
        self._apply_transition(DONATION_CONFIRMED, actor, "confirmed_at")

    def fulfill(self, actor):
        self._apply_transition(DONATION_FULFILLED, actor, "fulfilled_at")

    def cancel(self, actor):
        self._apply_transition(DONATION_CANCELLED, actor, "cancelled_at")
