"""Run synchronous Django ORM code on the event loop with awaited I/O.

Django's ORM is written as synchronous code. Instead of moving that code to a
worker thread, ``greenlet_spawn()`` runs it in a greenlet on the event-loop
thread. Whenever the code reaches database I/O, the asyncdjango backend calls
``await_()``, which switches back to the event loop, awaits the psycopg
coroutine, and resumes the ORM code with the result. Other tasks keep running
while the query is in flight, and no thread pool is involved.

This is the same technique SQLAlchemy uses for its asyncio extension.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import greenlet

from ..exceptions import MissingNativeContext

T = TypeVar("T")


class _NativeGreenlet(greenlet.greenlet):
    __slots__ = ("driver",)

    def __init__(self, fn: Callable[..., Any], driver: greenlet.greenlet) -> None:
        super().__init__(fn, driver)
        self.driver = driver


def in_native_context() -> bool:
    """Return whether the caller runs inside ``greenlet_spawn()``."""
    return isinstance(greenlet.getcurrent(), _NativeGreenlet)


def await_(awaitable: Awaitable[T]) -> T:
    """Await from synchronous code that runs inside ``greenlet_spawn()``."""
    current = greenlet.getcurrent()
    if not isinstance(current, _NativeGreenlet):
        close = getattr(awaitable, "close", None)
        if close is not None:
            close()
        raise MissingNativeContext(
            "Synchronous database access from an async context. Await the "
            "async ORM method instead (for example `await qs.aget()`), or wrap "
            "ORM-only synchronous code in `await asyncdjango.sync_to_native(fn)()`."
        )
    return current.driver.switch(awaitable)


def await_now(awaitable: Awaitable[T]) -> T:
    """``await_()`` for awaitables that usually finish without waiting.

    They are started in the greenlet, and only switch to the event loop if
    they do wait: fetching rows a client-side cursor already received, or
    closing it.
    """
    if not isinstance(greenlet.getcurrent(), _NativeGreenlet):
        return await_(awaitable)
    iterator = awaitable.__await__()
    try:
        waiting_on = iterator.send(None)
    except StopIteration as stop:
        return stop.value
    return await_(_resume(iterator, waiting_on))


async def finish(iterator: Any, waiting_on: Any) -> Any:
    """Finish, as a coroutine, an awaitable already waiting on ``waiting_on``."""
    return await _resume(iterator, waiting_on)


@types.coroutine
def _resume(iterator: Any, waiting_on: Any) -> Any:
    """Finish an awaitable that is already waiting on ``waiting_on``."""
    while True:
        try:
            value = yield waiting_on
        except GeneratorExit:
            iterator.close()
            raise
        except BaseException as exc:
            try:
                waiting_on = iterator.throw(exc)
            except StopIteration as stop:
                return stop.value
        else:
            try:
                waiting_on = iterator.send(value)
            except StopIteration as stop:
                return stop.value


async def greenlet_spawn(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``fn`` on this thread, awaiting each I/O step it yields.

    ``fn`` runs in the calling task's own context (not a copy), so context
    variables behave as if it ran in the task: values the synchronous code
    sets (``translation.activate()``, logging context) are visible to the
    coroutines it awaits and to the caller afterwards, as with asgiref's
    ``sync_to_async``.
    """
    driver = greenlet.getcurrent()
    glet = _NativeGreenlet(fn, driver)
    glet.gr_context = driver.gr_context
    result: Any = glet.switch(*args, **kwargs)
    while not glet.dead:
        try:
            value = await result
        except BaseException:
            # Includes CancelledError: raise it inside the ORM code so its
            # cleanup (for example transaction rollback) runs before leaving.
            result = glet.throw(*sys.exc_info())
        else:
            result = glet.switch(value)
    return result
