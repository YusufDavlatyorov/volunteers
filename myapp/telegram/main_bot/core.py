"""Main Telegram bot — transport-agnostic core (Feature Pack v2, feature 4).

``handle(event) -> Result`` turns one incoming Telegram event (command, text,
button press, location, contact, photo) into replies. Plain Python + Django:
the python-telegram-bot adapter (``app.py``) calls it via ``sync_to_async``
and sends the replies; tests call it directly — no Telegram involved.

Rules:

* **Linked accounts only** (``Users.telegram_id`` — set by the verified
  ``/link <code>`` round trip, ``services/telegram_link.py``, unchanged;
  ``/start <code>`` is the same thing through a deep link).
* **Every action calls the website's services** — task_actions (accept /
  complete / assign / applications / broadcast / create request), ai.actions
  (work stage), emergency, cancellation, chat, feedback, tracking, donations,
  matching, overdue / stale, notify. No business rule lives in the bot; the
  per-object checks are the same ``services/access.py`` functions.
* **Idempotent buttons**: every mutating button carries a one-time nonce
  (``cache.add`` — a double tap or a Telegram retry runs once) and the
  services keep their own conditional UPDATEs anyway.
* **Per-chat rate limit** (``RATE_LIMIT`` events / ``RATE_WINDOW`` s).
* Language = ``Profile.language`` (``/lang`` switches it). Logs are ID-only
  (internal user ids, never chat ids, text, phones or tokens).
* Wizards (new request, chat message, review comment, broadcast) keep their
  state in the cache for ``STATE_TTL`` (30 min).
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from accounts.models import REGION_CHOICES, Profile, Users

from ...forms import HelpRequestForm
from ...models import (
    HELP_TYPE_CHOICES,
    PRIORITY_CHOICES,
    REVIEW_TAGS,
    WORK_STAGE_ORDER,
    Donation,
    EmergencyReport,
    HelpRequest,
    Notification,
    VolunteerApplication,
)
from ...services import (
    access,
    achievements,
    cancellation,
    chat,
    community,
    donations,
    emergency,
    feedback,
    matching,
    notify_texts,
    overdue,
    presence,
    routing,
    stale,
    task_actions,
    tracking,
)
from ...services.ai import actions as ai_actions
from ...services.geo import haversine_km, is_valid_coordinate
from ...services.telegram_link import redeem_link_code
from ...validators import normalize_tj_phone
from ..i18n import t

logger = logging.getLogger(__name__)

PAGE = 5
STATE_TTL = 30 * 60
NONCE_TTL = 10 * 60
RATE_LIMIT = 30
RATE_WINDOW = 60
DESC_PREVIEW = 400
LANGS = ("ru", "tj", "en")


# --------------------------------------------------------------------------- #
# value types
# --------------------------------------------------------------------------- #

@dataclass
class Button:
    label: str
    data: str = ""
    url: str = ""


@dataclass
class KbButton:
    """A reply-keyboard button: ``request`` = "location" | "contact" | "" (text)."""
    label: str
    request: str = ""


@dataclass
class Reply:
    """One message. Telegram allows one markup per message, so a reply carries
    either inline ``buttons`` or a reply ``keyboard`` (location / contact
    requests only exist there) — never both — or ``remove_keyboard``."""
    text: str
    buttons: list = field(default_factory=list)        # rows of Button (inline)
    keyboard: list = field(default_factory=list)       # rows of KbButton (reply keyboard)
    remove_keyboard: bool = False
    edit: bool = False                                   # edit the message the button was on

    @property
    def request_location(self):
        return any(b.request == "location" for row in self.keyboard for b in row)

    @property
    def request_contact(self):
        return any(b.request == "contact" for row in self.keyboard for b in row)


@dataclass
class Event:
    chat_id: int
    kind: str                     # command | text | callback | location | contact | photo
    text: str = ""
    data: str = ""
    lat: float | None = None
    lng: float | None = None
    accuracy: float | None = None
    live: bool = False
    photo: bytes | None = None
    photo_too_large: bool = False
    contact_phone: str = ""
    contact_user_id: int | None = None
    is_private: bool = True


@dataclass
class Result:
    replies: list = field(default_factory=list)
    toast: str = ""               # short popup for a button press
    previous_chat_notice: int | None = None   # /link moved the account: tell the old chat


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _nonce():
    return secrets.token_hex(3)


def _claim(chat_id, nonce) -> bool:
    """One-time use of a mutating button."""
    if not nonce:
        return False
    return cache.add(f"tg:nonce:{chat_id}:{nonce}", 1, NONCE_TTL)


def _rate_ok(chat_id) -> bool:
    window = int(timezone.now().timestamp() // RATE_WINDOW)
    key = f"tg:rl:{chat_id}:{window}"
    try:
        cache.add(key, 0, RATE_WINDOW + 1)
        count = cache.incr(key)
    except Exception:  # noqa: BLE001 — a cache outage must not lock users out
        return True
    if count == RATE_LIMIT + 1:
        return None  # first refused event → tell them once
    return count <= RATE_LIMIT


def _state_key(chat_id):
    return f"tg:state:{chat_id}"


def get_state(chat_id):
    return cache.get(_state_key(chat_id))


def set_state(chat_id, flow, step, **data):
    cache.set(_state_key(chat_id), {"flow": flow, "step": step, "data": data}, STATE_TTL)


def clear_state(chat_id):
    cache.delete(_state_key(chat_id))


def _lang(user):
    try:
        lang = user.profile.language
    except Profile.DoesNotExist:
        lang = "ru"
    return lang if lang in LANGS else "ru"


def _label(table, code, lang):
    return notify_texts.LABELS[table].get(lang, {}).get(code) or notify_texts.LABELS[table]["ru"].get(code, code)


def _help_type(code, lang):
    return _label("help_type", code, lang)


def _region(code, lang):
    return _label("region", code, lang) if code else "—"


def _status(code, lang):
    return _label("task_status", code, lang)


def _priority(code, lang):
    return t(lang, f"priority.{code}")


def _stage(code, lang):
    return t(lang, f"stage.{code}")


def _ago(moment, lang):
    if not moment:
        return "—"
    minutes = int((timezone.now() - moment).total_seconds() // 60)
    if minutes < 60:
        return t(lang, "ago.min", n=max(minutes, 0))
    if minutes < 48 * 60:
        return t(lang, "ago.h", n=minutes // 60)
    return t(lang, "ago.d", n=minutes // 1440)


def _name(user):
    try:
        full_name = user.profile.full_name
    except Profile.DoesNotExist:
        full_name = ""
    return full_name or user.username


def _site(path):
    return settings.SITE_URL.rstrip("/") + path


def _kb(lang, request):
    """Reply keyboard: one location / contact request button + a text "Cancel"
    (inline buttons can't share the message)."""
    return [[KbButton(t(lang, f"kb.{request}"), request)], [KbButton(t(lang, "kb.cancel"))]]


CANCEL_LABELS = {"/cancel"}


def _is_cancel_text(text):
    return (text or "").strip() in CANCEL_LABELS or (text or "").strip() in {t(code, "kb.cancel") for code in LANGS}


def _back(lang, data="m"):
    return [Button(t(lang, "btn.back"), data)]


def _menu_row(lang):
    return [Button(t(lang, "btn.menu"), "m")]


def _pager(lang, action, page, total):
    row = []
    if page > 0:
        row.append(Button("‹ " + t(lang, "btn.prev"), f"{action}|{page - 1}"))
    if (page + 1) * PAGE < total:
        row.append(Button(t(lang, "btn.next") + " ›", f"{action}|{page + 1}"))
    return row


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _task(task_id):
    return HelpRequest.objects.select_related("client__profile", "volunteer__profile").filter(pk=_int(task_id)).first()


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def handle(event: Event) -> Result:
    allowed = _rate_ok(event.chat_id)
    if allowed is None:
        return Result([Reply(t("ru", "rate_limited"))])
    if not allowed:
        return Result()

    command, _, arg = (event.text or "").strip().partition(" ") if event.kind == "command" else ("", "", "")
    command = command.split("@", 1)[0].lower()

    if not event.is_private:
        # Groups: only /id (the main bot is a private assistant).
        if command == "/id":
            return Result([Reply(t("ru", "chat_id", chat_id=event.chat_id))])
        return Result()

    if command == "/id":
        return Result([Reply(t("ru", "chat_id", chat_id=event.chat_id))])
    if command == "/link" or (command == "/start" and arg.strip()):
        return _link(event, arg.strip())

    user = Users.objects.select_related("profile").filter(telegram_id=event.chat_id, is_active=True).first()
    if user is None:
        return Result([Reply(t("ru", "unlinked", bot_site=_site(reverse("telegram_link"))))])
    presence.mark_seen(user, source="telegram")
    lang = _lang(user)

    if command in ("/start", "/menu", "/help"):
        clear_state(event.chat_id)
        return Result([main_menu(user, lang)])
    if command == "/cancel" or (event.kind == "text" and _is_cancel_text(event.text)):
        clear_state(event.chat_id)
        return Result([Reply(t(lang, "cancelled"), remove_keyboard=True), main_menu(user, lang)])
    if command == "/lang":
        return Result([_lang_menu(lang)])
    if command.startswith("/chat_"):
        return Result(_chat_start(user, lang, event.chat_id, command[len("/chat_"):]))
    if event.kind == "command":
        return Result([Reply(t(lang, "unknown_command")), main_menu(user, lang)])

    if event.kind == "callback":
        return _callback(user, lang, event)
    if event.kind == "location":
        return Result(_location(user, lang, event))
    state = get_state(event.chat_id)
    if state:
        return Result(_continue_flow(user, lang, event, state))
    if event.kind == "photo":
        return Result([Reply(t(lang, "photo_where"))])
    return Result([main_menu(user, lang)])


def _link(event, code):
    if not code:
        return Result([Reply(t("ru", "link_usage", bot_site=_site(reverse("telegram_link"))))])
    outcome = redeem_link_code(event.chat_id, code)
    result = Result([Reply(outcome.message)])
    if outcome.ok:
        if outcome.previous_chat_id:
            result.previous_chat_notice = outcome.previous_chat_id
        user = Users.objects.select_related("profile").filter(telegram_id=event.chat_id, is_active=True).first()
        if user is not None:
            result.replies.append(main_menu(user, _lang(user)))
    return result


# --------------------------------------------------------------------------- #
# menus
# --------------------------------------------------------------------------- #

def main_menu(user, lang) -> Reply:
    rows = []
    if access.is_staff_user(user):
        rows += [
            [Button(t(lang, "menu.overdue"), "so|0"), Button(t(lang, "menu.stale"), "ss|0")],
            [Button(t(lang, "menu.unassigned"), "su|0"), Button(t(lang, "menu.sos"), "se|0")],
        ]
        row = [Button(t(lang, "menu.donations"), "sdo|0")]
        if user.is_superuser:
            row.insert(0, Button(t(lang, "menu.applications"), "sap|0"))
        rows.append(row)
        rows.append([Button(t(lang, "menu.broadcast"), "sb")])
        role = "staff"
    elif user.is_volunteer:
        rows += [
            [Button(t(lang, "menu.tasks"), "vt|0"), Button(t(lang, "menu.my_task"), "my")],
            [Button(t(lang, "menu.badges"), "ach")],
        ]
        role = "volunteer"
    elif user.is_client:
        rows += [[Button(t(lang, "menu.new_request"), "cn"), Button(t(lang, "menu.my_requests"), "cl|0")]]
        role = "client"
    else:
        role = "guest"
    rows.append([Button(t(lang, "menu.notifications"), "ntf"), Button(t(lang, "menu.language"), "lang")])
    if community.configured() and community.is_eligible(user):
        rows.append([Button(t(lang, "menu.community"), "com")])
    rows.append([Button(t(lang, "menu.open_site"), url=_site(reverse("profile")))])
    return Reply(t(lang, f"menu.title.{role}", name=_name(user)), rows)


def _lang_menu(lang):
    return Reply(t(lang, "lang.choose"), [[Button("Русский", "lang|ru"), Button("Тоҷикӣ", "lang|tj"),
                                           Button("English", "lang|en")], _menu_row(lang)])


# --------------------------------------------------------------------------- #
# callbacks
# --------------------------------------------------------------------------- #

def _callback(user, lang, event) -> Result:
    parts = (event.data or "").split("|")
    action, args = parts[0], parts[1:]
    nonce = args[-1] if args and args[-1].startswith("~") else ""
    if nonce:
        args = args[:-1]
        if not _claim(event.chat_id, nonce[1:]):
            return Result(toast=t(lang, "already_done"))

    handler = CALLBACKS.get(action)
    if handler is None:
        return Result([main_menu(user, lang)])
    roles = handler.roles
    if roles and not _has_role(user, roles):
        return Result(toast=t(lang, "forbidden"))
    out = handler.fn(user, lang, event, *args)
    if isinstance(out, Result):
        return out
    return Result(out if isinstance(out, list) else [out])


@dataclass(frozen=True)
class _Cb:
    fn: object
    roles: tuple = ()


def _has_role(user, roles):
    if "staff" in roles and access.is_staff_user(user):
        return True
    if "admin" in roles and user.is_superuser:
        return True
    if "volunteer" in roles and user.is_volunteer:
        return True
    if "client" in roles and user.is_client:
        return True
    return False


def _m(user, lang, event, *args):
    clear_state(event.chat_id)
    return main_menu(user, lang)


def _set_lang(user, lang, event, code=""):
    if code not in LANGS:
        return _lang_menu(lang)
    Profile.objects.filter(user=user).update(language=code)
    user.profile.language = code
    return [Reply(t(code, "lang.saved")), main_menu(user, code)]


def _lang_cb(user, lang, event, *args):
    return _lang_menu(lang)


# --- notifications ----------------------------------------------------------

def _notifications(user, lang, event, *args):
    rows = list(Notification.objects.filter(recipient=user, read_at__isnull=True).order_by("-created_at")[:5])
    if not rows:
        return Reply(t(lang, "ntf.none"), [_menu_row(lang)])
    lines = [t(lang, "ntf.title")]
    for n in rows:
        title, body = notify_texts.render_inapp(n.title_key, n.body_key, n.params, lang)
        lines.append(f"• {title}\n  {body}")
    return Reply("\n".join(lines), [[Button(t(lang, "ntf.mark_read"), f"ntfr|~{_nonce()}")], _menu_row(lang)])


def _notifications_read(user, lang, event, *args):
    Notification.objects.filter(recipient=user, read_at__isnull=True).update(read_at=timezone.now())
    return Result([Reply(t(lang, "ntf.done"), [_menu_row(lang)], edit=True)], toast=t(lang, "ntf.done"))


# --- community --------------------------------------------------------------

def _community(user, lang, event, *args):
    outcome = community.create_invites(user)
    if not outcome.ok:
        return Reply(t(lang, f"com.{outcome.code}"), [_menu_row(lang)])
    rows = [[Button(t(lang, f"com.chat.{key}" if key == community.GENERAL else "com.chat.region",
                      region=_region(key, lang)), url=url)] for key, url in outcome.links]
    rows.append(_menu_row(lang))
    return Reply(t(lang, "com.links", hours=getattr(settings, "COMMUNITY_INVITE_HOURS", 24)), rows)


# --------------------------------------------------------------------------- #
# volunteer
# --------------------------------------------------------------------------- #

def _visible_pending(user):
    qs = HelpRequest.objects.filter(status="pending").select_related("client")
    if user.region:
        qs = qs.filter(region__in=[user.region, ""])
    return qs.order_by("-is_urgent", "created_at", "id")


def _distance_text(user, task, lang):
    profile = getattr(user, "profile", None)
    if profile is None or not profile.has_location or not task.has_location:
        return ""
    km = haversine_km(float(profile.latitude), float(profile.longitude), float(task.latitude), float(task.longitude))
    return t(lang, "km", km=f"{km:.1f}")


def _v_tasks(user, lang, event, page="0"):
    page = max(_int(page) or 0, 0)
    qs = _visible_pending(user)
    total = qs.count()
    tasks = list(qs[page * PAGE:(page + 1) * PAGE])
    if not tasks:
        return Reply(t(lang, "v.no_tasks"), [_menu_row(lang)], edit=event.kind == "callback")
    rows = [[Button(f"#{x.pk} · {_help_type(x.help_type, lang)} · {_region(x.region, lang)}"
                    + (" ⚠️" if x.priority != "normal" else ""), f"vd|{x.pk}")] for x in tasks]
    pager = _pager(lang, "vt", page, total)
    if pager:
        rows.append(pager)
    rows.append(_menu_row(lang))
    return Reply(t(lang, "v.tasks_title", total=total), rows, edit=event.kind == "callback")


def _task_card(task, lang, *, show_contacts):
    lines = [
        f"#{task.pk} · {_help_type(task.help_type, lang)}",
        f"{t(lang, 'f.status')}: {_status(task.status, lang)}"
        + (f" · {_stage(task.work_stage, lang)}" if task.status == "active" else ""),
        f"{t(lang, 'f.priority')}: {_priority(task.priority, lang)}",
        f"{t(lang, 'f.region')}: {_region(task.region, lang)}",
        f"{t(lang, 'f.created')}: {_ago(task.created_at, lang)}",
        f"{t(lang, 'f.address')}: {task.address}",
    ]
    if show_contacts:
        lines.append(f"{t(lang, 'f.phone')}: {task.phone}")
    description = task.description if len(task.description) <= DESC_PREVIEW else task.description[:DESC_PREVIEW] + "…"
    lines += ["", description]
    return "\n".join(lines)


def _v_detail(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None or not access.can_view_task(user, task):
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    if task.volunteer_id == user.pk and task.status == "active":
        return _v_my(user, lang, event)
    text = _task_card(task, lang, show_contacts=False)
    distance = _distance_text(user, task, lang)
    if distance:
        text += f"\n\n{distance}"
    rows = []
    if task.status == "pending":
        rows.append([Button(t(lang, "v.accept"), f"va|{task.pk}|~{_nonce()}")])
    rows.append(_back(lang, "vt|0"))
    return Reply(text, rows)


def _v_accept(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None or not access.can_view_task(user, task):
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    outcome = task_actions.accept(user, task)
    if not outcome.ok:
        return Reply(t(lang, f"v.accept.{outcome.code}"), [_back(lang, "vt|0")])
    return [Reply(t(lang, "v.accepted")), _v_my(user, lang, event)]


def _active_task(user):
    return (HelpRequest.objects.select_related("client__profile").filter(volunteer=user, status="active")
            .order_by("-accepted_at").first())


def _v_my(user, lang, event, *args):
    task = _active_task(user)
    if task is None:
        return Reply(t(lang, "v.no_active"), [[Button(t(lang, "menu.tasks"), "vt|0")], _menu_row(lang)])
    text = _task_card(task, lang, show_contacts=True)
    rows = []
    order = list(WORK_STAGE_ORDER)
    idx = order.index(task.work_stage) if task.work_stage in order else 0
    if idx + 1 < len(order):
        nxt = order[idx + 1]
        rows.append([Button(t(lang, f"v.stage_btn.{nxt}"), f"vs|{task.pk}|{nxt}|~{_nonce()}")])
    rows.append([Button(t(lang, "v.complete"), f"vc|{task.pk}"), Button(t(lang, "btn.chat"), f"ch|{task.pk}")])
    rows.append([Button(t(lang, "v.route"), f"rt|{task.pk}"), Button(t(lang, "v.share_location"), "loc")])
    rows.append([Button("🆘 " + t(lang, "v.sos"), f"sos|{task.pk}")])
    rows.append(_menu_row(lang))
    return Reply(text, rows)


def _v_stage(user, lang, event, task_id="", target=""):
    task = _task(task_id)
    if task is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    if not (task.volunteer_id == user.pk or access.is_staff_user(user)):
        return Result(toast=t(lang, "forbidden"))
    try:
        ai_actions.advance_work_stage_for(user, task, target)
    except ValueError:
        return Result(toast=t(lang, "v.stage_failed"))
    return Result([_v_my(user, lang, event)], toast=t(lang, "v.stage_saved", stage=_stage(target, lang)))


def _v_complete_confirm(user, lang, event, task_id=""):
    return Reply(t(lang, "v.complete_confirm", task_id=task_id),
                 [[Button(t(lang, "btn.yes"), f"vcy|{task_id}|~{_nonce()}"), Button(t(lang, "btn.no"), "my")]])


def _v_complete(user, lang, event, task_id=""):
    task = HelpRequest.objects.filter(pk=_int(task_id), volunteer=user).first()
    if task is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    outcome = task_actions.complete(user, task)
    if not outcome.ok:
        return Reply(t(lang, "v.complete_already"), [_menu_row(lang)])
    return [Reply(t(lang, "v.completed")), main_menu(user, lang)]


def _v_sos_confirm(user, lang, event, task_id=""):
    return Reply(t(lang, "v.sos_confirm"),
                 [[Button("🆘 " + t(lang, "v.sos_send"), f"sosy|{task_id}|~{_nonce()}")], [Button(t(lang, "btn.no"), "my")]])


def _v_sos(user, lang, event, task_id=""):
    task = HelpRequest.objects.filter(pk=_int(task_id), volunteer=user, status="active").first()
    if task is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    report, created = emergency.report_emergency(volunteer=user, help_request=task, reason=t(lang, "v.sos_reason"))
    return Reply(t(lang, "v.sos_sent" if created else "v.sos_exists"), [_menu_row(lang)])


def _v_route(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None or not access.can_view_route(user, task) or not task.has_location:
        return Reply(t(lang, "v.no_route"), [_back(lang, "my")])
    links = routing.deep_links((float(task.latitude), float(task.longitude)))
    # Inline URL buttons accept only http(s) / tg: links — the yandexnavi://
    # app scheme would make Telegram reject the whole message.
    rows = [[Button(name, url=url)] for name, url in (
        ("Google Maps", links.get("google")), ("Yandex Maps", links.get("yandex_maps")),
        ("2GIS", links.get("dgis"))) if url and url.startswith("https://")]
    rows.append(_back(lang, "my"))
    return Reply(t(lang, "v.route_title", address=task.address), rows)


def _v_location_help(user, lang, event, *args):
    if not tracking.has_consent(user):
        return Reply(t(lang, "loc.consent"), [[Button(t(lang, "loc.agree"), f"lcons|~{_nonce()}")], _back(lang, "my")])
    return Reply(t(lang, "loc.how"), keyboard=_kb(lang, "location"))


def _v_consent(user, lang, event, *args):
    tracking.give_consent(user)
    return Reply(t(lang, "loc.how"), keyboard=_kb(lang, "location"))


def _location(user, lang, event):
    state = get_state(event.chat_id)
    if state and state.get("flow") == "new_request":
        return _continue_flow(user, lang, event, state)
    if not user.is_volunteer:
        return [Reply(t(lang, "loc.not_needed"))]
    task = _active_task(user)
    if task is None:
        return [Reply(t(lang, "loc.no_task"))]
    if not tracking.has_consent(user):
        return [Reply(t(lang, "loc.consent"), [[Button(t(lang, "loc.agree"), f"lcons|~{_nonce()}")]])]
    outcome = tracking.accept_point(user, task.pk, event.lat, event.lng, accuracy=event.accuracy, source="telegram")
    if event.live:
        return []  # live location updates are silent; the first one gets a confirmation below
    if outcome.ok:
        return [Reply(t(lang, "loc.saved"), remove_keyboard=True)]
    return [Reply(t(lang, f"loc.{outcome.code}") if outcome.code in ("inaccurate", "jump", "invalid")
                  else t(lang, "loc.failed"), remove_keyboard=True)]


def _achievements(user, lang, event, *args):
    items = achievements.progress_for(user)
    titles = notify_texts.LABELS["achievement"][lang]
    summary = feedback.public_summary(user)
    lines = [t(lang, "ach.title")]
    if summary["count"]:
        lines.append(t(lang, "ach.rating", avg=f"{summary['avg']:.1f}", count=summary["count"]))
    lines.append(t(lang, "ach.points", points=getattr(user.profile, "rating", 0)))
    earned = [i for i in items if i["earned"]]
    pending = [i for i in items if not i["earned"] and not i["manual"] and not i["coming_soon"]]
    if earned:
        lines.append("")
        lines += [f"🏅 {titles.get(i['code'], i['code'])} · {timezone.localtime(i['awarded_at']):%d.%m.%Y}" for i in earned]
    if pending:
        lines.append("")
        lines.append(t(lang, "ach.next"))
        lines += [f"▫️ {titles.get(i['code'], i['code'])} — {i['value']}/{i['threshold']}"
                  for i in sorted(pending, key=lambda i: -i["percent"])[:3]]
    return Reply("\n".join(lines), [[Button(t(lang, "menu.open_site"), url=_site(reverse("profile") + "#achievements"))],
                                    _menu_row(lang)])


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

def _c_new(user, lang, event, *args):
    set_state(event.chat_id, "new_request", "type")
    rows = [[Button(_help_type(code, lang), f"ct|{code}")] for code, _ in HELP_TYPE_CHOICES]
    rows.append([Button(t(lang, "btn.cancel"), "cx")])
    return Reply(t(lang, "c.step_type"), rows)


def _c_type(user, lang, event, code=""):
    state = get_state(event.chat_id)
    if not state or state["flow"] != "new_request" or code not in dict(HELP_TYPE_CHOICES):
        return _c_new(user, lang, event)
    set_state(event.chat_id, "new_request", "description", help_type=code)
    return Reply(t(lang, "c.step_description", type=_help_type(code, lang)), [[Button(t(lang, "btn.cancel"), "cx")]])


def _c_priority(user, lang, event, code=""):
    state = get_state(event.chat_id)
    if not state or state["flow"] != "new_request" or state["step"] != "priority" or code not in dict(PRIORITY_CHOICES):
        return Reply(t(lang, "c.expired"), [_menu_row(lang)])
    data = dict(state["data"], priority=code)
    set_state(event.chat_id, "new_request", "confirm", **data)
    return _c_summary(lang, data)


def _c_summary(lang, data):
    lines = [t(lang, "c.confirm_title"),
             f"{t(lang, 'f.type')}: {_help_type(data['help_type'], lang)}",
             f"{t(lang, 'f.priority')}: {_priority(data['priority'], lang)}",
             f"{t(lang, 'f.address')}: {data['address']}"
             + (" 📍" if data.get("latitude") is not None else ""),
             f"{t(lang, 'f.phone')}: {data['phone']}", "", data["description"]]
    return Reply("\n".join(lines), [[Button(t(lang, "c.send"), f"cok|~{_nonce()}"), Button(t(lang, "btn.cancel"), "cx")]])


def _c_submit(user, lang, event, *args):
    state = get_state(event.chat_id)
    if not state or state["flow"] != "new_request" or state["step"] != "confirm":
        return Reply(t(lang, "c.expired"), [_menu_row(lang)])
    data = state["data"]
    form = HelpRequestForm({
        "help_type": data["help_type"], "description": data["description"], "address": data["address"],
        "phone": data["phone"], "priority": data["priority"],
        "latitude": data.get("latitude") or "", "longitude": data.get("longitude") or "",
    })
    if not form.is_valid():
        field_name = next(iter(form.errors))
        return Reply(t(lang, "c.invalid", field=t(lang, f"f.{field_name}")), [[Button(t(lang, "menu.new_request"), "cn")]])
    task = task_actions.create_request(user, form)
    clear_state(event.chat_id)
    return [Reply(t(lang, "c.created", task_id=task.pk), remove_keyboard=True), main_menu(user, lang)]


def _c_cancel_wizard(user, lang, event, *args):
    clear_state(event.chat_id)
    return [Reply(t(lang, "cancelled"), remove_keyboard=True), main_menu(user, lang)]


def _c_list(user, lang, event, page="0"):
    page = max(_int(page) or 0, 0)
    qs = HelpRequest.objects.filter(client=user).order_by("-created_at", "-id")
    total = qs.count()
    tasks = list(qs[page * PAGE:(page + 1) * PAGE])
    if not tasks:
        return Reply(t(lang, "c.no_requests"), [[Button(t(lang, "menu.new_request"), "cn")], _menu_row(lang)])
    rows = [[Button(f"#{x.pk} · {_help_type(x.help_type, lang)} · {_status(x.status, lang)}", f"cd|{x.pk}")]
            for x in tasks]
    pager = _pager(lang, "cl", page, total)
    if pager:
        rows.append(pager)
    rows.append(_menu_row(lang))
    return Reply(t(lang, "c.my_requests"), rows, edit=event.kind == "callback")


def _eta_text(task, lang):
    latest = tracking.latest(task) if task.status == "active" else None
    if not latest or not task.has_location:
        return ""
    km = haversine_km(latest["lat"], latest["lng"], float(task.latitude), float(task.longitude))
    minutes = max(1, round(km / matching.AVERAGE_SPEED_KMH * 60))
    return t(lang, "c.eta", km=f"{km:.1f}", minutes=minutes)


def _c_detail(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None or task.client_id != user.pk:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    lines = [_task_card(task, lang, show_contacts=False)]
    if task.volunteer_id:
        public = feedback.public_summary(task.volunteer)
        lines.append("")
        lines.append(t(lang, "c.volunteer", name=_name(task.volunteer), region=_region(task.volunteer.region, lang)))
        if public["count"]:
            lines.append(t(lang, "ach.rating", avg=f"{public['avg']:.1f}", count=public["count"]))
        eta = _eta_text(task, lang)
        if eta:
            lines.append(eta)
    rows = []
    if access.can_write_chat(user, task) or (access.can_read_chat(user, task) and task.status == "active"):
        rows.append([Button(t(lang, "btn.chat"), f"ch|{task.pk}")])
    if cancellation.can_cancel(user, task):
        rows.append([Button(t(lang, "c.cancel_request"), f"ccan|{task.pk}")])
    if feedback.is_reviewable(user, task):
        rows.append([Button(t(lang, "c.review"), f"rv|{task.pk}")])
    rows.append(_back(lang, "cl|0"))
    return Reply("\n".join(lines), rows)


def _c_cancel_confirm(user, lang, event, task_id=""):
    return Reply(t(lang, "c.cancel_confirm", task_id=task_id),
                 [[Button(t(lang, "btn.yes"), f"ccany|{task_id}|~{_nonce()}"), Button(t(lang, "btn.no"), f"cd|{task_id}")]])


def _c_cancel(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None or task.client_id != user.pk:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    outcome = cancellation.cancel_request(task, user, "")
    return Reply(t(lang, "c.cancelled_ok" if outcome.ok else "c.cancel_failed"), [_back(lang, "cl|0")])


# --- review -----------------------------------------------------------------

def _rv_start(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None or not feedback.is_reviewable(user, task):
        return Reply(t(lang, "rv.unavailable"), [_menu_row(lang)])
    existing = feedback.review_for_task(task)
    if existing is not None and not feedback.can_edit(user, existing):
        return Reply(t(lang, "rv.closed"), [_menu_row(lang)])
    set_state(event.chat_id, "review", "stars", task_id=task.pk, tags=[])
    return Reply(t(lang, "rv.stars"), [[Button("★" * n, f"rs|{task.pk}|{n}") for n in (1, 2, 3)],
                                         [Button("★" * n, f"rs|{task.pk}|{n}") for n in (4, 5)],
                                         [Button(t(lang, "btn.cancel"), "m")]])


def _rv_tags_reply(lang, task_id, stars, tags):
    rows = [[Button(("✅ " if tag in tags else "") + t(lang, f"tag.{tag}"), f"rtg|{task_id}|{tag}")]
            for tag in REVIEW_TAGS]
    rows.append([Button(t(lang, "rv.next"), f"rdone|{task_id}")])
    return Reply(t(lang, "rv.tags", stars="★" * stars), rows, edit=True)


def _rv_stars(user, lang, event, task_id="", stars=""):
    state = get_state(event.chat_id)
    stars_value = feedback.clean_stars(stars)
    if not state or state["flow"] != "review" or stars_value is None:
        return Reply(t(lang, "c.expired"), [_menu_row(lang)])
    set_state(event.chat_id, "review", "tags", task_id=_int(task_id), stars=stars_value, tags=[])
    return _rv_tags_reply(lang, task_id, stars_value, [])


def _rv_tag(user, lang, event, task_id="", tag=""):
    state = get_state(event.chat_id)
    if not state or state["flow"] != "review" or tag not in REVIEW_TAGS:
        return Reply(t(lang, "c.expired"), [_menu_row(lang)])
    data = state["data"]
    tags = list(data.get("tags") or [])
    tags.remove(tag) if tag in tags else tags.append(tag)
    set_state(event.chat_id, "review", "tags", **dict(data, tags=tags))
    return _rv_tags_reply(lang, task_id, data["stars"], tags)


def _rv_done_tags(user, lang, event, task_id=""):
    state = get_state(event.chat_id)
    if not state or state["flow"] != "review":
        return Reply(t(lang, "c.expired"), [_menu_row(lang)])
    set_state(event.chat_id, "review", "comment", **state["data"])
    return Reply(t(lang, "rv.comment"), [[Button(t(lang, "rv.skip"), f"rskip|{task_id}|~{_nonce()}")]])


def _rv_skip(user, lang, event, task_id=""):
    return _rv_submit(user, lang, event, "")


def _rv_submit(user, lang, event, comment):
    state = get_state(event.chat_id)
    if not state or state["flow"] != "review":
        return Reply(t(lang, "c.expired"), [_menu_row(lang)])
    data = state["data"]
    task = _task(data["task_id"])
    clear_state(event.chat_id)
    if task is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    outcome = feedback.submit_review(user, task, stars=data.get("stars"), comment=comment, tags=data.get("tags") or [])
    if not outcome.ok:
        return Reply(t(lang, f"rv.err.{outcome.code}") if outcome.code in ("edit_closed", "comment_too_long")
                     else t(lang, "rv.unavailable"), [_menu_row(lang)])
    return [Reply(t(lang, "rv.thanks")), main_menu(user, lang)]


# --- chat relay (client + volunteer + staff) ---------------------------------

def _chat_start(user, lang, chat_id, task_id):
    task = _task(task_id)
    if task is None or not access.can_write_chat(user, task):
        return [Reply(t(lang, "chat.closed"), [_menu_row(lang)])]
    set_state(chat_id, "chat", "compose", task_id=task.pk)
    page = chat.history(user, task, limit=5) or {"messages": []}
    lines = [t(lang, "chat.title", task_id=task.pk)]
    for m in page["messages"]:
        who = t(lang, "chat.me") if m.get("mine") else (
            t(lang, "chat.curator") if m.get("is_staff_post") else (m.get("sender_display") or "…"))
        body = m.get("body") or ("📷" if m.get("image_url") else "")
        if m.get("kind") == "system":
            body, who = t(lang, "chat.system"), "—"
        lines.append(f"{who}: {body}"[:500])
    lines += ["", t(lang, "chat.compose")]
    return [Reply("\n".join(lines), [[Button(t(lang, "chat.stop"), "chx")], [Button(t(lang, "chat.open_site"),
                                                                                    url=_site(chat.chat_url(task)))]])]


def _chat_cb(user, lang, event, task_id=""):
    return _chat_start(user, lang, event.chat_id, task_id)


def _chat_stop(user, lang, event, *args):
    clear_state(event.chat_id)
    return main_menu(user, lang)


def _chat_send(user, lang, event, state):
    task = _task(state["data"].get("task_id"))
    if task is None or not access.can_write_chat(user, task):
        clear_state(event.chat_id)
        return [Reply(t(lang, "chat.closed"), [_menu_row(lang)])]
    image = None
    if event.kind == "photo" and (event.photo_too_large or not event.photo):
        return [Reply(t(lang, "chat.err.too_large"))]
    if event.kind == "photo" and event.photo:
        image = SimpleUploadedFile("telegram.jpg", event.photo, content_type="image/jpeg")
    text = event.text if event.kind in ("text", "photo") else ""
    outcome = chat.send_message(user, task, text=text or None, image=image)
    if not outcome.ok:
        return [Reply(t(lang, f"chat.err.{outcome.code}") if outcome.code in (
            "too_long", "rate_limited", "too_large", "invalid_image", "unsupported_format") else t(lang, "chat.err.generic"))]
    set_state(event.chat_id, "chat", "compose", task_id=task.pk)  # refresh the 30-min window
    return [Reply(t(lang, "chat.sent"), [[Button(t(lang, "chat.stop"), "chx")]])]


# --------------------------------------------------------------------------- #
# staff
# --------------------------------------------------------------------------- #

def _task_list_reply(lang, event, title_key, qs, action, page, detail="sd"):
    page = max(_int(page) or 0, 0)
    total = qs.count()
    tasks = list(qs[page * PAGE:(page + 1) * PAGE])
    if not tasks:
        return Reply(t(lang, f"{title_key}.none"), [_menu_row(lang)], edit=event.kind == "callback")
    rows = [[Button(f"#{x.pk} · {_help_type(x.help_type, lang)} · {_region(x.region, lang)} · {_ago(x.created_at, lang)}",
                    f"{detail}|{x.pk}")] for x in tasks]
    pager = _pager(lang, action, page, total)
    if pager:
        rows.append(pager)
    rows.append(_menu_row(lang))
    return Reply(t(lang, title_key, total=total), rows, edit=event.kind == "callback")


def _s_overdue(user, lang, event, page="0"):
    return _task_list_reply(lang, event, "s.overdue", overdue.currently_overdue_tasks(), "so", page)


def _s_stale(user, lang, event, page="0"):
    return _task_list_reply(lang, event, "s.stale", stale.currently_stale_pending(), "ss", page)


def _s_unassigned(user, lang, event, page="0"):
    qs = HelpRequest.objects.filter(status="pending").order_by("-is_urgent", "created_at", "id")
    return _task_list_reply(lang, event, "s.unassigned", qs, "su", page)


def _s_detail(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    lines = [_task_card(task, lang, show_contacts=True)]
    if task.volunteer_id:
        lines.append(t(lang, "s.volunteer", name=_name(task.volunteer)))
    rows = []
    if task.status == "pending":
        rows.append([Button(t(lang, "s.recommend"), f"sr|{task.pk}")])
    if task.status in ("pending", "active"):
        rows.append([Button(t(lang, "btn.chat"), f"ch|{task.pk}")] if access.can_write_chat(user, task) else [])
    rows = [r for r in rows if r]
    rows.append([Button(t(lang, "menu.open_site"), url=_site(reverse("task_detail", args=[task.pk])))])
    rows.append(_menu_row(lang))
    return Reply("\n".join(lines), rows)


def _s_recommend(user, lang, event, task_id=""):
    task = _task(task_id)
    if task is None or task.status != "pending":
        return Reply(t(lang, "s.not_pending"), [_menu_row(lang)])
    recs = matching.recommend_volunteers(task, limit=3)
    if not recs:
        return Reply(t(lang, "s.no_recs"), [_back(lang, f"sd|{task.pk}")])
    lines = [t(lang, "s.recs_title", task_id=task.pk)]
    rows = []
    for r in recs:
        v = r["volunteer"]
        lines.append(f"• {_name(v)} — {r['score']} · {r['distance_km']} km")
        rows.append([Button(t(lang, "s.assign", name=_name(v)), f"sa|{task.pk}|{v.pk}|~{_nonce()}")])
    rows.append(_back(lang, f"sd|{task.pk}"))
    return Reply("\n".join(lines), rows)


def _s_assign(user, lang, event, task_id="", volunteer_id="", force=False):
    task = _task(task_id)
    volunteer = Users.objects.select_related("profile").filter(pk=_int(volunteer_id), is_volunteer=True,
                                                               is_active=True).first()
    if task is None or volunteer is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    outcome = task_actions.assign(user, task, volunteer, confirm_away=force)
    if outcome.code == task_actions.AWAY:
        return Reply(t(lang, "s.away_confirm", name=_name(volunteer)),
                     [[Button(t(lang, "s.assign_anyway"), f"saf|{task.pk}|{volunteer.pk}|~{_nonce()}")],
                      _back(lang, f"sr|{task.pk}")])
    if not outcome.ok:
        return Reply(t(lang, "s.not_pending"), [_menu_row(lang)])
    return Reply(t(lang, "s.assigned", task_id=task.pk, name=_name(volunteer)), [_menu_row(lang)])


def _s_assign_force(user, lang, event, task_id="", volunteer_id=""):
    return _s_assign(user, lang, event, task_id, volunteer_id, force=True)


def _s_sos_list(user, lang, event, page="0"):
    page = max(_int(page) or 0, 0)
    qs = emergency.open_reports()
    total = qs.count()
    reports = list(qs[page * PAGE:(page + 1) * PAGE])
    if not reports:
        return Reply(t(lang, "s.sos.none"), [_menu_row(lang)], edit=event.kind == "callback")
    rows = [[Button(f"🆘 #{r.pk} · {_status_sos(r.status, lang)} · {_region(r.region, lang)} · {_ago(r.created_at, lang)}",
                    f"sed|{r.pk}")] for r in reports]
    pager = _pager(lang, "se", page, total)
    if pager:
        rows.append(pager)
    rows.append(_menu_row(lang))
    return Reply(t(lang, "s.sos", total=total), rows, edit=event.kind == "callback")


def _status_sos(code, lang):
    return t(lang, f"sos.status.{code}")


def _s_sos_detail(user, lang, event, report_id=""):
    report = EmergencyReport.objects.select_related("volunteer__profile", "help_request").filter(pk=_int(report_id)).first()
    if report is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    lines = [f"🆘 #{report.pk} · {_status_sos(report.status, lang)}",
             t(lang, "s.sos.volunteer", name=_name(report.volunteer)),
             t(lang, "s.sos.task", task_id=report.help_request_id),
             f"{t(lang, 'f.created')}: {_ago(report.created_at, lang)}"]
    if report.reason:
        lines += ["", report.reason[:500]]
    rows = []
    if report.status == EmergencyReport.STATUS_OPEN:
        rows.append([Button(t(lang, "s.sos.ack"), f"sea|{report.pk}|~{_nonce()}")])
    if report.is_open:
        rows.append([Button(t(lang, "s.sos.resolve"), f"ser|{report.pk}|~{_nonce()}")])
    if report.latitude is not None:
        rows.append([Button(t(lang, "s.sos.map"), url=routing.deep_links((float(report.latitude),
                                                                           float(report.longitude)))["google"])])
    rows.append(_back(lang, "se|0"))
    return Reply("\n".join(lines), rows)


def _s_sos_act(user, lang, event, report_id, action):
    report = EmergencyReport.objects.select_related("volunteer", "help_request").filter(pk=_int(report_id)).first()
    if report is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    try:
        if action == "ack":
            emergency.acknowledge(report, actor=user)
        else:
            emergency.resolve(report, actor=user, note=t(lang, "s.sos.note"))
    except ValueError:
        return Result(toast=t(lang, "s.sos.invalid"))
    return Result([_s_sos_detail(user, lang, event, report.pk)], toast=t(lang, "saved"))


def _s_sos_ack(user, lang, event, report_id=""):
    return _s_sos_act(user, lang, event, report_id, "ack")


def _s_sos_resolve(user, lang, event, report_id=""):
    return _s_sos_act(user, lang, event, report_id, "resolve")


def _s_applications(user, lang, event, page="0"):
    page = max(_int(page) or 0, 0)
    qs = VolunteerApplication.objects.filter(status=VolunteerApplication.STATUS_PENDING).select_related(
        "user__profile").order_by("created_at")
    total = qs.count()
    apps = list(qs[page * PAGE:(page + 1) * PAGE])
    if not apps:
        return Reply(t(lang, "s.apps.none"), [_menu_row(lang)], edit=event.kind == "callback")
    rows = [[Button(f"{_name(a.user)} · {_region(a.user.region, lang)} · {_ago(a.created_at, lang)}", f"sapd|{a.pk}")]
            for a in apps]
    pager = _pager(lang, "sap", page, total)
    if pager:
        rows.append(pager)
    rows.append(_menu_row(lang))
    return Reply(t(lang, "s.apps", total=total), rows, edit=event.kind == "callback")


def _s_application(user, lang, event, app_id=""):
    app = VolunteerApplication.objects.select_related("user__profile").filter(pk=_int(app_id)).first()
    if app is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    motivation = (app.reason or "")[:700]
    lines = [t(lang, "s.apps.card", name=_name(app.user), region=_region(app.user.region, lang),
               status=t(lang, f"va.{app.status}")), "", motivation]
    rows = []
    if app.status == VolunteerApplication.STATUS_PENDING:
        rows.append([Button(t(lang, "s.apps.approve"), f"sapy|{app.pk}|~{_nonce()}"),
                     Button(t(lang, "s.apps.reject"), f"sapn|{app.pk}|~{_nonce()}")])
    rows.append(_back(lang, "sap|0"))
    return Reply("\n".join(line for line in lines if line is not None), rows)


def _s_app_act(user, lang, event, app_id, approve):
    app = VolunteerApplication.objects.select_related("user").filter(pk=_int(app_id)).first()
    if app is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    fn = task_actions.approve_application if approve else task_actions.reject_application
    outcome = fn(user, app)
    key = ("s.apps.approved" if approve else "s.apps.rejected") if outcome.ok else "already_done"
    return Reply(t(lang, key, name=_name(app.user)), [_back(lang, "sap|0")])


def _s_app_approve(user, lang, event, app_id=""):
    return _s_app_act(user, lang, event, app_id, True)


def _s_app_reject(user, lang, event, app_id=""):
    return _s_app_act(user, lang, event, app_id, False)


def _s_donations(user, lang, event, page="0"):
    page = max(_int(page) or 0, 0)
    qs = donations.pending_offers()
    total = qs.count()
    offers = list(qs[page * PAGE:(page + 1) * PAGE])
    if not offers:
        return Reply(t(lang, "s.don.none"), [_menu_row(lang)], edit=event.kind == "callback")
    rows = [[Button(f"#{o.pk} · {o.item_label} × {o.quantity} · {_region(o.region, lang)}", f"sdod|{o.pk}")]
            for o in offers]
    pager = _pager(lang, "sdo", page, total)
    if pager:
        rows.append(pager)
    rows.append(_menu_row(lang))
    return Reply(t(lang, "s.don", total=total), rows, edit=event.kind == "callback")


def _s_donation(user, lang, event, offer_id=""):
    offer = Donation.objects.filter(pk=_int(offer_id)).first()
    if offer is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    lines = [f"#{offer.pk} · {offer.item_label} × {offer.quantity}{(' ' + offer.unit) if offer.unit else ''}",
             f"{t(lang, 'f.region')}: {_region(offer.region, lang)}",
             f"{t(lang, 'f.status')}: {_label('donation_status', offer.status, lang)}"]
    if offer.description:
        lines += ["", offer.description[:500]]
    rows = []
    if offer.status == "pending":
        rows.append([Button(t(lang, "s.don.approve"), f"sdoy|{offer.pk}|~{_nonce()}"),
                     Button(t(lang, "s.don.cancel"), f"sdon|{offer.pk}|~{_nonce()}")])
    rows.append(_back(lang, "sdo|0"))
    return Reply("\n".join(lines), rows)


def _s_donation_act(user, lang, event, offer_id, approve):
    offer = Donation.objects.filter(pk=_int(offer_id)).first()
    if offer is None:
        return Reply(t(lang, "not_found"), [_menu_row(lang)])
    try:
        (donations.approve_offer if approve else donations.cancel_offer)(offer, actor=user)
    except ValueError:
        return Result(toast=t(lang, "already_done"))
    return Reply(t(lang, "s.don.approved" if approve else "s.don.cancelled", offer_id=offer.pk), [_back(lang, "sdo|0")])


def _s_donation_approve(user, lang, event, offer_id=""):
    return _s_donation_act(user, lang, event, offer_id, True)


def _s_donation_cancel(user, lang, event, offer_id=""):
    return _s_donation_act(user, lang, event, offer_id, False)


def _s_broadcast(user, lang, event, *args):
    clear_state(event.chat_id)
    rows = [[Button(t(lang, "s.bc.all"), "sbr|all")]]
    regions = [Button(_region(code, lang), f"sbr|{code}") for code, _ in REGION_CHOICES]
    rows += [regions[i:i + 2] for i in range(0, len(regions), 2)]
    rows.append([Button(t(lang, "btn.cancel"), "m")])
    return Reply(t(lang, "s.bc.region"), rows)


def _s_broadcast_region(user, lang, event, region=""):
    if region != "all" and region not in dict(REGION_CHOICES):
        return _s_broadcast(user, lang, event)
    set_state(event.chat_id, "broadcast", "subject", region=region)
    return Reply(t(lang, "s.bc.subject"), [[Button(t(lang, "btn.cancel"), "m")]])


def _s_broadcast_send(user, lang, event, *args):
    state = get_state(event.chat_id)
    if not state or state["flow"] != "broadcast" or state["step"] != "confirm":
        return Reply(t(lang, "c.expired"), [_menu_row(lang)])
    data = state["data"]
    clear_state(event.chat_id)
    broadcast = task_actions.send_broadcast(user, region=data["region"], subject=data["subject"], message=data["message"])
    return [Reply(t(lang, "s.bc.sent", count=broadcast.sent_count)), main_menu(user, lang)]


# --------------------------------------------------------------------------- #
# free-text / photo / contact steps of the wizards
# --------------------------------------------------------------------------- #

def _continue_flow(user, lang, event, state):
    flow, step, data = state["flow"], state["step"], dict(state.get("data") or {})
    if flow == "chat":
        if event.kind in ("text", "photo"):
            return _chat_send(user, lang, event, state)
        return [Reply(t(lang, "chat.compose"))]
    if flow == "review" and step == "comment" and event.kind == "text":
        out = _rv_submit(user, lang, event, event.text)
        return out if isinstance(out, list) else [out]
    if flow == "broadcast" and event.kind == "text":
        text = event.text.strip()
        if step == "subject":
            set_state(event.chat_id, "broadcast", "message", **dict(data, subject=text[:255]))
            return [Reply(t(lang, "s.bc.message"), [[Button(t(lang, "btn.cancel"), "m")]])]
        if step == "message":
            data["message"] = text[:4000]
            set_state(event.chat_id, "broadcast", "confirm", **data)
            region = t(lang, "s.bc.all") if data["region"] == "all" else _region(data["region"], lang)
            return [Reply(t(lang, "s.bc.confirm", region=region, subject=data["subject"], message=data["message"]),
                          [[Button(t(lang, "s.bc.send"), f"sby|~{_nonce()}"), Button(t(lang, "btn.cancel"), "m")]])]
    if flow == "new_request":
        return _request_step(user, lang, event, step, data)
    return [Reply(t(lang, "c.expired")), main_menu(user, lang)]


def _request_step(user, lang, event, step, data):
    cancel = [[Button(t(lang, "btn.cancel"), "cx")]]
    if step == "description" and event.kind == "text":
        text = event.text.strip()
        if len(text) < 5:
            return [Reply(t(lang, "c.too_short"), cancel)]
        set_state(event.chat_id, "new_request", "address", **dict(data, description=text[:2000]))
        return [Reply(t(lang, "c.step_address"), keyboard=_kb(lang, "location"))]
    if step == "address":
        if event.kind == "location":
            if not is_valid_coordinate(event.lat, event.lng):
                return [Reply(t(lang, "loc.invalid"), keyboard=_kb(lang, "location"))]
            set_state(event.chat_id, "new_request", "address_text",
                      **dict(data, latitude=round(event.lat, 6), longitude=round(event.lng, 6)))
            return [Reply(t(lang, "c.step_address_text"), remove_keyboard=True)]
        if event.kind == "text" and event.text.strip():
            set_state(event.chat_id, "new_request", "phone", **dict(data, address=event.text.strip()[:255]))
            return [Reply(t(lang, "c.step_phone"), keyboard=_kb(lang, "contact"))]
    if step == "address_text" and event.kind == "text" and event.text.strip():
        set_state(event.chat_id, "new_request", "phone", **dict(data, address=event.text.strip()[:255]))
        return [Reply(t(lang, "c.step_phone"), keyboard=_kb(lang, "contact"))]
    if step == "phone" and event.kind in ("text", "contact"):
        raw = event.contact_phone if event.kind == "contact" else event.text
        try:
            phone = normalize_tj_phone(raw)
        except ValidationError:
            phone = ""
        if not phone:
            return [Reply(t(lang, "c.bad_phone"), keyboard=_kb(lang, "contact"))]
        set_state(event.chat_id, "new_request", "priority", **dict(data, phone=phone))
        rows = [[Button(_priority(code, lang), f"cp|{code}")] for code, _ in PRIORITY_CHOICES]
        rows.append([Button(t(lang, "btn.cancel"), "cx")])
        return [Reply(t(lang, "c.got_phone", phone=phone), remove_keyboard=True), Reply(t(lang, "c.step_priority"), rows)]
    return [Reply(t(lang, f"c.hint.{step}") if step in ("type", "description", "address", "address_text", "phone",
                                                         "priority", "confirm") else t(lang, "c.expired"), cancel)]


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

V, C, S, A = ("volunteer",), ("client",), ("staff",), ("admin",)
CALLBACKS = {
    "m": _Cb(_m), "lang": _Cb(_lang_cb), "ntf": _Cb(_notifications), "ntfr": _Cb(_notifications_read),
    "com": _Cb(_community),
    # volunteer
    "vt": _Cb(_v_tasks, V), "vd": _Cb(_v_detail, V), "va": _Cb(_v_accept, V), "my": _Cb(_v_my, V),
    "vs": _Cb(_v_stage, V + S), "vc": _Cb(_v_complete_confirm, V), "vcy": _Cb(_v_complete, V),
    "sos": _Cb(_v_sos_confirm, V), "sosy": _Cb(_v_sos, V), "rt": _Cb(_v_route, V + S),
    "loc": _Cb(_v_location_help, V), "lcons": _Cb(_v_consent, V), "ach": _Cb(_achievements, V),
    # client
    "cn": _Cb(_c_new, C), "ct": _Cb(_c_type, C), "cp": _Cb(_c_priority, C), "cok": _Cb(_c_submit, C),
    "cx": _Cb(_c_cancel_wizard), "cl": _Cb(_c_list, C), "cd": _Cb(_c_detail, C),
    "ccan": _Cb(_c_cancel_confirm, C), "ccany": _Cb(_c_cancel, C),
    "rv": _Cb(_rv_start, C), "rs": _Cb(_rv_stars, C), "rtg": _Cb(_rv_tag, C), "rdone": _Cb(_rv_done_tags, C),
    "rskip": _Cb(_rv_skip, C),
    # chat (any party / staff — access.can_write_chat decides)
    "ch": _Cb(_chat_cb), "chx": _Cb(_chat_stop),
    # staff
    "so": _Cb(_s_overdue, S), "ss": _Cb(_s_stale, S), "su": _Cb(_s_unassigned, S), "sd": _Cb(_s_detail, S),
    "sr": _Cb(_s_recommend, S), "sa": _Cb(_s_assign, S), "saf": _Cb(_s_assign_force, S),
    "se": _Cb(_s_sos_list, S), "sed": _Cb(_s_sos_detail, S), "sea": _Cb(_s_sos_ack, S), "ser": _Cb(_s_sos_resolve, S),
    "sap": _Cb(_s_applications, A), "sapd": _Cb(_s_application, A), "sapy": _Cb(_s_app_approve, A),
    "sapn": _Cb(_s_app_reject, A),
    "sdo": _Cb(_s_donations, S), "sdod": _Cb(_s_donation, S), "sdoy": _Cb(_s_donation_approve, S),
    "sdon": _Cb(_s_donation_cancel, S),
    "sb": _Cb(_s_broadcast, S), "sbr": _Cb(_s_broadcast_region, S), "sby": _Cb(_s_broadcast_send, S),
}
# "lang|<code>" sets the language; "lang" alone shows the menu.
CALLBACKS["lang"] = _Cb(lambda user, lang, event, code="": _set_lang(user, lang, event, code) if code else _lang_menu(lang))
