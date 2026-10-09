"""Server-side pre-render of the client-side i18n.js strings.

UI text is translated in the browser by static/js/i18n.js (`data-i18n` keys,
EN/RU/TJ tables). Templates carry English fallback text, so the raw HTML a
crawler or no-JS visitor receives used to be English. This middleware fills
every plain-text `data-i18n` element (and `data-i18n-ph` placeholder) with the
settings.SERVER_RENDER_LANGUAGE string from that same i18n.js table — one
source of truth, no second copy of the translations. i18n.js still runs after
load and switches to the visitor's chosen language as before. Attribute keys
are filled the same way: data-i18n-ph → placeholder, data-i18n-title → title,
data-i18n-alt → alt, data-i18n-aria → aria-label (on any tag).

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
# Double- or single-quoted (templates use both: data-i18n-args='{"date": …}').
_ARGS = re.compile(r"""\sdata-i18n-args=(?:"([^"]*)"|'([^']*)')""")
# <tag … data-i18n-ph|title|alt|aria="key" …> → the matching attribute.
_ATTR_KEYS = {"ph": "placeholder", "title": "title", "alt": "alt", "aria": "aria-label"}
_ATTR_TAG = re.compile(r'<[a-zA-Z][a-zA-Z0-9]*\b[^>]*\sdata-i18n-(?:ph|title|alt|aria)="[^"]+"[^>]*>')
_ATTR_KEY = re.compile(r'\sdata-i18n-(ph|title|alt|aria)="([^"]+)"')
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
# Inline <script>/<style> bodies are code, not markup: a data-i18n element inside
# a JS template string must keep its expression (it was being replaced with the
# Russian text, hardcoding one language into the script).
_RAW_BLOCK = re.compile(r"(<(script|style)\b[^>]*>.*?</\2\s*>)", re.S | re.I)


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
        text = _interpolate(table[key], (args.group(1) if args.group(1) is not None else args.group(2)) if args else None)
        return f"{open_tag}{escape(text)}{close_tag}"

    def attributes(match):
        tag = match.group(0)
        args = _ARGS.search(tag)
        args = (args.group(1) if args.group(1) is not None else args.group(2)) if args else None
        for kind, key in _ATTR_KEY.findall(tag):
            if key not in table:
                continue
            target = re.compile(rf'(\s{_ATTR_KEYS[kind]}=")[^"]*(")')
            text = escape(_interpolate(table[key], args))
            tag = target.sub(lambda m: f"{m.group(1)}{text}{m.group(2)}", tag, count=1)
        return tag

    def markup(chunk):
        return _ATTR_TAG.sub(attributes, _ELEMENT.sub(element, chunk))

    # Even indexes are markup, odd ones the raw <script>/<style> blocks (kept as is).
    parts = _RAW_BLOCK.split(document)
    return "".join(
        markup(part) if i % 3 == 0 else (part if i % 3 == 1 else "")
        for i, part in enumerate(parts)
    )


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
