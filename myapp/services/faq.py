"""Public FAQ data (/faq/): what the page shows and its schema.org FAQPage.

Only active categories with at least one active question are shown — two
queries in all (categories + their active items, prefetched), whatever the
number of rows.
"""

from django.db.models import Count, Prefetch, Q

from myapp.models import FAQ_LANGUAGES, FAQCategory, FAQItem


def public_categories():
    """Active categories (ordered), each with `.active_items` (ordered, active only).
    Categories without an active question are left out."""
    items = FAQItem.objects.filter(is_active=True).order_by("order", "id")
    categories = (
        FAQCategory.objects.filter(is_active=True)
        .annotate(n_active=Count("items", filter=Q(items__is_active=True)))
        .filter(n_active__gt=0)
        .order_by("order", "id")
        .prefetch_related(Prefetch("items", queryset=items, to_attr="active_items"))
    )
    return list(categories)


def page_payload(categories):
    """Each category/question with its text in every UI language (fallback: Russian),
    the shape the template renders."""
    return [
        {
            "id": category.pk,
            "names": [(lang, category.name(lang)) for lang in FAQ_LANGUAGES],
            "items": [
                {
                    "id": item.pk,
                    "questions": [(lang, item.question(lang)) for lang in FAQ_LANGUAGES],
                    "answers": [(lang, item.answer(lang)) for lang in FAQ_LANGUAGES],
                }
                for item in category.active_items
            ],
        }
        for category in categories
    ]


def faq_json_ld(categories):
    """schema.org FAQPage in Russian — the language the server renders for crawlers."""
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "inLanguage": "ru",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item.question("ru"),
                "acceptedAnswer": {"@type": "Answer", "text": item.answer("ru")},
            }
            for category in categories
            for item in category.active_items
        ],
    }
