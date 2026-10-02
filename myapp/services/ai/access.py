"""Per-object visibility checks for AI tools.

Thin re-export of ``myapp/services/access.py`` — the one copy of the task /
emergency visibility rules shared by the HTTP views, the WebSocket consumer and
the AI tools (kept as its own module so ``read_tools`` / ``actions`` keep their
``from . import access`` import without touching ``views``).
"""

from __future__ import annotations

from ..access import can_view_emergency, can_view_task

__all__ = ["can_view_task", "can_view_emergency"]
