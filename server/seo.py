"""Search-engine metadata for khayrkhoh.tj.

PUBLIC_PAGES is the single list of indexable pages: their Russian <title> /
meta description, canonical URL and sitemap entry. Every other page (the
whole CRM under /myapp/, the dashboard, token links, the Django admin, error
pages) renders `<meta name="robots" content="noindex, nofollow">` via
`seo_context`. robots.txt and sitemap.xml are derived from the same list.
"""

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.http import HttpResponse
from django.templatetags.static import static
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.views.decorators.http import require_GET

SITE_NAME = "KhayrKhoh"
ALTERNATE_NAMES = ["Khayr Khoh", "Хайрхох", "Хайрхоҳ", "khayrkhoh.tj"]
# Shown in the footer (base.html) — keep the two in sync.
CONTACT_PHONE = "+992 071 81 77 22"
ORG_DESCRIPTION = (
    "KhayrKhoh (Хайрхох) — волонтёрская платформа Таджикистана: пожилые люди "
    "оставляют заявки на помощь, волонтёры рядом откликаются, а кураторы "
    "координируют работу в пяти регионах страны."
)
OG_IMAGE = "images/brand/og-image.png"  # 1200x630
LOGO = "images/brand/logo-512.png"  # 512x512

HOME_TITLE = "KhayrKhoh — волонтёры помогают пожилым людям по всему Таджикистану"

# (url namespace "", url_name) -> page metadata. `canonical` is the url_name
# whose URL is canonical. Every page with a `sitemap` entry must be its own
# canonical (a sitemap may only list canonical URLs) — test-locked.
PUBLIC_PAGES = {
    "home": {
        "title": HOME_TITLE,
        "description": ORG_DESCRIPTION,
        "canonical": "home",
        "sitemap": {"priority": 1.0, "changefreq": "weekly"},
        "structured_data": True,
    },
    "about": {
        "title": "KhayrKhoh — о волонтёрской платформе помощи пожилым людям в Таджикистане",
        "description": (
            "О платформе KhayrKhoh (Хайрхох): как волонтёры, кураторы и пожилые "
            "люди в Душанбе, Согде, Хатлоне, ГБАО и РРП находят друг друга."
        ),
        # Same content as the home page, so "/" is the single canonical URL
        # (no duplicate); About stays reachable and robots-allowed, just not
        # in the sitemap.
        "canonical": "home",
        "sitemap": None,
        "structured_data": True,
    },
    "rating": {
        "title": "Рейтинг волонтёров — KhayrKhoh",
        "description": (
            "Рейтинг волонтёров KhayrKhoh: самые активные добровольцы, "
            "помогающие пожилым людям в регионах Таджикистана."
        ),
        "canonical": "rating",
        "sitemap": {"priority": 0.6, "changefreq": "daily"},
    },
    "register": {
        "title": "Стать волонтёром или попросить помощь — KhayrKhoh",
        "description": (
            "Регистрация на KhayrKhoh: подайте заявку волонтёра или попросите "
            "помощи для пожилого человека в любом регионе Таджикистана."
        ),
        "canonical": "register",
        "sitemap": {"priority": 0.8, "changefreq": "monthly"},
    },
    "login": {
        "title": "Вход в личный кабинет — KhayrKhoh",
        "description": (
            "Вход в личный кабинет KhayrKhoh для волонтёров, кураторов и "
            "пожилых людей, получающих помощь."
        ),
        "canonical": "login",
        "sitemap": {"priority": 0.4, "changefreq": "yearly"},
    },
}

# Private prefixes outside /myapp/. The /myapp/ CRM prefixes are derived from
# myapp.urls (private_myapp_prefixes), so a new login-only page is disallowed
# without touching this file; /myapp/ itself is NOT blocked wholesale, because
# public pages (about, rating) live under it.
ROBOTS_DISALLOW = [
    "/admin/",
    "/profile/",
    "/update-profile/",
    "/forgot-password/",
    "/reset-password/",
    "/confirm-email/",
    "/logout/",
]


def absolute(path):
    return f"{settings.SITE_URL}{path}"


def _json_ld(data):
    # Same escaping as Django's json_script: "<", ">" and "&" can't end the
    # <script> element or start markup.
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return mark_safe(text)


def organization_json_ld():
    home = absolute(reverse("home"))
    org_id = f"{home}#organization"
    return {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "NGO",
                "@id": org_id,
                "name": SITE_NAME,
                "alternateName": ALTERNATE_NAMES,
                "url": home,
                "logo": {
                    "@type": "ImageObject",
                    "url": absolute(static(LOGO)),
                    "width": 512,
                    "height": 512,
                },
                "image": absolute(static(OG_IMAGE)),
                "description": ORG_DESCRIPTION,
                "areaServed": "Tajikistan",
                "contactPoint": {
                    "@type": "ContactPoint",
                    "telephone": CONTACT_PHONE,
                    "contactType": "customer support",
                    "areaServed": "TJ",
                    "availableLanguage": ["ru", "tg", "en"],
                },
            },
            {
                "@type": "WebSite",
                "@id": f"{home}#website",
                "name": SITE_NAME,
                "alternateName": ALTERNATE_NAMES,
                "url": home,
                "inLanguage": "ru",
                "publisher": {"@id": org_id},
            },
        ],
    }


def public_page_name(request):
    """The PUBLIC_PAGES key for this request, or None (→ noindex)."""
    match = getattr(request, "resolver_match", None)
    if match is None or match.namespace:  # e.g. admin:login is not our login
        return None
    return match.url_name if match.url_name in PUBLIC_PAGES else None


def seo_context(request):
    name = public_page_name(request)
    context = {
        "site_name": SITE_NAME,
        "site_url": settings.SITE_URL,
        "og_image": absolute(static(OG_IMAGE)),
        "noindex": name is None,
    }
    if name is not None:
        page = PUBLIC_PAGES[name]
        context.update(
            title=page["title"],
            description=page["description"],
            canonical=absolute(reverse(page["canonical"])),
            json_ld=_json_ld(organization_json_ld()) if page.get("structured_data") else "",
        )
    return {"seo": context}


def sitemap_names():
    return [name for name, page in PUBLIC_PAGES.items() if page["sitemap"]]


def private_myapp_prefixes():
    """`/myapp/<segment>/` for every myapp route whose first path segment
    isn't a public page's — i.e. the whole login-only CRM."""
    from myapp import urls as myapp_urls

    mount = reverse("rating").rsplit("rating/", 1)[0]  # "/myapp/"
    public = {
        reverse(name)[len(mount):].split("/", 1)[0]
        for name in PUBLIC_PAGES
        if reverse(name).startswith(mount)
    }
    segments = {str(pattern.pattern).split("/", 1)[0] for pattern in myapp_urls.urlpatterns}
    return [f"{mount}{segment}/" for segment in sorted(segments - public - {""})]


@require_GET
def robots_txt(request):
    # Explicit Allow for every public page first (incl. non-sitemap ones like
    # About, which must stay crawlable for its canonical to be seen), then the
    # private prefixes.
    allow = [reverse(name) for name in PUBLIC_PAGES if reverse(name) != "/"]
    lines = ["User-agent: *"]
    lines += [f"Allow: {path}" for path in allow]
    lines += [f"Disallow: {path}" for path in ROBOTS_DISALLOW + private_myapp_prefixes()]
    lines += ["", f"Sitemap: {absolute(reverse('sitemap'))}", ""]
    return HttpResponse("\n".join(lines), content_type="text/plain; charset=utf-8")


class PublicPagesSitemap(Sitemap):
    """Only PUBLIC_PAGES with a sitemap entry, as absolute SITE_URL URLs."""

    def items(self):
        return sitemap_names()

    def location(self, name):
        return reverse(name)

    def get_protocol(self, protocol=None):
        return urlsplit(settings.SITE_URL).scheme

    def get_domain(self, site=None):
        return urlsplit(settings.SITE_URL).netloc

    def priority(self, name):
        return PUBLIC_PAGES[name]["sitemap"]["priority"]

    def changefreq(self, name):
        return PUBLIC_PAGES[name]["sitemap"]["changefreq"]

    def lastmod(self, name):
        # The home page shows the latest photo reports; the other public pages
        # have no meaningful modification date.
        if name == "home":
            from myapp.models import PhotoReport

            latest = PhotoReport.objects.order_by("-created_at").values_list("created_at", flat=True).first()
            return latest
        return None


SITEMAPS = {"pages": PublicPagesSitemap}
