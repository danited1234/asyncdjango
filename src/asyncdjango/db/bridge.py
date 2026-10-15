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

import contextvars
import sys
from typing import Any, Awaitable, Callable, TypeVar

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


_MISSING = object()


def _pull(context: contextvars.Context) -> None:
    """Apply context variables the greenlet changed to the running task."""
    for var, value in context.items():
        if var.get(_MISSING) is not value:
            var.set(value)


def _push(context: contextvars.Context) -> None:
    """Apply context variables the awaited code changed to the greenlet."""
    for var, value in contextvars.copy_context().items():
        if context.get(var, _MISSING) is not value:
            context.run(var.set, value)


async def greenlet_spawn(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run ``fn`` on this thread, awaiting each I/O step it yields.

    Context variables behave as if ``fn`` ran in the calling task: values the
    synchronous code sets (``translation.activate()``, logging context) are
    visible to coroutines it awaits and to the caller afterwards, as with
    asgiref's ``sync_to_async``.
    """
    glet = _NativeGreenlet(fn, greenlet.getcurrent())
    context = glet.gr_context = contextvars.copy_context()
    try:
        result: Any = glet.switch(*args, **kwargs)
        while not glet.dead:
            _pull(context)
            try:
                value = await result
            except BaseException:
                # Includes CancelledError: raise it inside the ORM code so its
                # cleanup (for example transaction rollback) runs before leaving.
                _push(context)
                result = glet.throw(*sys.exc_info())
            else:
                _push(context)
                result = glet.switch(value)
    finally:
        _pull(context)
    return result
