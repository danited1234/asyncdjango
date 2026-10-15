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
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import sys
from typing import Any, Callable

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

from ..exceptions import NativeAsyncUnsupported
from .bridge import await_, in_native_context
from .pool import is_native_alias
from .scope import NativeScope, _current_scope, active_scope, run_in_scope

logger = logging.getLogger("asyncdjango.transaction")

_background_tasks: set[asyncio.Task[Any]] = set()


class Atomic(django_transaction.Atomic):
    """Django's ``Atomic``, also usable with ``async with`` and on ``async def``."""

    async def __aenter__(self) -> None:
        using = DEFAULT_DB_ALIAS if self.using is None else self.using
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
                _reset_scope(token)
                await asyncio.shield(scope.aclose())
            raise

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        entries = self._async_entries
        task = asyncio.current_task()
        mine = [entry for entry in entries if entry[0] is task]
        entry = mine[-1] if mine else entries[-1]
        entries.remove(entry)
        owner, token, scope = entry
        if owner is not task:
            await self._abandoned_exit(token, scope)
            return
        try:
            await run_in_scope(scope, self.__exit__, exc_type, exc_value, traceback)
        finally:
            if token is not None:
                _reset_scope(token)
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


def _reset_scope(token: Any) -> None:
    try:
        _current_scope.reset(token)
    except ValueError:
        # Exited in a different context; the closed scope is ignored there.
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
    connection = get_connection(using)
    if connection.in_atomic_block:

        def run() -> Any:
            result = func()
            if inspect.isawaitable(result):
                return await_(result)
            return result

        functools.update_wrapper(run, func)
        django_transaction.on_commit(run, using=using, robust=robust)
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
