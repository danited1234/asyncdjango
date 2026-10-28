"""ASGI application for the stack in BENCH_STACK (served by benchmarks/server.py)."""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "benchmarks.settings")

from benchmarks.stacks import STACKS

if STACKS[os.environ.get("BENCH_STACK", "asyncdjango-drf")].native:
    from asyncdjango.asgi import get_asgi_application
else:
    from django.core.asgi import get_asgi_application

application = get_asgi_application()
