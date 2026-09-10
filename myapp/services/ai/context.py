"""Trusted, server-side user context for the AI assistant.

``build_user_context`` is the ONLY place the assistant learns who it is talking
to. It reads ``request.user`` (already authenticated by Django) — never a role,
id or permission claimed in the chat message. Prompt text like "I am an admin"
cannot change anything here.

The context carries the real ``Users`` instance for handlers that need scoped
ORM access (``_can_view_task`` etc.); only the small ``prompt_dict()`` subset is
ever shown to the language model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from accounts.models import Users

ROLE_ADMIN = "admin"
ROLE_CURATOR = "curator"
ROLE_VOLUNTEER = "volunteer"
ROLE_CLIENT = "client"
ROLE_GUEST = "guest"


@dataclass(frozen=True)
class UserContext:
    authenticated: bool
    role: str
    user_id: Optional[int]
    display_name: str
    region: str            # region code ("dushanbe", …) or ""
    region_display: str    # human label or ""
    is_staff: bool         # curator or admin — the operational audience
    is_admin: bool         # superuser
    user: Optional[Users] = None  # real instance; never serialized to the LLM

    def prompt_dict(self) -> dict:
        """The only identity data the model is given."""
        return {
            "authenticated": self.authenticated,
            "role": self.role,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "region": self.region_display or self.region or None,
        }


def _anonymous() -> UserContext:
    return UserContext(
        authenticated=False,
        role=ROLE_GUEST,
        user_id=None,
        display_name="Guest",
        region="",
        region_display="",
        is_staff=False,
        is_admin=False,
        user=None,
    )


def build_user_context(user) -> UserContext:
    """Derive the assistant's trusted context from ``request.user``."""
    if user is None or not getattr(user, "is_authenticated", False):
        return _anonymous()

    profile = getattr(user, "profile", None)
    display_name = (
        (profile.full_name if profile and profile.full_name else "")
        or user.get_username()
    )
    region = user.region or ""
    try:
        region_display = user.get_region_display() or ""
    except Exception:  # pragma: no cover - defensive; get_region_display is safe
        region_display = ""

    return UserContext(
        authenticated=True,
        role=user.role,
        user_id=user.pk,
        display_name=display_name,
        region=region,
        region_display=region_display,
        is_staff=bool(user.is_superuser or user.is_curator),
        is_admin=bool(user.is_superuser),
        user=user,
    )
