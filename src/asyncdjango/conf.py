"""The ``ASYNCDJANGO`` settings dictionary and its defaults."""

from __future__ import annotations

from typing import Any

from django.conf import settings

DEFAULTS: dict[str, Any] = {
    # Replace Django's thread-offloading a* ORM methods with native ones.
    "PATCH_ORM": True,
    # Use Django's sync_to_async implementation, with a warning, when an
    # operation cannot run natively. Off by default: such operations raise.
    "ALLOW_SYNC_FALLBACK": False,
    # Run the native path on event loops that async_to_sync() creates for a
    # synchronous caller (Celery tasks, scripts, async tests on Django's
    # TestCase). Off by default: ORM calls there use the caller thread's own
    # Django connection, as in plain Django, instead of a pool per call.
    "NATIVE_IN_ASYNC_TO_SYNC": False,
    # psycopg_pool.AsyncConnectionPool options applied to every alias.
    "POOL": {"min_size": 1, "max_size": 10, "timeout": 30.0},
    # Per-alias overrides merged over POOL, e.g. {"replica": {"max_size": 4}}.
    "POOLS": {},
    # Middleware class paths whose synchronous hooks run natively on the event
    # loop instead of in a worker thread (sync-only middleware, and
    # MiddlewareMixin's process_request/process_response). Only list
    # middleware that does no blocking I/O other than database access.
    "NATIVE_SYNC_MIDDLEWARE": [],
    # Also treat Django's built-in middleware that does no blocking I/O as
    # native (see asyncdjango.middleware).
    "NATIVE_BUILTIN_MIDDLEWARE": True,
    # Dotted path to a function(connection) returning SQL that sets the
    # session state a query needs (search_path for a tenant), or None. Run
    # only when the pooled connection's state differs (asyncdjango.db.session).
    "SESSION_SQL": None,
}


def get(name: str) -> Any:
    user = getattr(settings, "ASYNCDJANGO", {}) or {}
    if name not in DEFAULTS:
        raise KeyError(f"Unknown asyncdjango setting: {name}")
    return user.get(name, DEFAULTS[name])


def pool_options(alias: str) -> dict[str, Any]:
    options = {**DEFAULTS["POOL"], **get("POOL")}
    options.update(get("POOLS").get(alias, {}))
    return options
