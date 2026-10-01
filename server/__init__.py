# Load the Celery app with Django so @shared_task binds to it (myapp/tasks.py).
from .celery import app as celery_app

__all__ = ("celery_app",)
