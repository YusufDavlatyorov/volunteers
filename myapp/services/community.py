"""Volunteer community on Telegram (Feature Pack v2, feature 5) — the rules.

Transport-agnostic: the community bot process (``myapp/telegram/community_bot``)
and Django code (views, Celery, cron commands) both call in here. Telegram
calls go through ``myapp/telegram/api.py`` with the community bot token
(never raises, never logs the token).

Who may be in the community: an **active approved volunteer or staff member
with a linked Telegram** (``is_eligible``). They get **single-use invite links
valid 24 h** (``create_invites`` — only the SHA-256 of each link is stored);
the bot binds a joiner to their invite (``on_join``) and removes anyone who
joined otherwise. When someone stops being eligible (application rejected,
account deactivated, volunteer role removed) they are removed
(``revoke`` + the hourly ``sweep``). Removal is ban + unban: no permanent ban,
they can come back with a new invite if eligible again.

Owner decisions (2026-10-03): the weekly post is a "thank you of the week"
without rankings (counts + new badges, names alphabetical, only for volunteers
who didn't switch ``Profile.community_shoutouts`` off); "Наставник" is awarded
by hand (``/mentor``). The community never sees client data.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, Q
from django.utils import timezone

from accounts.models import REGION_CHOICES, Users

from ..models import (
    CommunityInvite,
    CommunityMember,
    CommunityPost,
    HelpRequest,
    ModerationAction,
    TaskReview,
    UserAchievement,
)
from ..telegram import api
from . import notify_texts
from .access import is_staff_user

logger = logging.getLogger(__name__)

GENERAL = "general"
INVITE_COOLDOWN_SECONDS = 600     # one new set of links per user per 10 min
FLOOD_MESSAGES = 5                # more than this …
FLOOD_WINDOW_SECONDS = 10         # … within this window → muted
FLOOD_MUTE_MINUTES = 10
DEFAULT_MUTE_MINUTES = 60
MAX_MUTE_MINUTES = 7 * 24 * 60
WEEKLY_NAMED_LIMIT = 40           # more new-badge holders are counted, not named (4096-char message cap)
MESSAGE_MAX = 4000

OK = "ok"
NOT_CONFIGURED = "not_configured"
NOT_ELIGIBLE = "not_eligible"
RATE_LIMITED = "rate_limited"
FAILED = "failed"


def token():
    return getattr(settings, "TELEGRAM_COMMUNITY_BOT_TOKEN", "") or ""


def chats() -> dict:
    """``{"general": -100…, "sogd": -100…}`` from TELEGRAM_COMMUNITY_CHAT_IDS
    ("general:-100…,sogd:-100…"; a bare id means general). Bad parts ignored."""
    out = {}
    regions = {code for code, _ in REGION_CHOICES}
    for part in (getattr(settings, "TELEGRAM_COMMUNITY_CHAT_IDS", "") or "").split(","):
        part = part.strip()
        if not part:
            continue
        key, _, raw = part.rpartition(":")
        key = (key or GENERAL).strip().lower()
        try:
            chat_id = int(raw.strip())
        except ValueError:
            continue
        if key == GENERAL or key in regions:
            out.setdefault(key, chat_id)
    return out


def configured() -> bool:
    return bool(token() and chats().get(GENERAL))


def is_community_chat(chat_id) -> bool:
    return chat_id in set(chats().values())


def chats_for(user) -> list:
    """The chats ``user`` may join: general + their region's group (if any).
    Staff: every configured chat (they moderate)."""
    configured_chats = chats()
    if is_staff_user(user):
        return list(configured_chats.items())
    out = [(GENERAL, configured_chats[GENERAL])] if GENERAL in configured_chats else []
    region = getattr(user, "region", "")
    if region and region in configured_chats:
        out.append((region, configured_chats[region]))
    return out


def is_eligible(user) -> bool:
    return bool(
        user is not None and getattr(user, "pk", None) and user.is_active and user.telegram_id
        and (user.is_volunteer or is_staff_user(user))
    )


def user_for_tg(tg_user_id):
    """The linked account of a Telegram user (private chat id == user id)."""
    if not tg_user_id:
        return None
    return Users.objects.filter(telegram_id=tg_user_id, is_active=True).first()


def staff_for_tg(tg_user_id):
    user = user_for_tg(tg_user_id)
    return user if user is not None and is_staff_user(user) else None


def link_hash(link) -> str:
    return hashlib.sha256(str(link).strip().encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# invites
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class InviteOutcome:
    ok: bool
    code: str
    links: list = field(default_factory=list)   # [(chat key, url)] — shown once, never stored
    expires_at: object = None


def create_invites(user, now=None) -> InviteOutcome:
    """Fresh single-use links (24 h) for every chat ``user`` may join."""
    if not configured():
        return InviteOutcome(False, NOT_CONFIGURED)
    if not is_eligible(user):
        return InviteOutcome(False, NOT_ELIGIBLE)
    if not cache.add(f"community:invite:{user.pk}", 1, INVITE_COOLDOWN_SECONDS):
        return InviteOutcome(False, RATE_LIMITED)
    now = now or timezone.now()
    expires = now + timedelta(hours=int(getattr(settings, "COMMUNITY_INVITE_HOURS", 24)))
    links = []
    for key, chat_id in chats_for(user):
        url = api.create_invite_link(token(), chat_id, name=f"kk-{user.pk}", expire_date=expires.timestamp())
        if not url:
            continue
        CommunityInvite.objects.create(user=user, chat_id=chat_id, link_hash=link_hash(url), expires_at=expires)
        links.append((key, url))
    if not links:
        cache.delete(f"community:invite:{user.pk}")
        return InviteOutcome(False, FAILED)
    logger.info("community invites created user=%s chats=%s", user.pk, len(links))
    return InviteOutcome(True, OK, links, expires)


# --------------------------------------------------------------------------- #
# joins / leaves / removal
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class JoinDecision:
    allowed: bool
    reason: str
    user: object = None


def _remember_member(chat_id, tg_user_id, user):
    CommunityMember.objects.update_or_create(
        chat_id=chat_id, tg_user_id=tg_user_id,
        defaults={"user": user, "left_at": None, "removed_reason": "", "joined_at": timezone.now()},
    )


def on_join(chat_id, tg_user_id, invite_link=None, now=None) -> JoinDecision:
    """Decide about someone who just joined a community chat. Allowed: the
    holder of an unused, unexpired invite minted for *their* account (the link
    is consumed), or an eligible staff member. Everyone else is removed by the
    caller (the bot) — ``allowed=False``."""
    now = now or timezone.now()
    if invite_link:
        invite = (CommunityInvite.objects.select_related("user")
                  .filter(link_hash=link_hash(invite_link), chat_id=chat_id).first())
        if invite is not None and invite.used_at is None and invite.expires_at > now \
                and invite.user.telegram_id == tg_user_id and is_eligible(invite.user):
            claimed = CommunityInvite.objects.filter(pk=invite.pk, used_at__isnull=True).update(
                used_at=now, used_by_tg_id=tg_user_id)
            if claimed:
                _remember_member(chat_id, tg_user_id, invite.user)
                logger.info("community join chat=%s user=%s via invite=%s", chat_id, invite.user_id, invite.pk)
                return JoinDecision(True, "invite", invite.user)
    staff = staff_for_tg(tg_user_id)
    if staff is not None:
        _remember_member(chat_id, tg_user_id, staff)
        return JoinDecision(True, "staff", staff)
    record(chat_id, ModerationAction.KICK, target_tg_id=tg_user_id, reason="unknown_joiner")
    logger.info("community join refused chat=%s reason=unknown_joiner", chat_id)
    return JoinDecision(False, "unknown_joiner")


def on_leave(chat_id, tg_user_id, reason="left"):
    CommunityMember.objects.filter(chat_id=chat_id, tg_user_id=tg_user_id, left_at__isnull=True).update(
        left_at=timezone.now(), removed_reason=reason[:40])


def revoke(user, reason="not_eligible") -> int:
    """Remove ``user`` from every community chat they are in (if they are no
    longer eligible). Returns how many chats they were removed from."""
    if user is None or is_eligible(user):
        return 0
    members = list(CommunityMember.objects.filter(left_at__isnull=True).filter(
        Q(user=user) | Q(tg_user_id=user.telegram_id or 0)))
    removed = 0
    for member in members:
        api.kick(token(), member.chat_id, member.tg_user_id)
        on_leave(member.chat_id, member.tg_user_id, reason)
        record(member.chat_id, ModerationAction.KICK, target_tg_id=member.tg_user_id, target_user=user,
               reason=reason)
        removed += 1
    if removed:
        logger.info("community revoke user=%s chats=%s reason=%s", user.pk, removed, reason)
    return removed


def revoke_safely(user, reason="not_eligible") -> int:
    """Event hook (application rejected …): queued on Celery, inline fallback;
    never raises into the caller."""
    if not configured() or user is None:
        return 0
    try:
        from ..notifications import _dispatch
        from ..tasks import community_revoke_task

        _dispatch(community_revoke_task, user.pk, reason)
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("community revoke failed user=%s", getattr(user, "pk", None))
        return 0


def sweep() -> int:
    """Hourly: remove members who are no longer eligible (deactivated in the
    admin, volunteer role removed, Telegram unlinked) or never were bound to
    an account. Idempotent."""
    if not configured():
        return 0
    removed = 0
    for member in CommunityMember.objects.filter(left_at__isnull=True).select_related("user"):
        user = member.user
        if user is not None and is_eligible(user) and user.telegram_id == member.tg_user_id:
            continue
        api.kick(token(), member.chat_id, member.tg_user_id)
        on_leave(member.chat_id, member.tg_user_id, "sweep")
        record(member.chat_id, ModerationAction.KICK, target_tg_id=member.tg_user_id, target_user=user,
               reason="sweep")
        removed += 1
    if removed:
        logger.info("community sweep removed=%s", removed)
    return removed


# --------------------------------------------------------------------------- #
# moderation
# --------------------------------------------------------------------------- #

def record(chat_id, action, *, target_tg_id=None, target_user=None, actor=None, reason=""):
    return ModerationAction.objects.create(
        chat_id=chat_id, action=action, target_tg_id=target_tg_id, target_user=target_user,
        actor=actor, reason=(reason or "")[:300],
    )


def is_flooding(chat_id, tg_user_id) -> bool:
    """More than FLOOD_MESSAGES messages in FLOOD_WINDOW_SECONDS."""
    window = int(timezone.now().timestamp() // FLOOD_WINDOW_SECONDS)
    key = f"community:flood:{chat_id}:{tg_user_id}:{window}"
    try:
        cache.add(key, 0, FLOOD_WINDOW_SECONDS + 1)
        count = cache.incr(key)
    except Exception:  # noqa: BLE001 — a cache hiccup never mutes anyone
        return False
    return count > FLOOD_MESSAGES


def warn_count(chat_id, tg_user_id, days=30) -> int:
    since = timezone.now() - timedelta(days=days)
    return ModerationAction.objects.filter(chat_id=chat_id, target_tg_id=tg_user_id,
                                           action=ModerationAction.WARN, created_at__gte=since).count()


def report_to_staff(chat_id, reporter_tg_id, target_tg_id, message_id) -> int:
    """``/report`` (reply) → one in-app notice to staff with a link to the
    message; the message text is never copied anywhere."""
    from ..notifications import staff_recipients
    from .notify import notify

    reporter = user_for_tg(reporter_tg_id)
    record(chat_id, ModerationAction.REPORT, target_tg_id=target_tg_id, target_user=user_for_tg(target_tg_id),
           actor=reporter, reason=f"message:{message_id}")
    link = message_link(chat_id, message_id)
    return notify(
        staff_recipients(), "community_report",
        params={"message_id": message_id},
        link=link, dedupe_key=f"community_report:{chat_id}:{message_id}",
    )


def message_link(chat_id, message_id) -> str:
    """t.me link to a message in a supergroup (-100… ids)."""
    raw = str(chat_id)
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/{message_id}"
    return ""


# --------------------------------------------------------------------------- #
# posts: mirrored broadcasts / events, weekly "thank you"
# --------------------------------------------------------------------------- #

def _post(kind, ref, chat_id, text, *, pin_it=False):
    """Post once per (kind, ref, chat) — the row is claimed before sending, so
    a retry never posts twice."""
    try:
        with transaction.atomic():
            post = CommunityPost.objects.create(kind=kind, ref=str(ref)[:64], chat_id=chat_id)
    except IntegrityError:
        return None
    result = api.send_message(token(), chat_id, text, disable_web_page_preview=True)
    if not result:
        post.delete()  # let a later retry try again
        return None
    CommunityPost.objects.filter(pk=post.pk).update(message_id=result.get("message_id"))
    if pin_it and result.get("message_id"):
        api.pin(token(), chat_id, result["message_id"])
    return result


def _targets(region):
    configured_chats = chats()
    if region and region != "all" and region in configured_chats:
        return [configured_chats[region]]
    return [configured_chats[GENERAL]] if GENERAL in configured_chats else []


def mirror_broadcast(broadcast) -> int:
    """A staff broadcast from the website, pinned in the community (the
    region's group when it targets one region and that group exists)."""
    if not configured():
        return 0
    text = f"📣 {broadcast.subject}\n\n{broadcast.message}"[:3900]
    return sum(1 for chat_id in _targets(broadcast.region)
               if _post(CommunityPost.KIND_BROADCAST, broadcast.pk, chat_id, text, pin_it=True))


def mirror_event(event) -> int:
    if not configured():
        return 0
    when = timezone.localtime(event.date).strftime("%d.%m.%Y %H:%M") if event.date else ""
    text = f"📅 {event.title}\n{when}\n\n{(event.description or '')[:1500]}"
    return sum(1 for chat_id in _targets(getattr(event, "region", ""))
               if _post(CommunityPost.KIND_EVENT, event.pk, chat_id, text, pin_it=True))


def _mirror_safely(kind, obj):
    if not configured() or obj is None:
        return 0
    try:
        from ..notifications import _dispatch
        from ..tasks import community_mirror_task

        _dispatch(community_mirror_task, kind, obj.pk)
        return 1
    except Exception:  # noqa: BLE001
        logger.exception("community mirror failed kind=%s id=%s", kind, getattr(obj, "pk", None))
        return 0


def mirror_broadcast_safely(broadcast):
    return _mirror_safely(CommunityPost.KIND_BROADCAST, broadcast)


def mirror_event_safely(event):
    return _mirror_safely(CommunityPost.KIND_EVENT, event)


def weekly_numbers(now=None) -> dict:
    """The last 7 days, aggregate only (3 queries + 1 for names)."""
    now = now or timezone.now()
    since = now - timedelta(days=7)
    done = HelpRequest.objects.filter(status="completed", completed_at__gte=since, completed_at__lt=now)
    by_region = dict(done.values_list("region").annotate(n=Count("id")).order_by())
    totals = done.aggregate(n=Count("id"), volunteers=Count("volunteer", distinct=True))
    avg = TaskReview.objects.filter(hidden_at__isnull=True, created_at__gte=since, created_at__lt=now) \
        .aggregate(a=Avg("stars"))["a"]
    awards = list(UserAchievement.objects.filter(awarded_at__gte=since, awarded_at__lt=now,
                                                 user__is_active=True)
                  .select_related("user__profile", "achievement").order_by("user_id"))
    named, anonymous = {}, set()
    for row in awards:
        profile = getattr(row.user, "profile", None)
        if profile is not None and profile.community_shoutouts:
            name = profile.full_name or row.user.username
            named.setdefault(name, []).append(row.achievement.code)
        else:
            anonymous.add(row.user_id)
    if len(named) > WEEKLY_NAMED_LIMIT:
        names = sorted(named, key=str.casefold)
        for extra in names[WEEKLY_NAMED_LIMIT:]:
            del named[extra]
        anonymous |= {f"named-overflow:{n}" for n in names[WEEKLY_NAMED_LIMIT:]}
    return {
        "completed": totals["n"] or 0,
        "volunteers": totals["volunteers"] or 0,
        "by_region": {code: by_region.get(code, 0) for code, _ in REGION_CHOICES if by_region.get(code)},
        "avg_rating": round(avg, 1) if avg is not None else None,
        "named": dict(sorted(named.items(), key=lambda kv: kv[0].casefold())),
        "anonymous": len(anonymous),
    }


WEEKLY_TEXT = {
    "ru": {
        "title": "🤍 Спасибо недели",
        "done": "За неделю выполнено запросов: {completed}. Помогали волонтёров: {volunteers}.",
        "regions": "По регионам: {regions}.",
        "rating": "Средняя оценка клиентов: {avg}.",
        "badges": "Новые значки:",
        "anon": "…и ещё волонтёров с новыми значками: {n}.",
        "quiet": "На этой неделе было спокойно — спасибо всем, кто на связи.",
        "footer": "Без мест и рейтингов: каждый запрос — общее дело. Спасибо!",
    },
    "tj": {
        "title": "🤍 Ташаккури ҳафта",
        "done": "Дар як ҳафта дархостҳо иҷро шуданд: {completed}. Ихтиёриён кумак карданд: {volunteers}.",
        "regions": "Аз рӯи минтақаҳо: {regions}.",
        "rating": "Баҳои миёнаи мизоҷон: {avg}.",
        "badges": "Нишонҳои нав:",
        "anon": "…ва боз ихтиёриёни дорои нишони нав: {n}.",
        "quiet": "Ин ҳафта ором гузашт — ташаккур ба ҳамаи онҳое, ки дар тамос ҳастанд.",
        "footer": "Бе ҷой ва рейтинг: ҳар дархост кори умумӣ аст. Ташаккур!",
    },
}


def weekly_text(numbers) -> str:
    """RU, then TJ (the group is bilingual). No client data, no rankings."""
    parts = []
    for lang in ("ru", "tj"):
        t = WEEKLY_TEXT[lang]
        regions = notify_texts.LABELS["region"][lang]
        titles = notify_texts.LABELS["achievement"][lang]
        lines = [t["title"]]
        if numbers["completed"]:
            lines.append(t["done"].format(**numbers))
            if numbers["by_region"]:
                lines.append(t["regions"].format(regions=", ".join(
                    f"{regions.get(code, code)} — {n}" for code, n in numbers["by_region"].items())))
            if numbers["avg_rating"] is not None:
                lines.append(t["rating"].format(avg=numbers["avg_rating"]))
        else:
            lines.append(t["quiet"])
        if numbers["named"] or numbers["anonymous"]:
            lines.append("")
            lines.append(t["badges"])
            for name, codes in numbers["named"].items():
                lines.append(f"• {name}: " + ", ".join(titles.get(code, code) for code in codes))
            if numbers["anonymous"]:
                lines.append(t["anon"].format(n=numbers["anonymous"]))
        lines.append("")
        lines.append(t["footer"])
        parts.append("\n".join(lines))
    return "\n\n———\n\n".join(parts)[:MESSAGE_MAX]


def post_weekly(now=None) -> bool:
    """Post the weekly thank-you once per ISO week to the general chat."""
    if not configured():
        return False
    now = now or timezone.now()
    year, week, _ = timezone.localtime(now).isocalendar()
    text = weekly_text(weekly_numbers(now))
    return bool(_post(CommunityPost.KIND_WEEKLY, f"{year}-W{week:02d}", chats()[GENERAL], text))
