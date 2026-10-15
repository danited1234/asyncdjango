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
    # psycopg_pool.AsyncConnectionPool options applied to every alias.
    "POOL": {"min_size": 1, "max_size": 10, "timeout": 30.0},
    # Per-alias overrides merged over POOL, e.g. {"replica": {"max_size": 4}}.
    "POOLS": {},
    # Sync-only middleware paths to run natively on the event loop instead of
    # in a worker thread. Only list middleware that does no blocking non-DB I/O.
    "NATIVE_SYNC_MIDDLEWARE": [],
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
