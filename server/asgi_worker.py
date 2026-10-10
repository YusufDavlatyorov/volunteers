"""Gunicorn worker for production: uvicorn serving server/asgi.py (HTTP + /ws/).

    gunicorn server.asgi:application -k server.asgi_worker.UvicornWorker -c deploy/gunicorn.conf.py

The stock ``uvicorn_worker.UvicornWorker`` (the maintained successor of the
deprecated ``uvicorn.workers`` module) with three settings uvicorn only takes
as Config arguments, kept identical to the uvicorn command the WebSocket
service ran before (2026-10):

* ``proxy_headers=False`` — the visitor's address stays the TCP peer (our
  nginx on 127.0.0.1), and ``server/client_ip.py`` alone decides which proxy
  header to believe (X-Real-IP from loopback nginx, CF-Connecting-IP from
  Cloudflare ranges). With uvicorn's default it would rewrite the peer from
  X-Forwarded-For first — a second, untested trust path.
* ``ws_max_size=16384`` — frames over 16 KB are refused by the server itself;
  the consumer already closes anything over 4 KB (docs/WS_PROTOCOL.md).
* ``access_log=False`` / ``lifespan="off"`` — nginx keeps the access log;
  Django has no lifespan handler (uvicorn would log that on every start).
"""

from uvicorn_worker import UvicornWorker as _UvicornWorker


class UvicornWorker(_UvicornWorker):
    CONFIG_KWARGS = {
        **_UvicornWorker.CONFIG_KWARGS,
        "proxy_headers": False,
        "ws_max_size": 16384,
        "access_log": False,
        "lifespan": "off",
    }
