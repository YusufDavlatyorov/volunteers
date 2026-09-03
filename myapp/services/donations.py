"""Donation & store foundation — the one place a donation is created and its
money is calculated.

No payment provider is involved. ``create_donation`` returns a ``pending``
donation; an admin later calls ``confirm`` / ``fulfill`` / ``cancel`` (which are
model methods, wrapped here so a view stays thin). A future payment service
plugs in at ``confirm`` — nothing else changes.

Money safety:
  * every amount is a ``Decimal``;
  * for a product-linked donation the amount is ``unit_price * quantity``
    computed here, and any caller-supplied ``amount`` is discarded;
  * ``unit_price_snapshot`` freezes the price so later ``Product.price`` edits
    never touch existing donations;
  * quantity and amount bounds are validated before the row is written.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction

from ..models import (
    DEFAULT_CURRENCY,
    MAX_DONATION_AMOUNT,
    MAX_DONATION_QUANTITY,
    MIN_MONEY,
    Donation,
    Product,
)

_CENTS = Decimal("0.01")


def _to_money(value):
    """Coerce an arbitrary caller value to a 2dp Decimal, or raise
    ValidationError. Rejects NaN / infinity / junk / float noise."""
    if value is None or value == "":
        raise ValidationError("Укажите сумму пожертвования.")
    try:
        amount = Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError("Некорректная сумма.")
    if not amount.is_finite():
        raise ValidationError("Некорректная сумма.")
    return amount


def _validate_quantity(quantity):
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ValidationError("Некорректное количество.")
    if quantity < 1 or quantity > MAX_DONATION_QUANTITY:
        raise ValidationError(f"Количество должно быть от 1 до {MAX_DONATION_QUANTITY}.")
    return quantity


def create_donation(*, donor, product=None, quantity=1, amount=None, currency=None, message=""):
    """Create a ``pending`` donation and return it.

    Two shapes:
      * **product-linked** — pass ``product`` (+ optional ``quantity``). The
        amount and currency come from the product; ``amount`` / ``currency``
        args are ignored. The active-product check and the insert are one
        atomic unit so a product deactivated mid-request can't slip through.
      * **free** — pass ``amount`` (+ optional ``currency``). Must be > 0 and
        within ``MAX_DONATION_AMOUNT``.
    """
    message = (message or "").strip()[:1000]

    with transaction.atomic():
        if product is not None:
            quantity = _validate_quantity(quantity)
            # Re-read inside the transaction: the product may have been
            # deactivated between the view rendering the form and this call.
            product = Product.objects.get(pk=product.pk)
            if not product.is_active:
                raise ValidationError("Этот товар сейчас недоступен для пожертвований.")
            unit_price = product.price
            total = (unit_price * quantity).quantize(_CENTS, rounding=ROUND_HALF_UP)
            resolved_currency = product.currency
            snapshot = unit_price
        else:
            quantity = 1
            snapshot = None
            resolved_currency = (currency or DEFAULT_CURRENCY)
            total = _to_money(amount)

        if total < MIN_MONEY:
            raise ValidationError("Сумма пожертвования должна быть больше нуля.")
        if total > MAX_DONATION_AMOUNT:
            raise ValidationError("Сумма пожертвования слишком велика.")

        donation = Donation(
            donor=donor,
            product=product,
            quantity=quantity,
            unit_price_snapshot=snapshot,
            amount=total,
            currency=resolved_currency,
            message=message,
            status=Donation.PENDING,
        )
        donation.full_clean(exclude=["donor"])  # backstop the model-level money rules
        donation.save()
    return donation


def confirm_donation(donation, *, actor):
    """Admin verified the funds arrived (out of band — there is no gateway)."""
    donation.confirm(actor)
    return donation


def fulfill_donation(donation, *, actor):
    """The purpose the donation was for has been delivered."""
    donation.fulfill(actor)
    return donation


def cancel_donation(donation, *, actor):
    donation.cancel(actor)
    return donation


# --- read helpers -----------------------------------------------------------

def donations_for(user):
    """A donor's own donations, newest first. The queryset-level scope that
    stops one user seeing another's — templates never re-filter."""
    return (
        Donation.objects.filter(donor=user)
        .select_related("product", "reviewed_by")
        .order_by("-created_at")
    )


def all_donations():
    """Every donation — admin only (see the view guard)."""
    return (
        Donation.objects.select_related("donor", "product", "reviewed_by")
        .order_by("-created_at")
    )


def active_products():
    return Product.objects.filter(is_active=True).order_by("name")


def donation_summary():
    """Compact aggregates for the admin dashboard: count per status, and the
    confirmed+fulfilled total per currency (amounts across currencies are never
    summed together). Constant number of queries."""
    from django.db.models import Count, Q, Sum

    by_status = Donation.objects.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=Donation.PENDING)),
        confirmed=Count("id", filter=Q(status=Donation.CONFIRMED)),
        fulfilled=Count("id", filter=Q(status=Donation.FULFILLED)),
        cancelled=Count("id", filter=Q(status=Donation.CANCELLED)),
    )
    raised = (
        Donation.objects.filter(status__in=[Donation.CONFIRMED, Donation.FULFILLED])
        .values("currency")
        .annotate(total=Sum("amount"))
        .order_by("currency")
    )
    by_status["raised"] = [{"currency": r["currency"], "total": r["total"]} for r in raised]
    return by_status
