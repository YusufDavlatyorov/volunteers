"""The one place that answers "what is this visitor's IP?" (rate limits, logs).

Production chain: visitor → (optionally Cloudflare) → nginx → gunicorn on
127.0.0.1. ``REMOTE_ADDR`` is nginx's address for *every* request (in the
Docker era it was the bridge gateway — same problem), so using it directly made
all visitors share one login / password-reset throttle bucket (a few failed
logins anywhere locked everyone out). nginx passes the TCP peer it saw as
``X-Real-IP`` (deploy/nginx/khayrkhoh.conf). The uvicorn worker deliberately
does not rewrite the peer from proxy headers (server/asgi_worker.py), so this
module is the only place that decides.

Trust rules — a header is only believed when the hop that set it is ours:

* ``X-Real-IP`` — only when ``DJANGO_BEHIND_TLS_PROXY=True`` *and* the direct
  peer is a loopback / private address (our nginx). A visitor can't reach
  gunicorn directly — it listens on 127.0.0.1.
* ``CF-Connecting-IP`` — only when ``DJANGO_BEHIND_CLOUDFLARE=True`` *and* the
  address nginx saw belongs to Cloudflare (``CLOUDFLARE_IP_RANGES``). Someone
  who bypasses Cloudflare and talks to the origin with a forged header is not
  from those ranges, so the header is ignored. docs/CLOUDFLARE.md says how to
  refresh the ranges.
"""

import ipaddress
from functools import lru_cache

from django.conf import settings

UNKNOWN = "unknown"


def _parse(value):
    try:
        return ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None


@lru_cache(maxsize=8)
def _networks(ranges):
    return tuple(ipaddress.ip_network(r, strict=False) for r in ranges)


def is_cloudflare(ip):
    addr = _parse(str(ip)) if not isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)) else ip
    if addr is None:
        return False
    return any(addr in net for net in _networks(tuple(settings.CLOUDFLARE_IP_RANGES)) if addr.version == net.version)


def client_ip(request):
    """Best-known client IP as a string (``"unknown"`` if nothing usable)."""
    meta = request.META
    peer = _parse(meta.get("REMOTE_ADDR"))
    ip = peer
    if getattr(settings, "BEHIND_TLS_PROXY", False) and peer is not None and (peer.is_loopback or peer.is_private):
        real = _parse(meta.get("HTTP_X_REAL_IP"))
        if real is not None:
            ip = real
    if getattr(settings, "BEHIND_CLOUDFLARE", False) and ip is not None and is_cloudflare(ip):
        visitor = _parse(meta.get("HTTP_CF_CONNECTING_IP"))
        if visitor is not None:
            ip = visitor
    return str(ip) if ip is not None else UNKNOWN
