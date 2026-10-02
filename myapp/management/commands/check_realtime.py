"""Realtime self-check: is the Channels layer the shared Redis one, and does a
group message make a round trip through it?

Run inside any app container after a deploy (DEPLOY.md → "WebSockets"):

    docker compose exec -T web python manage.py check_realtime
    docker compose exec -T ws  python manage.py check_realtime

Exit code 0 = OK. Exit code 1 = the layer is missing, still the in-process
``InMemoryChannelLayer`` (events from ``web`` / ``celery_worker`` would never
reach the ``ws`` process), or the round trip failed / timed out. Read-only:
uses a throw-away group and channel, prints no secrets (the Redis URL's
password, if any, is masked).
"""

import asyncio
import re
import uuid

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

TIMEOUT_SECONDS = 5


def _masked(url):
    return re.sub(r"//([^:@/]*):[^@/]*@", r"//\1:***@", url or "")


class Command(BaseCommand):
    help = "Check that the Channels layer is Redis-backed and a group message makes a round trip."

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-memory", action="store_true",
            help="Accept the in-process layer (local dev / tests) instead of failing.",
        )

    def handle(self, *args, **options):
        layer = get_channel_layer()
        if layer is None:
            raise CommandError("No channel layer configured (CHANNEL_LAYERS).")
        backend = f"{type(layer).__module__}.{type(layer).__name__}"
        self.stdout.write(f"backend: {backend}")
        is_redis = "channels_redis" in backend
        if is_redis:
            self.stdout.write(f"redis: {_masked(getattr(settings, 'CHANNEL_REDIS_URL', ''))}")
        elif not options["allow_memory"]:
            raise CommandError(
                "In-process channel layer: realtime events from web/celery would not reach the ws "
                "service. Set DJANGO_CHANNEL_REDIS_URL (docker-compose.yml sets redis://redis:6379/2)."
            )

        group = f"selfcheck_{uuid.uuid4().hex[:12]}"
        token = uuid.uuid4().hex

        async def round_trip():
            channel = await layer.new_channel()
            await layer.group_add(group, channel)
            try:
                await layer.group_send(group, {"type": "selfcheck.ping", "token": token})
                message = await asyncio.wait_for(layer.receive(channel), TIMEOUT_SECONDS)
                return message.get("token") == token
            finally:
                await layer.group_discard(group, channel)

        try:
            ok = async_to_sync(round_trip)()
        except asyncio.TimeoutError as exc:
            raise CommandError(f"Round trip timed out after {TIMEOUT_SECONDS}s.") from exc
        except Exception as exc:  # noqa: BLE001 — report any layer failure as a check failure
            raise CommandError(f"Round trip failed: {type(exc).__name__}") from exc
        if not ok:
            raise CommandError("Round trip returned an unexpected message.")
        self.stdout.write(self.style.SUCCESS("round trip: OK"))
