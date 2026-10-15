"""Run synchronous middleware hooks natively instead of in a thread.

Django's ``MiddlewareMixin`` (the base of every built-in middleware) is
async-capable, but its async path still calls ``process_request()`` and
``process_response()`` through ``sync_to_async``: two thread hops per
middleware per request. Sync-only middleware and synchronous
``process_view()``/``process_exception()`` hooks are also run in a thread.

For middleware classes on the *native list*, asyncdjango runs those hooks in a
greenlet on the event loop instead (database access in them is awaited). The
list is:

* Django's built-in middleware that performs no blocking I/O other than the
  database (enabled by ``ASYNCDJANGO["NATIVE_BUILTIN_MIDDLEWARE"]``, default
  True). Session-dependent ones are included only when ``SESSION_ENGINE`` is
  the database or signed-cookie backend, because the cache and file backends
  block.
* Whatever the project lists in ``ASYNCDJANGO["NATIVE_SYNC_MIDDLEWARE"]``.

Matching is by exact class path: a subclass of a built-in is not trusted
automatically and must be listed.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils.deprecation import MiddlewareMixin

from . import conf
from .db.scope import run_native

# No blocking I/O beyond the database, whatever the configuration.
BUILTIN_NATIVE_MIDDLEWARE = frozenset({
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.http.ConditionalGetMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sites.middleware.CurrentSiteMiddleware",
    "django.contrib.admindocs.middleware.XViewMiddleware",
    "django.contrib.flatpages.middleware.FlatpageFallbackMiddleware",
    "django.contrib.redirects.middleware.RedirectFallbackMiddleware",
})
# Read or write the session, so only native with a non-blocking backend.
SESSION_NATIVE_MIDDLEWARE = frozenset({
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
})
NON_BLOCKING_SESSION_ENGINES = frozenset({
    "django.contrib.sessions.backends.db",
    "django.contrib.sessions.backends.signed_cookies",
})
# Deliberately absent: cache middleware (blocking cache I/O) and
# BrokenLinkEmailsMiddleware (sends mail).


def native_middleware_paths() -> frozenset[str]:
    paths = set(conf.get("NATIVE_SYNC_MIDDLEWARE"))
    if conf.get("NATIVE_BUILTIN_MIDDLEWARE"):
        paths |= BUILTIN_NATIVE_MIDDLEWARE
        engine = getattr(settings, "SESSION_ENGINE", "django.contrib.sessions.backends.db")
        if engine in NON_BLOCKING_SESSION_ENGINES:
            paths |= SESSION_NATIVE_MIDDLEWARE
    return frozenset(paths)


def class_path(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def is_native_class(cls: type) -> bool:
    return class_path(cls) in native_middleware_paths()


_original_acall: Any = None


async def _native_acall(self: MiddlewareMixin, request: Any) -> Any:
    if not is_native_class(type(self)):
        return await _original_acall(self, request)
    response = None
    if hasattr(self, "process_request"):
        response = await run_native(self.process_request, request)
    response = response or await self.get_response(request)
    if hasattr(self, "process_response"):
        response = await run_native(self.process_response, request, response)
    return response


def install() -> None:
    """Patch ``MiddlewareMixin.__acall__``; inert for classes not on the list."""
    global _original_acall
    if _original_acall is not None:
        return
    _original_acall = MiddlewareMixin.__acall__
    _native_acall.__wrapped__ = _original_acall
    MiddlewareMixin.__acall__ = _native_acall
