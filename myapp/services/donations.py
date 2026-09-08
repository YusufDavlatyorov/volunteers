"""In-kind donations — the one place an offer of goods is created and advanced.

**No money, no payment provider.** ``create_offer`` returns a ``pending``
donation (an offer of physical goods); a curator or admin calls
``approve_offer`` / ``cancel_offer``; a volunteer can be assigned to collect or
deliver; and the goods move ``ready -> received -> distributed`` as they change
hands. All of that is model methods (see ``myapp.models.donations.Donation``)
wrapped here so views stay thin.

Safety:
  * quantity is validated (1..MAX_DONATION_QUANTITY) before the row is written;
  * a product-linked offer re-reads the product inside the transaction so a
    catalogue item deactivated mid-request can't slip through;
  * transitions use the model's conditional-UPDATE guard (no lost updates).
"""

import logging

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum

from ..models import (
    DONATION_CATEGORY_CHOICES,
    DONATION_DONOR_TYPE_CHOICES,
    DONATION_FULFILMENT_CHOICES,
    MAX_DONATION_QUANTITY,
    Donation,
    Product,
)
from ..models.donations import DONOR_TYPE_BUSINESS, FULFILMENT_PICKUP

logger = logging.getLogger(__name__)

_CATEGORY_KEYS = {value for value, _ in DONATION_CATEGORY_CHOICES}
_DONOR_TYPE_KEYS = {value for value, _ in DONATION_DONOR_TYPE_CHOICES}
_FULFILMENT_KEYS = {value for value, _ in DONATION_FULFILMENT_CHOICES}


def _validate_quantity(quantity):
    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise ValidationError("Некорректное количество.")
    if quantity < 1 or quantity > MAX_DONATION_QUANTITY:
        raise ValidationError(f"Количество должно быть от 1 до {MAX_DONATION_QUANTITY}.")
    return quantity


def create_offer(
    *,
    donor,
    donor_type="individual",
    organization_name="",
    product=None,
    item_name="",
    category="other",
    quantity=1,
    unit="",
    description="",
    fulfilment=FULFILMENT_PICKUP,
    location="",
    region="",
    message="",
):
    """Create a ``pending`` offer of goods and return it.

    Two shapes:
      * **catalogue** — pass ``product``; ``category`` / ``unit`` default to the
        product's when not given, ``item_name`` is left blank.
      * **free-text** — pass ``item_name`` (+ ``category`` / ``unit``).

    The active-product check and the insert are one atomic unit so a product
    deactivated mid-request can't slip through.
    """
    quantity = _validate_quantity(quantity)
    donor_type = donor_type if donor_type in _DONOR_TYPE_KEYS else "individual"
    fulfilment = fulfilment if fulfilment in _FULFILMENT_KEYS else FULFILMENT_PICKUP
    category = category if category in _CATEGORY_KEYS else "other"
    organization_name = (organization_name or "").strip()[:200]
    item_name = (item_name or "").strip()[:200]
    unit = (unit or "").strip()[:40]
    location = (location or "").strip()[:255]
    description = (description or "").strip()[:2000]
    message = (message or "").strip()[:1000]

    with transaction.atomic():
        if product is not None:
            # Re-read inside the transaction: the product may have been
            # deactivated between the view rendering the form and this call.
            product = Product.objects.get(pk=product.pk)
            if not product.is_active:
                raise ValidationError("Этот товар сейчас не нужен для пожертвований.")
            category = product.category
            unit = unit or product.unit
            item_name = ""
        elif not item_name:
            raise ValidationError("Укажите товар из каталога или название предмета.")

        if donor_type == DONOR_TYPE_BUSINESS and not organization_name:
            raise ValidationError("Укажите название организации.")

        offer = Donation(
            donor=donor,
            donor_type=donor_type,
            organization_name=organization_name,
            product=product,
            item_name=item_name,
            category=category,
            quantity=quantity,
            unit=unit,
            description=description,
            fulfilment=fulfilment,
            location=location,
            region=region or "",
            message=message,
            status=Donation.PENDING,
        )
        offer.full_clean(exclude=["donor"])  # backstop the model-level rules
        offer.save()
    return offer


def _log_transition(offer, action, actor):
    # IDs only — never the donor's message or contact details. One line for the
    # log aggregator, the operational audit trail (see CLAUDE.md).
    logger.info(
        "donation #%s %s by user #%s (category=%s, qty=%s, status=%s)",
        offer.pk, action, getattr(actor, "pk", None),
        offer.category, offer.quantity, offer.status,
    )


def approve_offer(offer, *, actor):
    """Curator/admin verified the offer is genuine and useful."""
    offer.approve(actor)
    _log_transition(offer, "approved", actor)
    return offer


def mark_offer_ready(offer, *, actor):
    """The donor has the goods ready for pickup / drop-off."""
    offer.mark_ready(actor)
    _log_transition(offer, "ready", actor)
    return offer


def mark_offer_received(offer, *, actor):
    """The goods have reached Generation Connect (collected or dropped off)."""
    offer.mark_received(actor)
    _log_transition(offer, "received", actor)
    return offer


def mark_offer_distributed(offer, *, actor):
    """The goods have been handed to the clients / help requests they were for."""
    offer.mark_distributed(actor)
    _log_transition(offer, "distributed", actor)
    return offer


def cancel_offer(offer, *, actor):
    offer.cancel(actor)
    _log_transition(offer, "cancelled", actor)
    return offer


def assign_offer_volunteer(offer, *, volunteer, actor):
    """Assign the volunteer who will collect or deliver the goods."""
    offer.assign_volunteer(volunteer, actor)
    logger.info(
        "donation #%s assigned to volunteer #%s by user #%s",
        offer.pk, getattr(volunteer, "pk", None), getattr(actor, "pk", None),
    )
    return offer


# --- read helpers -----------------------------------------------------------

def donations_for(user):
    """A donor's own offers, newest first. The queryset-level scope that stops
    one user seeing another's — templates never re-filter."""
    return (
        Donation.objects.filter(donor=user)
        .select_related("product", "reviewed_by", "assigned_volunteer")
        .order_by("-created_at")
    )


def assigned_to(volunteer):
    """Open offers a volunteer has been asked to collect or deliver."""
    return (
        Donation.objects.filter(
            assigned_volunteer=volunteer, status__in=Donation.OPEN_STATUSES
        )
        .select_related("product", "donor")
        .order_by("-updated_at")
    )


def all_offers():
    """Every offer — staff (curator + admin) only (see the view guard)."""
    return (
        Donation.objects.select_related("donor", "product", "reviewed_by", "assigned_volunteer")
        .order_by("-created_at")
    )


def pending_offers():
    """Offers still awaiting a curator's review — the dashboard queue."""
    return (
        Donation.objects.filter(status=Donation.PENDING)
        .select_related("donor", "product")
        .order_by("created_at")
    )


def active_products():
    return Product.objects.filter(is_active=True).order_by("name")


def offer_summary():
    """Operational aggregates for the staff dashboards — item counts, not money.
    Constant number of queries."""
    by_status = Donation.objects.aggregate(
        total=Count("id"),
        pending=Count("id", filter=Q(status=Donation.PENDING)),
        approved=Count("id", filter=Q(status=Donation.APPROVED)),
        ready=Count("id", filter=Q(status=Donation.READY)),
        received=Count("id", filter=Q(status=Donation.RECEIVED)),
        distributed=Count("id", filter=Q(status=Donation.DISTRIBUTED)),
        cancelled=Count("id", filter=Q(status=Donation.CANCELLED)),
        active_offers=Count("id", filter=Q(status__in=Donation.OPEN_STATUSES)),
        items_received=Sum(
            "quantity", filter=Q(status__in=[Donation.RECEIVED, Donation.DISTRIBUTED])
        ),
        items_distributed=Sum("quantity", filter=Q(status=Donation.DISTRIBUTED)),
        # Distinct business names that have offered something not-cancelled —
        # "businesses helping". Business offers always carry an organization_name.
        businesses=Count(
            "organization_name",
            filter=Q(donor_type=DONOR_TYPE_BUSINESS) & ~Q(status=Donation.CANCELLED),
            distinct=True,
        ),
    )
    by_status["items_received"] = by_status["items_received"] or 0
    by_status["items_distributed"] = by_status["items_distributed"] or 0
    return by_status
