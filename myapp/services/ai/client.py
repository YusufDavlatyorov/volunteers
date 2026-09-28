"""Thin Groq (OpenAI-compatible) chat client for the assistant.

Same discipline as ``services/geo.py`` and ``services/maps.py``: pure I/O, a
finite timeout, and it NEVER raises for an expected failure — every problem
comes back as ``ChatResult(ok=False, error=...)`` so a broken or unconfigured
provider degrades to the canned fallback instead of 500-ing the chat view.

Provider and dependency are unchanged from the previous implementation: Groq's
``/openai/v1/chat/completions`` over ``requests``. The only addition is
``GROQ_ASSISTANT_MODEL`` (a tool-capable default) with a one-shot retry on the
older ``GROQ_MODEL``.

Tests patch ``myapp.services.ai.client.requests.post``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_TIMEOUT = (5, 20)  # (connect, read) — bound how long one call can pin a worker
_MAX_TOKENS = 900
_TEMPERATURE = 0.3


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ChatResult:
    ok: bool
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_message: dict | None = None  # raw message dict, for the tool loop
    error: str | None = None


def _assistant_models() -> list[str]:
    primary = getattr(settings, "GROQ_ASSISTANT_MODEL", "") or "llama-3.3-70b-versatile"
    fallback = getattr(settings, "GROQ_MODEL", "") or "llama-3.1-8b-instant"
    return [primary] if primary == fallback else [primary, fallback]


def _parse_choice(data: dict) -> ChatResult:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return ChatResult(ok=False, error="malformed_response")

    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") or {}
        name = fn.get("name") or ""
        try:
            args = json.loads(fn.get("arguments") or "{}")
            if not isinstance(args, dict):
                args = {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("ai.client: unparseable tool arguments for %s", name)
            args = {}
        calls.append(ToolCall(id=raw.get("id") or name, name=name, arguments=args))

    return ChatResult(
        ok=True,
        text=(message.get("content") or "").strip(),
        tool_calls=calls,
        assistant_message=message,
    )


def chat(messages: list[dict], tools: list[dict] | None = None) -> ChatResult:
    """One round-trip to Groq. Returns a ChatResult; never raises."""
    api_key = getattr(settings, "GROQ_API_KEY", "")
    if not api_key:
        return ChatResult(ok=False, error="no_api_key")

    payload: dict = {
        "messages": messages,
        "temperature": _TEMPERATURE,
        "max_completion_tokens": _MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    last_error = "provider_error"
    for model in _assistant_models():
        try:
            response = requests.post(
                _ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={**payload, "model": model},
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
            result = _parse_choice(response.json())
            if result.ok:
                return result
            last_error = result.error or "malformed_response"
        except (requests.RequestException, ValueError) as exc:
            last_error = "provider_error"
            logger.warning("ai.client: Groq call failed (model=%s): %s", model, exc)

    return ChatResult(ok=False, error=last_error)
