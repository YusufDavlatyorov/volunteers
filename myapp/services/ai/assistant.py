"""Orchestration: the classifier gate, the tool loop, and the confirmation flow.

``run_conversation`` and ``confirm_action`` are the only functions the view
calls. Both return an ``AssistantReply`` and never raise — a provider or tool
failure degrades to a localized fallback string.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import client, confirmations, prompts, tools
from .context import UserContext
from .policies import classify_message

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4
HISTORY_MAX_TURNS = 8
HISTORY_MAX_CHARS = 6000
_MSG_CLIP = 2000


@dataclass
class AssistantReply:
    text: str
    pending_action: dict | None = None   # {"id", "tool", "summary"}
    action_result: dict | None = None
    meta: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _bounded_history(history) -> list[dict]:
    if not isinstance(history, list):
        return []
    cleaned = []
    for entry in history[-HISTORY_MAX_TURNS * 2:]:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        cleaned.append({"role": role, "content": content.strip()[:_MSG_CLIP]})
    cleaned = cleaned[-HISTORY_MAX_TURNS:]
    # trim from the front until under the char budget
    while cleaned and sum(len(m["content"]) for m in cleaned) > HISTORY_MAX_CHARS:
        cleaned.pop(0)
    return cleaned


def _tool_message(tool_call_id: str, payload: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload, ensure_ascii=False)[:6000],
    }


_ACTION_OK_TEXT = {
    "create_help_request": {
        "ru": "Готово. Заявка #{request_id} создана и волонтёры вашего региона уведомлены.",
        "tj": "Тайёр. Дархости #{request_id} сохта шуд ва волонтёрони минтақаи шумо огоҳ карда шуданд.",
        "en": "Done. Request #{request_id} was created and volunteers in your region were notified.",
    },
    "escalate_task_to_staff": {
        "ru": "Готово. Запрос #{request_id} передан координаторам на рассмотрение.",
        "tj": "Тайёр. Дархости #{request_id} ба ҳамоҳангсозон барои баррасӣ фиристода шуд.",
        "en": "Done. Request #{request_id} was escalated to the coordinators.",
    },
    "notify_volunteer_about_task": {
        "ru": "Готово. Рекомендация по запросу #{request_id} отправлена волонтёру.",
        "tj": "Тайёр. Тавсия оид ба дархости #{request_id} ба волонтёр фиристода шуд.",
        "en": "Done. The recommendation for request #{request_id} was sent to the volunteer.",
    },
    "advance_work_stage": {
        "ru": "Готово. Этап запроса #{request_id} обновлён: {work_stage}.",
        "tj": "Тайёр. Марҳилаи дархости #{request_id} нав шуд: {work_stage}.",
        "en": "Done. Request #{request_id} work stage is now: {work_stage}.",
    },
}


def _compose_action_text(tool: str, result: dict, lang: str) -> str:
    if not result.get("success"):
        return prompts.tool_error_text(lang)
    templates = _ACTION_OK_TEXT.get(tool)
    if not templates:
        return {"ru": "Готово.", "tj": "Тайёр.", "en": "Done."}.get(lang, "Готово.")
    text = templates.get(lang) or templates["ru"]
    try:
        return text.format(**result)
    except (KeyError, IndexError):
        return text


# --------------------------------------------------------------------------- #
# main entry points
# --------------------------------------------------------------------------- #

def run_conversation(ctx: UserContext, message: str, history=None, lang: str = "ru") -> AssistantReply:
    message = (message or "").strip()
    if not message:
        return AssistantReply(text=prompts.fallback_text(lang))

    if not ctx.authenticated:
        return AssistantReply(text=prompts.refusal_text("off_topic", lang))

    kind = classify_message(message)
    if kind:
        logger.info("ai: message classified '%s' for user #%s (no Groq call)", kind, ctx.user_id)
        return AssistantReply(text=prompts.refusal_text(kind, lang), meta={"classified": kind})

    allowed = tools.allowed_tool_names(ctx)
    schemas = tools.schemas_for(allowed)
    messages = (
        [{"role": "system", "content": prompts.system_prompt(ctx, lang)}]
        + _bounded_history(history)
        + [{"role": "user", "content": message[:_MSG_CLIP]}]
    )

    tools_used: list[str] = []

    for _round in range(MAX_TOOL_ROUNDS):
        result = client.chat(messages, tools=schemas)
        if not result.ok:
            logger.warning("ai: provider error '%s' for user #%s", result.error, ctx.user_id)
            return AssistantReply(text=prompts.fallback_text(lang),
                                  meta={"provider_error": result.error})

        messages.append(result.assistant_message or {"role": "assistant", "content": result.text})

        if not result.tool_calls:
            return AssistantReply(text=result.text or prompts.fallback_text(lang),
                                  meta={"tools_used": tools_used})

        pending = None
        for call in result.tool_calls:
            tools_used.append(call.name)
            outcome = tools.dispatch(call.name, call.arguments, ctx)
            confirmation = outcome.get("confirmation") if outcome.get("success") else None
            if confirmation:
                cid = confirmations.stash(
                    ctx, confirmation["tool"], confirmation["clean_args"], confirmation["summary"]
                )
                pending = {"id": cid, "tool": confirmation["tool"], "summary": confirmation["summary"]}
                logger.info("ai: action '%s' staged for confirmation (user #%s)",
                            confirmation["tool"], ctx.user_id)
                messages.append(_tool_message(
                    call.id,
                    {"status": "confirmation_required", "summary": confirmation["summary"]},
                ))
            else:
                messages.append(_tool_message(call.id, outcome))

        if pending:
            closing = client.chat(messages)  # ask the model to phrase the confirm question
            text = closing.text if closing.ok and closing.text else pending["summary"]
            return AssistantReply(text=text, pending_action=pending,
                                  meta={"tools_used": tools_used})

    # loop exhausted — force one final answer without tools
    final = client.chat(messages)
    return AssistantReply(
        text=(final.text if final.ok and final.text else prompts.fallback_text(lang)),
        meta={"tools_used": tools_used, "rounds_exhausted": True},
    )


def confirm_action(ctx: UserContext, confirmation_id: str, lang: str = "ru") -> AssistantReply:
    if not ctx.authenticated:
        return AssistantReply(text=prompts.refusal_text("off_topic", lang))

    payload = confirmations.take(ctx, confirmation_id)
    if payload is None:
        return AssistantReply(text=prompts.confirmation_expired_text(lang),
                              meta={"confirmation": "expired"})

    tool = payload["tool"]
    result = tools.execute_action(tool, payload.get("clean_args", {}), ctx)

    # Try to phrase the outcome naturally; fall back to a deterministic template.
    text = _compose_action_text(tool, result, lang)
    phrasing = client.chat([
        {"role": "system", "content": prompts.system_prompt(ctx, lang)},
        {"role": "user", "content": f"I confirmed this action: {payload['summary']}"},
        {"role": "assistant", "content": "The system executed it."},
        {"role": "user", "content": (
            f"Execution result JSON: {json.dumps(result, ensure_ascii=False)}. "
            f"In one or two sentences, in my language, tell me what happened. "
            f"If it failed, say so plainly."
        )},
    ])
    if phrasing.ok and phrasing.text:
        text = phrasing.text

    return AssistantReply(text=text, action_result=result,
                          meta={"confirmed_tool": tool, "success": result.get("success")})
