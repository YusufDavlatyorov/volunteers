"""Celery app for background notification delivery (myapp/tasks.py).

Worker (production, `celery_worker` compose service):
    celery -A server worker --pool=threads --concurrency=4 --loglevel=INFO
Configuration lives in server/settings.py under the CELERY_ prefix.
"""

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "server.settings")

app = Celery("khayrkhoh")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
