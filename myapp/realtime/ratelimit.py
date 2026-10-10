"""Inbound WebSocket rate limits (plan §3.4), on the shared Django cache.

Production cache = Redis, so the limits hold across all gunicorn workers'
connections and survive reconnects; tests use LocMemCache. Fixed windows keyed
per user (+ per task for chat.send): cheap ``add`` + ``incr``, no Lua.

``hit()`` returns ``(allowed, count, limit)``; the consumer answers an excess
with ``error {code: "rate_limited"}`` and closes with 4408 once a client keeps
going past ``ABUSE_FACTOR`` × the limit in one window.
"""

import time

from django.core.cache import cache

# bucket -> (max messages, window seconds)
LIMITS = {
    "default": (60, 60),
    "location.update": (1, 5),
    "chat.send": (20, 60),       # per task (see key())
    "chat.typing": (1, 2),
}
ABUSE_FACTOR = 2
MAX_MESSAGE_BYTES = 4096


def bucket_for(msg_type):
    return msg_type if msg_type in LIMITS else "default"


def _key(user_id, bucket, scope, window_start):
    return f"wsrl:{user_id}:{bucket}:{scope}:{window_start}"


async def hit(user_id, msg_type, scope=""):
    bucket = bucket_for(msg_type)
    limit, window = LIMITS[bucket]
    window_start = int(time.time() // window)
    key = _key(user_id, bucket, scope, window_start)
    try:
        await cache.aadd(key, 0, window + 1)
        count = await cache.aincr(key)
    except ValueError:  # key evicted between add and incr
        await cache.aset(key, 1, window + 1)
        count = 1
    except Exception:  # noqa: BLE001 — a cache outage must not kill sockets
        return True, 0, limit
    return count <= limit, count, limit


def is_abuse(count, limit):
    return count > limit * ABUSE_FACTOR
