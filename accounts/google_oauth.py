"""Sign in with Google ("Continue with Google") — OpenID Connect authorization-code flow, no new dependency.

The flow (views in accounts/views.py):

1. ``google_login_view`` stores a random ``state`` + ``nonce`` + PKCE verifier in
   the session and redirects to Google (:func:`authorization_url`).
2. Google sends the browser back to ``/auth/google/callback/?code=…&state=…``.
   ``google_callback_view`` checks ``state`` against the session (CSRF on the
   login itself), then :func:`exchange_code` swaps the code for an ID token
   server-to-server and :func:`verify_id_token` checks its claims.
3. The verified identity (:class:`GoogleIdentity`) is matched to an account:
   by ``Users.google_sub`` first, then by a verified email; otherwise the user
   finishes a short sign-up form (role / username / region).

Why the ID token's signature is not checked against Google's JWKS: it comes
straight from Google's token endpoint over TLS in response to our own
client-secret-authenticated request, which OpenID Connect Core §3.1.3.7 allows
to stand in for signature validation. Every claim that matters (iss, aud, exp,
nonce, email_verified) is still checked.

Like the other outbound clients (geo, maps, AI), nothing here raises for an
expected failure: :func:`exchange_code` returns ``None`` and logs one line
without the response body or the exception text (which can carry the code).
"""

import base64
import binascii
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
VALID_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
TOKEN_TIMEOUT_SECONDS = 10
# Allowed clock difference between this server and Google when checking exp/iat.
CLOCK_SKEW_SECONDS = 120

# Session keys. The "pending" identity lives only until the sign-up form is
# submitted (or the session ends) — it is never written to the database
# before the user has picked a role.
SESSION_FLOW_KEY = "google_oauth_flow"
SESSION_PENDING_KEY = "google_oauth_pending"
# How long a started login may take before the callback is refused.
FLOW_TTL_SECONDS = 10 * 60
PENDING_TTL_SECONDS = 30 * 60


def is_enabled():
    return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    email_verified: bool
    name: str = ""

    def to_session(self):
        return {"sub": self.sub, "email": self.email, "email_verified": self.email_verified,
                "name": self.name, "at": int(time.time())}

    @classmethod
    def from_session(cls, data):
        if not isinstance(data, dict) or not data.get("sub") or not data.get("email"):
            return None
        if int(time.time()) - int(data.get("at", 0)) > PENDING_TTL_SECONDS:
            return None
        return cls(sub=data["sub"], email=data["email"],
                   email_verified=bool(data.get("email_verified")), name=data.get("name", ""))


def _b64url(raw_bytes):
    return base64.urlsafe_b64encode(raw_bytes).rstrip(b"=").decode("ascii")


def start_flow(session, next_url=None):
    """Mint state/nonce/PKCE for one login attempt and keep them in the session
    (with the already-checked ``next`` page to land on afterwards)."""
    flow = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "verifier": secrets.token_urlsafe(64),
        "at": int(time.time()),
        "next": next_url,
    }
    session[SESSION_FLOW_KEY] = flow
    return flow


def pop_flow(session, state):
    """Consume the stored flow if ``state`` matches and it hasn't expired.

    Always removes it — a state value is single-use, so a replayed callback
    URL (browser back button, a leaked link) fails cleanly."""
    flow = session.pop(SESSION_FLOW_KEY, None)
    if not isinstance(flow, dict) or not state:
        return None
    if not secrets.compare_digest(str(flow.get("state", "")), str(state)):
        return None
    if int(time.time()) - int(flow.get("at", 0)) > FLOW_TTL_SECONDS:
        return None
    return flow


def authorization_url(flow, redirect_uri):
    challenge = _b64url(hashlib.sha256(flow["verifier"].encode("ascii")).digest())
    params = {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": flow["state"],
        "nonce": flow["nonce"],
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(code, verifier, redirect_uri):
    """Swap an authorization code for Google's ID token (the raw JWT string), or None."""
    try:
        response = requests.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=TOKEN_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        # The exception text can contain the request; log only its type.
        logger.warning("google oauth: token request failed (%s)", type(exc).__name__)
        return None
    if response.status_code != 200:
        error = ""
        try:
            error = str(response.json().get("error", ""))[:64]
        except ValueError:
            pass
        logger.warning("google oauth: token endpoint answered %s %s", response.status_code, error)
        return None
    try:
        id_token = response.json().get("id_token")
    except ValueError:
        id_token = None
    if not id_token or not isinstance(id_token, str):
        logger.warning("google oauth: token response without id_token")
        return None
    return id_token


def _jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (IndexError, ValueError, binascii.Error, UnicodeError):
        return None
    return claims if isinstance(claims, dict) else None


def verify_id_token(token, nonce, now=None):
    """Check the ID token's claims; return a GoogleIdentity or None."""
    claims = _jwt_claims(token)
    if claims is None:
        logger.warning("google oauth: unreadable id_token")
        return None
    now = int(now if now is not None else time.time())
    problems = []
    if claims.get("iss") not in VALID_ISSUERS:
        problems.append("iss")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if settings.GOOGLE_OAUTH_CLIENT_ID not in auds:
        problems.append("aud")
    try:
        if int(claims.get("exp", 0)) + CLOCK_SKEW_SECONDS < now:
            problems.append("exp")
        if int(claims.get("iat", 0)) - CLOCK_SKEW_SECONDS > now:
            problems.append("iat")
    except (TypeError, ValueError):
        problems.append("exp")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), str(nonce)):
        problems.append("nonce")
    if not claims.get("sub") or not claims.get("email"):
        problems.append("sub/email")
    if problems:
        logger.warning("google oauth: id_token rejected (%s)", ",".join(problems))
        return None
    verified = claims.get("email_verified")
    return GoogleIdentity(
        sub=str(claims["sub"]),
        email=str(claims["email"]).strip().lower(),
        email_verified=verified is True or verified == "true",
        name=str(claims.get("name", ""))[:120],
    )


def suggest_username(identity, taken):
    """A free username from the Google email's local part (4–30 chars, [A-Za-z0-9_.])."""
    from .models import Users

    base = "".join(ch for ch in identity.email.split("@")[0] if ch.isalnum() or ch in "_.")
    base = base[:Users.USERNAME_MAX_LENGTH - 4]
    if len(base) < 4:
        base = (base + "_user")[:8] if base else "user"
    candidate = base
    for _ in range(20):
        if not taken(candidate):
            return candidate
        candidate = f"{base}{secrets.randbelow(9000) + 1000}"
    return ""
