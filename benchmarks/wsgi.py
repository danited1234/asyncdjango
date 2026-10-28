"""WSGI application for the sync DRF stack (served by benchmarks/server.py)."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "benchmarks.settings")

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
