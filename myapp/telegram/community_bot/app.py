"""python-telegram-bot adapter for the community bot (Feature Pack v2, feature 5).

Turns ``chat_member`` updates and group / private messages into
``core`` calls (run in the bot's DB thread) and executes the returned
``Action``s. The bot must be an admin of each community chat with the rights
"delete messages", "ban users", "invite users via link" and "pin messages"
(docs/COMMUNITY_BOT.md); ``chat_member`` updates are requested explicitly.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.utils import timezone
from telegram import ChatPermissions, MessageEntity, Update
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, ContextTypes, MessageHandler, filters

from . import core

logger = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "chat_member"]
LINK_ENTITIES = {MessageEntity.URL, MessageEntity.TEXT_LINK}


def _sync(fn):
    def run(*args, **kwargs):
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()
    return sync_to_async(run, thread_sensitive=True)


on_member_core = _sync(core.on_member)
on_message_core = _sync(core.on_message)
on_private_core = _sync(core.on_private)


async def execute(bot, actions):
    for action in actions:
        try:
            if action.kind == "reply":
                await bot.send_message(action.chat_id, action.text, reply_to_message_id=action.message_id,
                                       disable_web_page_preview=True, allow_sending_without_reply=True)
            elif action.kind == "delete":
                await bot.delete_message(action.chat_id, action.message_id)
            elif action.kind == "restrict":
                await bot.restrict_chat_member(
                    action.chat_id, action.user_id, ChatPermissions(can_send_messages=False),
                    until_date=timezone.now() + timedelta(minutes=action.minutes))
            elif action.kind == "ban":
                await bot.ban_chat_member(action.chat_id, action.user_id)
            elif action.kind == "kick":
                await bot.ban_chat_member(action.chat_id, action.user_id)
                await bot.unban_chat_member(action.chat_id, action.user_id, only_if_banned=True)
        except TelegramError as exc:
            logger.warning("community action %s failed: %s", action.kind, type(exc).__name__)


def has_link(message) -> bool:
    entities = list(message.entities or []) + list(message.caption_entities or [])
    if any(e.type in LINK_ENTITIES for e in entities):
        return True
    text = (message.text or message.caption or "").lower()
    return "t.me/" in text or "http://" in text or "https://" in text or "www." in text


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    change = update.chat_member
    if change is None:
        return
    invite = change.invite_link.invite_link if change.invite_link else None
    member = change.new_chat_member.user
    actions = await on_member_core(change.chat.id, member.id, change.old_chat_member.status,
                                   change.new_chat_member.status, invite, member.first_name or "")
    await execute(context.bot, actions)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None or message.chat is None:
        return
    if message.chat.type == "private":
        await execute(context.bot, await on_private_core(message.chat.id, message.text or ""))
        return
    if message.new_chat_members or message.left_chat_member:
        return  # service messages; membership comes from chat_member updates
    reply = message.reply_to_message
    msg = core.GroupMessage(
        chat_id=message.chat.id, message_id=message.message_id,
        from_id=message.from_user.id if message.from_user else None,
        text=message.text or message.caption or "", has_link=has_link(message),
        is_bot=bool(message.from_user and message.from_user.is_bot),
        via_sender_chat=message.sender_chat is not None,
        reply_to_message_id=reply.message_id if reply else None,
        reply_to_user_id=reply.from_user.id if reply and reply.from_user else None,
    )
    await execute(context.bot, await on_message_core(msg))


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    logger.error("community bot error: %s", type(context.error).__name__,
                 exc_info=not isinstance(context.error, TelegramError))


def build(token, **builder_options) -> Application:
    builder = Application.builder().token(token)
    for name, value in builder_options.items():
        builder = getattr(builder, name)(value)
    app = builder.build()
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ALL & filters.UpdateType.MESSAGE, on_message))
    app.add_error_handler(on_error)
    return app
