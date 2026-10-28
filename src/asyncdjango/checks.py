"""System checks that point out work still leaving the event loop."""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Tags, Warning, register
from django.utils.module_loading import import_string


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
    from django.utils.deprecation import MiddlewareMixin

    from .middleware import native_middleware_paths

    native = native_middleware_paths()
    messages = []
    for path in getattr(settings, "MIDDLEWARE", []):
        try:
            middleware = import_string(path)
        except ImportError:
            continue
        if path in native:
            continue
        if (isinstance(middleware, type) and issubclass(middleware, MiddlewareMixin)
                and middleware.__acall__ is MiddlewareMixin.__acall__):
            messages.append(Warning(
                f"Middleware {path!r} is based on MiddlewareMixin; its "
                "process_request()/process_response() run in a worker thread "
                "on every request.",
                hint="Add it to ASYNCDJANGO['NATIVE_SYNC_MIDDLEWARE'] if its "
                "hooks perform no blocking I/O other than database access, or "
                "give it a native async __acall__.",
                id="asyncdjango.W003",
            ))
            continue
        if not getattr(middleware, "async_capable", False):
            messages.append(Warning(
                f"Middleware {path!r} is synchronous only; Django runs it in a "
                "worker thread on every request.",
                hint="Use an async-capable version, or add it to "
                "ASYNCDJANGO['NATIVE_SYNC_MIDDLEWARE'] if it performs no "
                "blocking I/O other than database access.",
                id="asyncdjango.W002",
            ))
    return messages
