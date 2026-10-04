# KhayrKhoh (khayrkhoh.tj) — production image.
# Builds the Django app + gunicorn; PostgreSQL is a separate, pre-existing
# shared container (see docker-compose.yml) — this image has no database of
# its own. Migrations / collectstatic / the initial-accounts command are run
# as separate one-off commands against this image (see README → "Docker
# deployment"), not automatically on container start.

# Exact patch + Debian release pinned (not the floating `3.14-slim`): the
# server builds this image itself at deploy time, so a floating tag would give
# it whatever Python was newest that day — not the one the suite was run on
# (milestone 6: full suite green in this image, built for linux/amd64).
FROM python:3.14.8-slim-trixie

# Fail fast + smaller image: no .pyc cache, unbuffered logs to stdout/stderr
# (so `docker logs` / journald capture them, matching server/settings.py LOGGING).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Runs as an unprivileged user; MEDIA_ROOT/STATIC_ROOT below must stay
# writable by it (docker-compose.yml mounts them as volumes it owns).
RUN useradd --create-home --uid 1000 khayrkhoh \
    && mkdir -p /app/media /app/staticfiles \
    && chown -R khayrkhoh:khayrkhoh /app
USER khayrkhoh

EXPOSE 8000

# --workers 3 matches the resource budget documented in README (mem_limit
# 700m / cpus 1.0 for this container). --timeout is Gunicorn's own worker
# timeout, independent of Django's EMAIL_TIMEOUT / outbound request timeouts.
CMD ["gunicorn", "server.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60"]
