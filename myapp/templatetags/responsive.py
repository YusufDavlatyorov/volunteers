"""`{% responsive_image %}` — WebP variants without changing the layout.

    {% load responsive %}
    {% responsive_image report.image report.image_variants sizes="(max-width: 700px) 92vw, 380px" class="report-img" alt=report.title width=800 height=600 loading="lazy" %}
    {% responsive_image profile.image profile.image_variants variant="thumb" class="avatar" alt="" %}

Renders ``<picture><source type="image/webp" srcset=…><img src=original …></picture>``.
The ``<img>`` keeps exactly the attributes (class, width/height, alt) the
template had before, and ``picture { display: contents }`` (style.css) keeps
the box where it was. Browsers without WebP (old iPhones on iOS 12, still
common among elderly clients) take the original. No variants yet (Celery
still working, or a broken source) → just the original ``<img>``.

``variant="thumb"`` serves one fixed variant (avatars: ≤ 96 px on screen);
otherwise every variant goes into ``srcset`` with its nominal width and the
browser picks by ``sizes``.
"""

from django import template
from django.forms.utils import flatatt
from django.utils.html import format_html

from myapp.services.images import DEFAULT_SIZES

register = template.Library()


@register.simple_tag
def responsive_image(field, variants=None, sizes="100vw", variant="", **attrs):
    if not field:
        return ""
    try:
        original = field.url
    except ValueError:
        return ""
    attrs = {key.replace("_", "-"): value for key, value in attrs.items() if value is not None and value != ""}
    img = format_html("<img src=\"{}\"{}>", original, flatatt(attrs))
    variants = variants or {}
    storage = field.storage
    if variant:
        path = variants.get(variant)
        if not path:
            return img
        return format_html('<picture><source type="image/webp" srcset="{}">{}</picture>', storage.url(path), img)
    candidates = sorted((px, variants[name]) for name, px in DEFAULT_SIZES.items() if variants.get(name))
    if not candidates:
        return img
    srcset = ", ".join(f"{storage.url(path)} {px}w" for px, path in candidates)
    return format_html('<picture><source type="image/webp" srcset="{}" sizes="{}">{}</picture>', srcset, sizes, img)
