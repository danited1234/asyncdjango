"""System checks that point out work still leaving the event loop."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Tags, Warning, register
from django.utils.module_loading import import_string

from . import conf


@register(Tags.database)
def check_native_databases(app_configs, **kwargs):
    from django.db import connections

    from .db.backend import wrapper_class

    messages = []
    for alias, settings_dict in connections.settings.items():
        if wrapper_class(settings_dict["ENGINE"]) is None:
            messages.append(Warning(
                f"Database alias {alias!r} ({settings_dict['ENGINE']}) is not "
                "served by the native async backend.",
                hint="Only PostgreSQL with psycopg 3 is supported. Async ORM "
                "calls on this alias raise NativeAsyncUnsupported unless "
                "ASYNCDJANGO['ALLOW_SYNC_FALLBACK'] is True.",
                id="asyncdjango.W001",
            ))
    return messages


@register()
def check_sync_middleware(app_configs, **kwargs):
    native = set(conf.get("NATIVE_SYNC_MIDDLEWARE"))
    messages = []
    for path in getattr(settings, "MIDDLEWARE", []):
        try:
            middleware = import_string(path)
        except ImportError:
            continue
        if not getattr(middleware, "async_capable", False) and path not in native:
            messages.append(Warning(
                f"Middleware {path!r} is synchronous only; Django runs it in a "
                "worker thread on every request.",
                hint="Use an async-capable version, or add it to "
                "ASYNCDJANGO['NATIVE_SYNC_MIDDLEWARE'] if it performs no "
                "blocking I/O other than database access.",
                id="asyncdjango.W002",
            ))
    return messages
