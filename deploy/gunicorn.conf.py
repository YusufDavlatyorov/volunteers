# Gunicorn settings for khayrkhoh-web.service (deploy/systemd/). The app, the
# worker class and the bind address are on the unit's ExecStart line; this file
# holds the tuning. Production is one 1-vCPU / 3.8 GB host (2026-10).

# 3 workers on 1 vCPU, as the WSGI service ran before. Under ASGI each worker
# runs Django's sync views on one thread, so HTTP concurrency is unchanged
# (3 requests at a time); WebSocket connections are async and cost no thread.
workers = 3

# Worker heartbeat timeout — independent of Django's outbound request timeouts.
timeout = 60
# On restart/stop, in-flight requests get this long; open WebSockets are then
# closed and the clients reconnect (static/js/realtime.js backs off and retries).
graceful_timeout = 30
keepalive = 5

# Errors and worker lifecycle to stderr → journald (`journalctl -u khayrkhoh-web`).
# No access log: nginx keeps one already.
errorlog = "-"
accesslog = None
loglevel = "info"

# Django's persistent connections (CONN_MAX_AGE) are not safe under ASGI —
# Django's docs say to disable them there. Forced here, for this process only:
# raw_env is applied to os.environ before the app is imported, so it wins over
# .env; celery/bots keep the settings default.
raw_env = ["DJANGO_DB_CONN_MAX_AGE=0"]
