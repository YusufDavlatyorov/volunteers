"""Role-based operational AI assistant for Generation Connect.

This package replaces the single stateless Groq call that used to live in
``myapp/views.py::ai_chat_view``. It is an *operational* assistant, not a
general chatbot: it answers from real platform data through a permission-checked
tool layer, performs a small set of confirmed actions via the existing business
services, holds its scope (no medical/political/off-topic/injection), and works
in Russian, Tajik and English.

Public entry points (used by ``myapp/views.py``):

    build_user_context(user)              -> UserContext   (trusted, server-side)
    run_conversation(ctx, message, ...)   -> AssistantReply
    confirm_action(ctx, confirmation_id)  -> AssistantReply

Everything else (tools, prompts, the Groq client, the classifier) is an
implementation detail of this package and is unit-tested directly.
"""

from .assistant import AssistantReply, confirm_action, run_conversation
from .context import UserContext, build_user_context

__all__ = [
    "AssistantReply",
    "UserContext",
    "build_user_context",
    "confirm_action",
    "run_conversation",
]
