"""Community bot — transport-agnostic core (Feature Pack v2, feature 5).

Input: a member joined / left a community chat, a message in a community
chat, or a private message to the bot. Output: a list of ``Action``s the
python-telegram-bot adapter executes (reply, delete, restrict, ban, kick).
All rules come from ``services/community.py`` (eligibility, invite binding,
moderation log, reports) and ``services/achievements.award_manual``.

Group texts are bilingual (RU, then TJ) — one community, two languages.
Moderation: links from non-staff are deleted, a flood (> 5 messages / 10 s)
mutes for 10 min; staff reply to a message with ``/warn`` (3 within 30 days →
24 h mute), ``/mute [minutes]``, ``/ban``, ``/mentor``; anyone may ``/report``.
Messages from people who aren't eligible members are removed with their
author (the community is approved volunteers + coordinators only).
Logs/records carry IDs only — never message text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ...models import ModerationAction
from ...services import achievements, community

logger = logging.getLogger(__name__)

WARN_LIMIT = 3
WARN_MUTE_MINUTES = 24 * 60


@dataclass
class Action:
    kind: str                 # reply | delete | restrict | ban | kick
    chat_id: int
    text: str = ""
    message_id: int | None = None     # delete: this message; reply: reply to it
    user_id: int | None = None
    minutes: int = 0


@dataclass
class GroupMessage:
    chat_id: int
    message_id: int
    from_id: int | None
    text: str = ""
    has_link: bool = False
    is_bot: bool = False
    via_sender_chat: bool = False     # anonymous admin / channel post
    reply_to_message_id: int | None = None
    reply_to_user_id: int | None = None


TEXTS = {
    "welcome": "Добро пожаловать в сообщество волонтёров KhayrKhoh, {name}! 🤍\n"
               "Ба ҷамъияти ихтиёриёни KhayrKhoh хуш омадед, {name}! 🤍\n\n{rules}",
    "rules": "Правила / Қоидаҳо:\n"
             "1. Уважение к каждому · Эҳтиром ба ҳар кас.\n"
             "2. Никаких данных клиентов (адресов, телефонов, фото) · Ҳеҷ гуна маълумоти мизоҷон (суроға, телефон, сурат).\n"
             "3. Ссылки публикуют только координаторы · Истинодҳоро танҳо ҳамоҳангсозон мегузоранд.\n"
             "4. Без рекламы и флуда · Бе реклама ва паёмҳои пай дар пай.\n"
             "Жалоба: ответьте на сообщение командой /report · Шикоят: ба паём бо фармони /report ҷавоб диҳед.",
    "help": "Команды / Фармонҳо: /rules — правила / қоидаҳо, /report — жалоба (ответом на сообщение) / шикоят (дар ҷавоби паём).\n"
            "Координаторам / Ба ҳамоҳангсозон: /warn, /mute [мин], /ban, /mentor — ответом на сообщение / дар ҷавоби паём.",
    "link_removed": "Ссылки в чате публикуют только координаторы. · Истинодҳоро дар чат танҳо ҳамоҳангсозон мегузоранд.",
    "flood_muted": "Слишком много сообщений подряд — пауза {minutes} мин. · Паёмҳо аз ҳад зиёд — танаффус {minutes} дақ.",
    "reported": "Спасибо, координаторы посмотрят. · Ташаккур, ҳамоҳангсозон медиданд.",
    "reply_needed": "Ответьте этой командой на сообщение. · Бо ин фармон ба паём ҷавоб диҳед.",
    "staff_only": "Эта команда — для координаторов. · Ин фармон барои ҳамоҳангсозон аст.",
    "warned": "Предупреждение ({count}/{limit}). · Огоҳӣ ({count}/{limit}).",
    "warn_muted": "Третье предупреждение — запрет писать на 24 ч. · Огоҳии сеюм — манъи навиштан барои 24 соат.",
    "muted": "Запрет писать на {minutes} мин. · Манъи навиштан барои {minutes} дақ.",
    "banned": "Участник удалён из сообщества. · Иштирокчӣ аз ҷамъият хориҷ карда шуд.",
    "mentor_ok": "Значок «Наставник» выдан: {name}. Спасибо за помощь новичкам! · Нишони «Мураббӣ» дода шуд: {name}. Ташаккур барои кумак ба навомадагон!",
    "mentor_already": "У {name} уже есть значок «Наставник». · {name} аллакай нишони «Мураббӣ» дорад.",
    "mentor_invalid": "Значок можно выдать только волонтёру с привязанным аккаунтом. · Нишонро танҳо ба ихтиёрии дорои ҳисоби пайвастшуда додан мумкин аст.",
    "private": "Это бот сообщества волонтёров KhayrKhoh. Приглашение (одноразовая ссылка) можно получить на сайте в профиле или в основном боте → «Сообщество волонтёров».\n\n"
               "Ин боти ҷамъияти ихтиёриёни KhayrKhoh аст. Даъватномаро (истиноди якдафъаина) дар сайт дар профил ё дар боти асосӣ → «Ҷамъияти ихтиёриён» гирифтан мумкин аст.",
}


def _t(key, **params):
    text = TEXTS[key]
    return text.format(**params) if params else text


def _name(user, fallback=""):
    if user is None:
        return fallback or "—"
    profile = getattr(user, "profile", None)
    return (getattr(profile, "full_name", "") or user.username) if profile is not None else user.username


# --------------------------------------------------------------------------- #
# membership
# --------------------------------------------------------------------------- #

JOINED = {"member", "restricted", "administrator"}
GONE = {"left", "kicked"}


def on_member(chat_id, tg_user_id, old_status, new_status, invite_link=None, first_name="") -> list:
    if not community.is_community_chat(chat_id) or not tg_user_id:
        return []
    if new_status in JOINED and old_status in GONE | {None, ""}:
        decision = community.on_join(chat_id, tg_user_id, invite_link)
        if not decision.allowed:
            return [Action("kick", chat_id, user_id=tg_user_id)]
        return [Action("reply", chat_id, text=_t("welcome", name=_name(decision.user, first_name), rules=_t("rules")))]
    if new_status in GONE and old_status in JOINED:
        community.on_leave(chat_id, tg_user_id, "kicked" if new_status == "kicked" else "left")
    return []


# --------------------------------------------------------------------------- #
# group messages
# --------------------------------------------------------------------------- #

def _command(text):
    if not text or not text.startswith("/"):
        return "", ""
    head, _, rest = text.partition(" ")
    return head.split("@", 1)[0].lower(), rest.strip()


STAFF_COMMANDS = {"/warn", "/mute", "/ban", "/mentor"}


def on_message(msg: GroupMessage) -> list:
    chat_id = msg.chat_id
    command, args = _command(msg.text)
    if command == "/id" and not msg.is_bot:
        # Setup aid (docs/COMMUNITY_BOT.md): works before the chat is configured.
        return [Action("reply", chat_id, text=f"chat id: {chat_id}", message_id=msg.message_id)]
    if not community.is_community_chat(chat_id) or msg.is_bot or msg.via_sender_chat or not msg.from_id:
        return []
    staff = community.staff_for_tg(msg.from_id)

    if staff is None:
        member = community.user_for_tg(msg.from_id)
        if member is None or not community.is_eligible(member):
            community.record(chat_id, ModerationAction.KICK, target_tg_id=msg.from_id, reason="not_member")
            return [Action("delete", chat_id, message_id=msg.message_id), Action("kick", chat_id, user_id=msg.from_id)]

    if command in ("/rules", "/start"):
        return [Action("reply", chat_id, text=_t("rules"), message_id=msg.message_id)]
    if command == "/help":
        return [Action("reply", chat_id, text=_t("help"), message_id=msg.message_id)]
    if command == "/report":
        if not msg.reply_to_message_id:
            return [Action("reply", chat_id, text=_t("reply_needed"), message_id=msg.message_id)]
        community.report_to_staff(chat_id, msg.from_id, msg.reply_to_user_id, msg.reply_to_message_id)
        return [Action("delete", chat_id, message_id=msg.message_id), Action("reply", chat_id, text=_t("reported"))]
    if command in STAFF_COMMANDS:
        if staff is None:
            return [Action("delete", chat_id, message_id=msg.message_id),
                    Action("reply", chat_id, text=_t("staff_only"))]
        return _staff_command(staff, msg, command, args)

    if staff is not None:
        return []  # coordinators may post links and are never flood-limited
    if community.is_flooding(chat_id, msg.from_id):
        community.record(chat_id, ModerationAction.MUTE, target_tg_id=msg.from_id,
                         target_user=community.user_for_tg(msg.from_id), reason="flood")
        return [Action("restrict", chat_id, user_id=msg.from_id, minutes=community.FLOOD_MUTE_MINUTES),
                Action("reply", chat_id, text=_t("flood_muted", minutes=community.FLOOD_MUTE_MINUTES))]
    if msg.has_link:
        community.record(chat_id, ModerationAction.DELETE, target_tg_id=msg.from_id,
                         target_user=community.user_for_tg(msg.from_id), reason="link")
        return [Action("delete", chat_id, message_id=msg.message_id), Action("reply", chat_id, text=_t("link_removed"))]
    return []


def _staff_command(staff, msg, command, args):
    chat_id = msg.chat_id
    target_tg = msg.reply_to_user_id
    if not msg.reply_to_message_id or not target_tg:
        return [Action("reply", chat_id, text=_t("reply_needed"), message_id=msg.message_id)]
    target_user = community.user_for_tg(target_tg)
    done = [Action("delete", chat_id, message_id=msg.message_id)]
    if command == "/warn":
        community.record(chat_id, ModerationAction.WARN, target_tg_id=target_tg, target_user=target_user, actor=staff,
                         reason="staff_warn")
        count = community.warn_count(chat_id, target_tg)
        if count >= WARN_LIMIT:
            community.record(chat_id, ModerationAction.MUTE, target_tg_id=target_tg, target_user=target_user,
                             actor=staff, reason="warn_limit")
            return done + [Action("restrict", chat_id, user_id=target_tg, minutes=WARN_MUTE_MINUTES),
                           Action("reply", chat_id, text=_t("warn_muted"), message_id=msg.reply_to_message_id)]
        return done + [Action("reply", chat_id, text=_t("warned", count=count, limit=WARN_LIMIT),
                              message_id=msg.reply_to_message_id)]
    if command == "/mute":
        try:
            minutes = int(args.split()[0]) if args else community.DEFAULT_MUTE_MINUTES
        except ValueError:
            minutes = community.DEFAULT_MUTE_MINUTES
        minutes = max(1, min(minutes, community.MAX_MUTE_MINUTES))
        community.record(chat_id, ModerationAction.MUTE, target_tg_id=target_tg, target_user=target_user, actor=staff,
                         reason=f"staff_mute:{minutes}")
        return done + [Action("restrict", chat_id, user_id=target_tg, minutes=minutes),
                       Action("reply", chat_id, text=_t("muted", minutes=minutes))]
    if command == "/ban":
        community.record(chat_id, ModerationAction.BAN, target_tg_id=target_tg, target_user=target_user, actor=staff,
                         reason="staff_ban")
        community.on_leave(chat_id, target_tg, "ban")
        return done + [Action("delete", chat_id, message_id=msg.reply_to_message_id),
                       Action("ban", chat_id, user_id=target_tg), Action("reply", chat_id, text=_t("banned"))]
    if command == "/mentor":
        row, code = achievements.award_manual(staff, target_user, "mentor")
        if code in ("ok", "already"):
            if code == "ok":
                community.record(chat_id, ModerationAction.MENTOR, target_tg_id=target_tg, target_user=target_user,
                                 actor=staff, reason="mentor")
            return done + [Action("reply", chat_id, text=_t(f"mentor_{code}", name=_name(target_user)))]
        return done + [Action("reply", chat_id, text=_t("mentor_invalid"))]
    return done


def on_private(chat_id, text) -> list:
    command, _ = _command(text)
    if command == "/id":
        return [Action("reply", chat_id, text=str(chat_id))]
    return [Action("reply", chat_id, text=_t("private"))]
