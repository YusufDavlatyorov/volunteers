"""The public FAQ page (/faq/) — content from the Django admin (FAQCategory / FAQItem)."""

from django.conf import settings
from django.shortcuts import render
from django.views.decorators.http import require_GET

from server.seo import CONTACT_PHONE, _json_ld

from .services import faq


@require_GET
def faq_view(request):
    categories = faq.public_categories()
    bot = (getattr(settings, "TELEGRAM_BOT_USERNAME", "") or "").strip().lstrip("@")
    return render(request, "myapp/faq.html", {
        "categories": faq.page_payload(categories),
        "faq_json_ld": _json_ld(faq.faq_json_ld(categories)) if categories else "",
        "contact_phone": CONTACT_PHONE,
        "contact_phone_href": "+" + "".join(ch for ch in CONTACT_PHONE if ch.isdigit()),
        "telegram_bot": bot,
    })
