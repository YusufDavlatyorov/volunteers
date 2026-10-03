"""python-telegram-bot adapter for the main bot (Feature Pack v2, feature 4).

Thin by design: turn a PTB ``Update`` into a ``core.Event``, run
``core.handle`` in a worker thread (Django ORM is sync), and render the
``core.Result`` back — inline keyboards, reply keyboards for location /
contact requests, edits of the message a button was pressed on, a toast for
the button press. No business logic here.

Long polling (not a webhook) — documented choice: one process, no public
endpoint to expose and secure, and it copes with the server's slow uplink
(Telegram holds the request open; nothing has to reach us inbound).
"""

from __future__ import annotations

import logging

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from ..i18n import t
from . import core

logger = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 5 * 1024 * 1024
ALLOWED_UPDATES = ["message", "edited_message", "callback_query"]


def _handle_sync(event):
    """core.handle in the bot's DB thread. A long-lived process must drop
    connections the database closed meanwhile (CONN_MAX_AGE / restarts)."""
    close_old_connections()
    try:
        return core.handle(event)
    finally:
        close_old_connections()


run_core = sync_to_async(_handle_sync, thread_sensitive=True)


def _markup(reply: core.Reply):
    if reply.remove_keyboard:
        return ReplyKeyboardRemove()
    if reply.keyboard:
        rows = [[KeyboardButton(b.label, request_location=b.request == "location",
                                request_contact=b.request == "contact") for b in row] for row in reply.keyboard]
        return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)
    if reply.buttons:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(b.label, url=b.url) if b.url else InlineKeyboardButton(b.label, callback_data=b.data)
             for b in row] for row in reply.buttons if row
        ])
    return None


async def _send(bot, chat_id, reply: core.Reply, source_message=None):
    markup = _markup(reply)
    if reply.edit and source_message is not None and (markup is None or isinstance(markup, InlineKeyboardMarkup)):
        try:
            await source_message.edit_text(reply.text, reply_markup=markup, disable_web_page_preview=True)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            # too old / not editable → fall through to a fresh message
    await bot.send_message(chat_id, reply.text, reply_markup=markup, disable_web_page_preview=True)


async def render(bot, chat_id, result: core.Result, source_message=None):
    for reply in result.replies:
        await _send(bot, chat_id, reply, source_message)
    if result.previous_chat_notice:
        try:
            await bot.send_message(result.previous_chat_notice, t("ru", "link.detached"))
        except TelegramError:
            pass


def event_from_message(update: Update, photo: bytes | None = None, photo_too_large=False) -> core.Event | None:
    message = update.message or update.edited_message
    if message is None or message.chat is None:
        return None
    chat = message.chat
    base = dict(chat_id=chat.id, is_private=chat.type == "private")
    if message.location is not None:
        loc = message.location
        return core.Event(kind="location", lat=loc.latitude, lng=loc.longitude,
                          accuracy=loc.horizontal_accuracy, live=update.edited_message is not None, **base)
    if message.contact is not None:
        return core.Event(kind="contact", contact_phone=message.contact.phone_number or "",
                          contact_user_id=message.contact.user_id, **base)
    if message.photo:
        return core.Event(kind="photo", text=message.caption or "", photo=photo,
                          photo_too_large=photo_too_large, **base)
    text = message.text or ""
    if not text:
        return None
    kind = "command" if text.startswith("/") else "text"
    return core.Event(kind=kind, text=text, **base)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if message is None:
        return
    if update.edited_message is not None and message.location is None:
        return  # an edited text is not a new command
    photo, too_large = None, False
    if message.photo and message.chat.type == "private":
        largest = message.photo[-1]
        if (largest.file_size or 0) > MAX_PHOTO_BYTES:
            too_large = True
        else:
            file = await context.bot.get_file(largest.file_id)
            photo = bytes(await file.download_as_bytearray())
    event = event_from_message(update, photo, too_large)
    if event is None:
        return
    result = await run_core(event)
    await render(context.bot, event.chat_id, result)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query is None or query.message is None:
        return
    chat = query.message.chat
    event = core.Event(chat_id=chat.id, kind="callback", data=query.data or "", is_private=chat.type == "private")
    result = await run_core(event)
    try:
        await query.answer(result.toast or None)
    except TelegramError:
        pass
    await render(context.bot, chat.id, result, source_message=query.message)


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    # Type only — the exception text may quote user input.
    logger.error("telegram main bot error: %s", type(context.error).__name__,
                 exc_info=not isinstance(context.error, TelegramError))


def build(token, **builder_options) -> Application:
    builder = Application.builder().token(token)
    for name, value in builder_options.items():
        builder = getattr(builder, name)(value)
    app = builder.build()
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.COMMAND | filters.LOCATION | filters.CONTACT | filters.PHOTO)
        & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
        on_message,
    ))
    app.add_error_handler(on_error)
    return app
