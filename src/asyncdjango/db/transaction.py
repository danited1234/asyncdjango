"""``async with transaction.atomic()`` on the native backend.

``Atomic`` subclasses Django's ``Atomic`` and adds ``__aenter__``/``__aexit__``.
They run Django's own ``__enter__``/``__exit__`` natively, so nesting,
savepoints, ``durable=True``, ``set_rollback()``, and ``on_commit()`` behave
exactly as in synchronous Django. The outermost block binds a native scope to
the current task until it exits; every ORM call awaited inside the block uses
that transaction's connection.

Synchronous helpers such as ``set_rollback()``, ``get_rollback()``, and
``on_commit()`` with a synchronous callback perform no I/O; Django's versions
work unchanged inside an async atomic block and are re-exported here.

On an event loop that ``async_to_sync()`` created for a synchronous caller (a
Celery task, for example), ``async with atomic()`` runs Django's
``__enter__``/``__exit__`` on that caller's thread instead, so the block uses
the caller's Django connection like every other ORM call on that loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import logging
import sys
from typing import Any, Callable

from asgiref.sync import async_to_sync
from django.db import DEFAULT_DB_ALIAS
from django.db import transaction as django_transaction
from django.db.transaction import (  # noqa: F401  (re-exported)
    get_autocommit,
    get_connection,
    get_rollback,
    mark_for_rollback_on_error,
    non_atomic_requests,
    set_rollback,
)

from .._threads import run_in_caller_thread
from ..exceptions import NativeAsyncUnsupported
from .bridge import await_, in_native_context
from .pool import is_native_alias
from .scope import (
    NativeScope,
    _current_scope,
    active_scope,
    in_sync_caller_loop,
    run_in_scope,
)

logger = logging.getLogger("asyncdjango.transaction")

_background_tasks: set[asyncio.Task[Any]] = set()

# Marks an entry made on the synchronous caller's thread (no native scope).
_CALLER_THREAD = object()
# The caller thread's Django connections, by alias, while an async atomic block
# opened on a caller-thread loop is active: on_commit() runs on the event loop
# and must register its callback on the caller's connection.
_caller_thread_connections: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("asyncdjango_caller_thread_connections", default=None)
)


class Atomic(django_transaction.Atomic):
    """Django's ``Atomic``, also usable with ``async with`` and on ``async def``."""

    async def __aenter__(self) -> None:
        using = DEFAULT_DB_ALIAS if self.using is None else self.using
        if in_sync_caller_loop():
            await _enter_in_caller_thread(self, using)
            return
        if not is_native_alias(using):
            raise NativeAsyncUnsupported(
                f"async transaction.atomic() requires a psycopg 3 PostgreSQL "
                f"database; {using!r} is not one"
            )
        scope = active_scope()
        token = None
        if scope is None:
            caller = sys._getframe(1)
            in_generator = bool(caller.f_code.co_flags & inspect.CO_ASYNC_GENERATOR)
            scope = NativeScope(transactional=True,
                                generator_frame=caller if in_generator else None)
            token = _current_scope.set(scope)
        # Entry state lives per task: one Atomic instance may be entered by
        # several tasks at once (Django keeps its own state on the connection).
        entry = (asyncio.current_task(), token, scope)
        self.__dict__.setdefault("_async_entries", []).append(entry)
        try:
            await run_in_scope(scope, self.__enter__)
        except BaseException:
            self._async_entries.remove(entry)
            if token is not None:
                _reset(_current_scope, token)
                await asyncio.shield(scope.aclose())
            raise

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        entries = self._async_entries
        task = asyncio.current_task()
        mine = [entry for entry in entries if entry[0] is task]
        entry = mine[-1] if mine else entries[-1]
        entries.remove(entry)
        owner, token, scope = entry
        if scope is _CALLER_THREAD:
            await _exit_in_caller_thread(self, owner, *token, exc_type, exc_value, traceback)
            return
        if owner is not task:
            await self._abandoned_exit(token, scope)
            return
        try:
            await run_in_scope(scope, self.__exit__, exc_type, exc_value, traceback)
        finally:
            if token is not None:
                _reset(_current_scope, token)
                await asyncio.shield(scope.aclose())

    async def _abandoned_exit(self, token: Any, scope: NativeScope) -> None:
        # Reached when an async generator holding this block is finalized by
        # the event loop in another task (the consumer stopped iterating
        # without aclose()). Django's __exit__ cannot run here; roll back.
        logger.warning(
            "transaction.atomic() exited from a different task (an abandoned async "
            "generator?); rolling it back. Close generators with contextlib.aclosing()."
        )
        if token is not None:
            await asyncio.shield(scope.aclose())  # pool rolls back the transaction
            return
        using = DEFAULT_DB_ALIAS if self.using is None else self.using
        wrapper = scope.wrappers.get(using)
        if wrapper is not None:
            wrapper.needs_rollback = True

    def __call__(self, func: Callable[..., Any]) -> Callable[..., Any]:
        if not inspect.iscoroutinefunction(func):
            # Not super(): this method is also installed on Django's Atomic.
            return contextlib.ContextDecorator.__call__(self, func)
        using, savepoint, durable = self.using, self.savepoint, self.durable

        @functools.wraps(func)
        async def inner(*args: Any, **kwargs: Any) -> Any:
            # A fresh instance per call: concurrent calls must not share state.
            async with Atomic(using, savepoint, durable):
                return await func(*args, **kwargs)

        return inner


async def _enter_in_caller_thread(atomic: Any, using: str) -> None:
    # Module functions, not methods: patch.install() copies only the dunder
    # methods onto Django's Atomic.
    def enter() -> Any:
        atomic.__enter__()
        return get_connection(using)

    connection = await run_in_caller_thread(enter)
    token = _caller_thread_connections.set(
        {**(_caller_thread_connections.get() or {}), using: connection})
    entry = (asyncio.current_task(), (token, connection), _CALLER_THREAD)
    atomic.__dict__.setdefault("_async_entries", []).append(entry)


async def _exit_in_caller_thread(atomic: Any, owner: Any, token: Any, connection: Any,
                                 exc_type: Any, exc_value: Any, traceback: Any) -> None:
    try:
        if owner is not asyncio.current_task():
            # An abandoned async generator finalized by the loop: roll back.
            logger.warning(
                "transaction.atomic() exited from a different task (an abandoned "
                "async generator?); rolling it back. Close generators with "
                "contextlib.aclosing()."
            )
            connection.needs_rollback = True
        await run_in_caller_thread(atomic.__exit__, exc_type, exc_value, traceback)
    finally:
        _reset(_caller_thread_connections, token)


def _reset(var: contextvars.ContextVar[Any], token: Any) -> None:
    try:
        var.reset(token)
    except ValueError:
        # Exited in a different context; the stale value is ignored there.
        pass


def atomic(using: Any = None, savepoint: bool = True, durable: bool = False) -> Any:
    """Same signature and behavior as ``django.db.transaction.atomic``."""
    if callable(using):
        return Atomic(DEFAULT_DB_ALIAS, savepoint, durable)(using)
    return Atomic(using, savepoint, durable)


def on_commit(func: Callable[[], Any], using: str | None = None,
              robust: bool = False) -> None:
    """Register ``func`` to run after the current transaction commits.

    ``func`` may be synchronous, a coroutine function, or any callable that
    returns an awaitable (``lambda: notify(order.pk)`` with an ``async def
    notify``). Inside an atomic block the result is awaited natively when the
    outermost block commits; like any on_commit callback it delays the
    ``async with`` exit until it finishes. Outside an atomic block Django runs
    callbacks immediately: an awaitable result is then awaited in place when
    called from native code, or scheduled as a background task from async
    code (exceptions are logged).
    """
    connection = None
    if in_sync_caller_loop():
        caller_connections = _caller_thread_connections.get() or {}
        connection = caller_connections.get(DEFAULT_DB_ALIAS if using is None else using)
    if connection is None:
        connection = get_connection(using)
    if connection.in_atomic_block:

        def run() -> Any:
            result = func()
            if not inspect.isawaitable(result):
                return result
            if in_native_context():
                return await_(result)
            # On the synchronous caller's thread, during the atomic block's exit.
            return async_to_sync(_await)(result)

        functools.update_wrapper(run, func)
        connection.on_commit(run, robust=robust)
        return
    try:
        result = func()
    except Exception:
        if not robust:
            raise
        logger.exception("on_commit callback failed")
        return
    if not inspect.isawaitable(result):
        return
    if in_native_context():
        await_(result)
        return
    task = asyncio.ensure_future(result)
    _background_tasks.add(task)
    task.add_done_callback(_finish_background_task)


def _finish_background_task(task: asyncio.Task[Any]) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("on_commit callback failed", exc_info=task.exception())


async def _await(awaitable: Any) -> Any:
    return await awaitable
