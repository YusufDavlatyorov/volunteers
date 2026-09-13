"""Server-issued, single-use confirmation tokens for AI actions.

The language model never gets to execute an action. When it calls an action
tool, the validated payload is stashed here under a random id that is bound to
the user; the id goes to the frontend, and the action runs only when the user
POSTs that id back. Cache-backed (``django.core.cache``) with a short TTL, so it
works with the project's LocMemCache in dev and a shared cache in prod.
"""

from __future__ import annotations

import secrets

from django.core.cache import cache

TTL_SECONDS = 5 * 60
_PREFIX = "ai_pending_action"


def _key(user_id, confirmation_id: str) -> str:
    return f"{_PREFIX}:{user_id}:{confirmation_id}"


def stash(ctx, tool: str, clean_args: dict, summary: str) -> str:
    """Store a validated pending action; return its confirmation id."""
    confirmation_id = secrets.token_urlsafe(18)
    cache.set(
        _key(ctx.user_id, confirmation_id),
        {"tool": tool, "clean_args": clean_args, "summary": summary, "user_id": ctx.user_id},
        TTL_SECONDS,
    )
    return confirmation_id


def take(ctx, confirmation_id: str) -> dict | None:
    """Pop a pending action if it exists, belongs to this user, and is unexpired.
    Single-use: it is deleted on read."""
    if not confirmation_id:
        return None
    key = _key(ctx.user_id, confirmation_id)
    payload = cache.get(key)
    if payload is None:
        return None
    cache.delete(key)
    if payload.get("user_id") != ctx.user_id:
        return None
    return payload
