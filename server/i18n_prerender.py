"""Server-side pre-render of the client-side i18n.js strings.

UI text is translated in the browser by static/js/i18n.js (`data-i18n` keys,
EN/RU/TJ tables). Templates carry English fallback text, so the raw HTML a
crawler or no-JS visitor receives used to be English. This middleware fills
every plain-text `data-i18n` element (and `data-i18n-ph` placeholder) with the
settings.SERVER_RENDER_LANGUAGE string from that same i18n.js table — one
source of truth, no second copy of the translations. i18n.js still runs after
load and switches to the visitor's chosen language as before.

Only elements whose content is plain text are rewritten; i18n.js replaces
`textContent` anyway, so an element with nested markup is never a translation
target. Unknown keys keep their template text.
"""

import html
import json
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils.html import escape

I18N_JS = Path(settings.BASE_DIR) / "static" / "js" / "i18n.js"

_LANG_OPEN = re.compile(r"^  (\w+): \{\s*$")
_LANG_CLOSE = re.compile(r"^  \},?\s*$")
_PAIR = re.compile(r'"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"')

# <tag … data-i18n="key" …>plain text</tag>
_ELEMENT = re.compile(
    r'(<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*?\sdata-i18n="([^"]+)"[^>]*)>)([^<]*)(</\2\s*>)'
)
_ARGS = re.compile(r'\sdata-i18n-args="([^"]*)"')
# <input|textarea … data-i18n-ph="key" …>
_PH_TAG = re.compile(r'<(?:input|textarea)\b[^>]*\sdata-i18n-ph="([^"]+)"[^>]*>')
_PH_ATTR = re.compile(r'(\splaceholder=")[^"]*(")')
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _parse(source):
    tables, current = {}, None
    for line in source.splitlines():
        opened = _LANG_OPEN.match(line)
        if opened:
            current = tables.setdefault(opened.group(1), {})
            continue
        if current is not None and _LANG_CLOSE.match(line):
            current = None
            continue
        if current is not None and not line.lstrip().startswith("//"):
            for key, value in _PAIR.findall(line):
                current[json.loads(f'"{key}"')] = json.loads(f'"{value}"')
    return tables


@lru_cache(maxsize=4)
def _tables_for(mtime):
    return _parse(I18N_JS.read_text(encoding="utf-8"))


def translation_table(lang):
    """The `lang` table of i18n.js as {key: text}; {} if missing/unreadable."""
    try:
        mtime = I18N_JS.stat().st_mtime
    except OSError:
        return {}
    return _tables_for(mtime).get(lang, {})


def _interpolate(text, args_attr):
    # Mirrors interpolate() in i18n.js: {name} from the data-i18n-args JSON.
    if not args_attr:
        return text
    try:
        args = json.loads(html.unescape(args_attr))
    except ValueError:
        return text
    if not isinstance(args, dict):
        return text
    return _PLACEHOLDER.sub(lambda m: str(args[m.group(1)]) if m.group(1) in args else m.group(0), text)


def render_html(document, lang):
    table = translation_table(lang)
    if not table:
        return document

    def element(match):
        open_tag, _tag, attrs, key, _text, close_tag = match.groups()
        if key not in table:
            return match.group(0)
        args = _ARGS.search(attrs)
        text = _interpolate(table[key], args.group(1) if args else None)
        return f"{open_tag}{escape(text)}{close_tag}"

    def placeholder(match):
        tag, key = match.group(0), match.group(1)
        if key not in table or not _PH_ATTR.search(tag):
            return tag
        return _PH_ATTR.sub(lambda m: f"{m.group(1)}{escape(table[key])}{m.group(2)}", tag, count=1)

    document = _ELEMENT.sub(element, document)
    return _PH_TAG.sub(placeholder, document)


class ServerSideI18nMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if (
            getattr(response, "streaming", False)
            or "text/html" not in response.get("Content-Type", "")
            or b"data-i18n" not in response.content
        ):
            return response
        charset = response.charset or "utf-8"
        rendered = render_html(response.content.decode(charset), settings.SERVER_RENDER_LANGUAGE)
        response.content = rendered.encode(charset)
        if response.has_header("Content-Length"):
            response["Content-Length"] = str(len(response.content))
        return response
