"""An ASGI handler that keeps Django's request path on the event loop.

``django.core.handlers.asgi.ASGIHandler`` already awaits async views, but it
still moves several per-request steps to a worker thread with
``sync_to_async``: the ``request_started`` receivers, ``response.render()``
(every DRF ``Response``), exception-to-response conversion, error logging,
and ``response.close()`` (which sends ``request_finished``). Synchronous-only
middleware also runs in a thread.

``NativeASGIHandler`` runs those steps natively in a greenlet instead, so any
database access in them is awaited on the event loop. Two exceptions stay on
threads deliberately (see ``asyncdjango._threads``): 5xx/unexpected-error
reporting, whose logging handlers may send mail synchronously, and finishing a
request that already ran code in Django's per-request thread. It also:

* answers ASGI ``lifespan`` events, opening the connection pools at startup
  and closing them at shutdown;
* supports ``ATOMIC_REQUESTS`` for async views with a native transaction;
* can run selected synchronous middleware natively
  (``ASYNCDJANGO["NATIVE_SYNC_MIDDLEWARE"]``).

Synchronous *views* still run in a worker thread, as in Django: they may call
blocking libraries. Write views as ``async def`` to stay on the event loop.

Parts of ``handle()``, ``get_response_async()``, and ``_get_response_async()``
are adapted from Django 6.0.3 (BSD-3-Clause); see docs/upstream.md.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from functools import wraps
from typing import Any

import django
from asgiref.sync import AsyncToSync, SyncToAsync, iscoroutinefunction
from django.conf import settings
from django.core import signals
from django.core.handlers import base as base_handler
from django.core.handlers import exception as exception_handler
from django.core.handlers.asgi import ASGIHandler, get_script_prefix
from django.core.exceptions import BadRequest, PermissionDenied, RequestAborted
from django.http import Http404
from django.http.multipartparser import MultiPartParserError
from django.db import connections
from django.urls import set_script_prefix, set_urlconf
from django.utils.log import log_response

from . import conf
from ._threads import run_error_reporting, run_in_request_thread
from .middleware import native_middleware_paths
from .db.bridge import await_
from .db.pool import close_pools, open_pools
from .db.scope import run_native, sync_to_native
from .db.transaction import Atomic
from .signals import asend as native_asend

logger = logging.getLogger("asyncdjango.asgi")
_load_lock = threading.Lock()


_ROUTINE_EXCEPTIONS = (Http404, PermissionDenied, BadRequest, MultiPartParserError)


def _native_convert_exception_to_response(get_response: Any) -> Any:
    if not iscoroutinefunction(get_response):
        return _django_convert_exception_to_response(get_response)

    @wraps(get_response)
    async def inner(request: Any) -> Any:
        try:
            return await get_response(request)
        except Exception as exc:
            if isinstance(exc, _ROUTINE_EXCEPTIONS):
                # 4xx responses: rendered and logged (as warnings) natively.
                return await run_native(exception_handler.response_for_exception,
                                        request, exc)
            # 500s and SuspiciousOperation are reported at ERROR level, which
            # may email admins synchronously: keep Django's thread for them.
            return await run_error_reporting(exception_handler.response_for_exception,
                                             request, exc)

    return inner


_django_convert_exception_to_response = exception_handler.convert_exception_to_response


def _request_thread_used() -> bool:
    """Whether this request already ran code in Django's per-request thread.

    Synchronous views and unlisted sync middleware run in a thread owned by
    the request's ThreadSensitiveContext. Django connections are thread-local,
    so a connection opened there must also be closed there (by the
    request_finished receivers); otherwise it is orphaned when the thread ends.
    """
    context = SyncToAsync.thread_sensitive_context.get(None)
    return context is not None and context in SyncToAsync.context_to_thread_executor


class NativeASGIHandler(ASGIHandler):
    def __init__(self) -> None:
        self._native_middleware = native_middleware_paths()
        super().__init__()

    # Startup ---------------------------------------------------------------

    def load_middleware(self, is_async: bool = False) -> None:
        # Django's load_middleware() wraps each layer with the module-level
        # convert_exception_to_response(); substitute the native version.
        with _load_lock:
            base_handler.convert_exception_to_response = _native_convert_exception_to_response
            try:
                super().load_middleware(is_async)
            finally:
                base_handler.convert_exception_to_response = (
                    _django_convert_exception_to_response
                )

    def _is_native_middleware(self, method: Any) -> bool:
        target = getattr(method, "__self__", None) or getattr(method, "__wrapped__", None)
        if target is None:
            return False
        cls = target if isinstance(target, type) else type(target)
        return f"{cls.__module__}.{cls.__qualname__}" in self._native_middleware

    def adapt_method_mode(self, is_async: bool, method: Any, method_is_async: bool | None = None,
                          debug: bool = False, name: str | None = None) -> Any:
        if method_is_async is None:
            method_is_async = iscoroutinefunction(method)
        if is_async and not method_is_async and self._is_native_middleware(method):
            return sync_to_native(method)
        if (not is_async and method_is_async and name
                and name.removeprefix("middleware ") in self._native_middleware):
            # A natively run sync middleware calls the async handler beneath
            # it; it already runs in a greenlet, so await that handler there.
            @wraps(method)
            def call_async(request: Any) -> Any:
                return await_(method(request))

            return call_async
        return super().adapt_method_mode(is_async, method, method_is_async, debug, name)

    # ASGI entry point --------------------------------------------------------

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
            return
        await super().__call__(scope, receive, send)

    async def lifespan(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await open_pools()
                    await warm_content_types()
                except Exception as exc:
                    logger.exception("Opening database pools failed")
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    await close_pools()
                except Exception as exc:
                    logger.exception("Closing database pools failed")
                    await send({"type": "lifespan.shutdown.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return

    # Request handling (adapted from Django's ASGIHandler.handle) ---------------

    async def handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        try:
            body_file = await self.read_body(receive)
        except RequestAborted:
            return
        set_script_prefix(get_script_prefix(scope))
        await native_asend(signals.request_started, sender=self.__class__, scope=scope)
        request, error_response = self.create_request(scope, body_file)
        if request is None:
            body_file.close()
            await self.send_response(error_response, send)
            await self._close_response(error_response)
            return

        async def process_request(request: Any, send: Any) -> Any:
            response = await self.run_get_response(request)
            try:
                await self.send_response(response, send)
            except asyncio.CancelledError:
                # Client disconnected during send_response (ignore exception).
                pass
            return response

        tasks = [
            asyncio.create_task(self.listen_for_disconnect(receive)),
            asyncio.create_task(process_request(request, send)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks:
            if task.done():
                try:
                    task.result()
                except RequestAborted:
                    pass
                except AssertionError:
                    body_file.close()
                    raise
            else:
                # Allow views to handle cancellation; native transactions roll
                # back and return their connections as the task unwinds.
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        try:
            response = tasks[1].result()
        except asyncio.CancelledError:
            if _request_thread_used():
                await run_in_request_thread(signals.request_finished.send, self.__class__)
            else:
                await native_asend(signals.request_finished, sender=self.__class__)
        else:
            await self._close_response(response)
        body_file.close()

    async def _close_response(self, response: Any) -> None:
        # response.close() sends request_finished, which closes Django's
        # thread-local connections; run it where those connections live.
        if _request_thread_used():
            await run_in_request_thread(response.close)
        else:
            await run_native(response.close)

    async def get_response_async(self, request: Any) -> Any:
        set_urlconf(settings.ROOT_URLCONF)
        response = await self._middleware_chain(request)
        response._resource_closers.append(request.close)
        if response.status_code >= 500:
            # ERROR level: handlers like AdminEmailHandler block, so keep
            # Django's thread for it.
            await run_error_reporting(log_response, "%s: %s", response.reason_phrase,
                                      request.path, response=response, request=request)
        elif response.status_code >= 400:
            log_response("%s: %s", response.reason_phrase, request.path,
                         response=response, request=request)
        return response

    async def _get_response_async(self, request: Any) -> Any:
        response = None
        callback, callback_args, callback_kwargs = self.resolve_request(request)
        for middleware_method in self._view_middleware:
            response = await middleware_method(request, callback, callback_args,
                                               callback_kwargs)
            if response:
                break
        if response is None:
            wrapped_callback = self.make_view_atomic(callback)
            if not iscoroutinefunction(wrapped_callback):
                # Synchronous views keep Django's worker-thread behavior.
                wrapped_callback = self.adapt_method_mode(True, wrapped_callback, False)
            try:
                response = await wrapped_callback(request, *callback_args, **callback_kwargs)
            except Exception as exc:
                response = await self._process_exception(exc, request)
                if response is None:
                    raise
        self.check_response(response, callback)
        if hasattr(response, "render") and callable(response.render):
            for middleware_method in self._template_response_middleware:
                response = await middleware_method(request, response)
                self.check_response(
                    response, middleware_method,
                    name="%s.process_template_response"
                    % (middleware_method.__self__.__class__.__name__,),
                )
            try:
                if iscoroutinefunction(response.render):
                    response = await response.render()
                else:
                    response = await run_native(response.render)
            except Exception as exc:
                response = await self._process_exception(exc, request)
                if response is None:
                    raise
        if asyncio.iscoroutine(response):
            raise RuntimeError("Response is still a coroutine.")
        return response

    async def _process_exception(self, exc: Exception, request: Any) -> Any:
        if not self._exception_middleware:
            return None
        # Async process_exception() hooks are wrapped in async_to_sync by
        # Django; inside a native greenlet that awaits directly.
        if all(isinstance(method, AsyncToSync) or self._is_native_middleware(method)
               for method in self._exception_middleware):
            return await run_native(self.process_exception_by_middleware, exc, request)
        # Unlisted synchronous process_exception hooks may block; keep
        # Django's per-request thread for them.
        return await run_in_request_thread(self.process_exception_by_middleware, exc, request)

    def make_view_atomic(self, view: Any) -> Any:
        if not iscoroutinefunction(view):
            return super().make_view_atomic(view)
        non_atomic_requests = getattr(view, "_non_atomic_requests", set())
        for alias, settings_dict in connections.settings.items():
            if settings_dict["ATOMIC_REQUESTS"] and alias not in non_atomic_requests:
                view = Atomic(alias, True, False)(view)
        return view


async def warm_content_types() -> None:
    """Load every ContentType into Django's cache with one native query.

    Generic relation managers and ``get_for_model()`` otherwise run a
    synchronous query on a cache miss, which fails in async code.
    """
    from django.apps import apps

    if not apps.is_installed("django.contrib.contenttypes"):
        return
    from django.contrib.contenttypes.models import ContentType
    from django.db import router

    using = router.db_for_read(ContentType)
    from .db.pool import is_native_alias

    if not is_native_alias(using):
        return

    def load() -> None:
        for content_type in ContentType.objects.db_manager(using).all():
            ContentType.objects._add_to_cache(using, content_type)

    await run_native(load)


def get_asgi_application() -> NativeASGIHandler:
    """Drop-in replacement for ``django.core.asgi.get_asgi_application()``."""
    django.setup(set_prefix=False)
    return NativeASGIHandler()
