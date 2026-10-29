"""Task-owned native database scopes and ``run_native()``.

Django keeps one ``DatabaseWrapper`` per alias per thread. On an event loop
every request shares one thread, so asyncdjango keeps native wrappers per
*scope* instead. A scope belongs to one asyncio task:

* each ``run_native()`` call outside a transaction gets a short-lived scope
  that acquires pooled connections on demand and returns them afterwards;
* ``async with transaction.atomic()`` binds a scope to the task until the
  outermost block exits, so every ORM call inside shares its connection.

While a scope is active, ``django.db.connections[alias]`` returns the scope's
wrapper, so Django's unchanged ORM code finds the right connection.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any, TypeVar

from asgiref.sync import AsyncToSync

from .. import conf
from .._threads import run_in_caller_thread
from ..exceptions import CrossTaskConnectionError
from .bridge import finish, greenlet_spawn, in_native_context
from .pool import is_native_alias

T = TypeVar("T")

_current_scope: contextvars.ContextVar[NativeScope | None] = contextvars.ContextVar(
    "asyncdjango_scope", default=None
)


class NativeScope:
    """Database wrappers (and their pooled connections) owned by one task.

    A *transactional* scope belongs to an ``async with atomic()`` block:
    another task that inherits it through its context gets
    ``CrossTaskConnectionError``. A temporary scope (one ``run_native()``
    call, or an ``aiterator()``) is simply invisible to other tasks, which
    get their own. A closed scope is invisible to everyone.
    """

    # Test scopes (asyncdjango.test.NativeTestCase) span several tasks.
    shared = False

    def __init__(self, *, transactional: bool = False,
                 generator_frame: FrameType | None = None) -> None:
        self.owner = asyncio.current_task()
        self.transactional = transactional
        # Set when an atomic() block is opened inside an async generator: its
        # context variable leaks into the consumer's context while the
        # generator is suspended, so the scope is only honored while that
        # frame is running.
        self.generator_frame = generator_frame
        self.closed = False
        self.wrappers: dict[str, Any] = {}

    def frame_is_running(self) -> bool:
        frame = sys._getframe(1)
        while frame is not None:
            if frame is self.generator_frame:
                return True
            frame = frame.f_back
        return False

    @property
    def in_transaction(self) -> bool:
        """A transactional scope, or one whose connection is in an atomic block."""
        return self.transactional or any(
            wrapper.in_atomic_block for wrapper in self.wrappers.values())

    def owned_by_current_task(self) -> bool:
        return self.shared or asyncio.current_task() is self.owner

    def check_owner(self) -> None:
        if not self.owned_by_current_task():
            raise CrossTaskConnectionError(
                "This task inherited a database scope (an open "
                "transaction.atomic() block) from the task that created it. "
                "Concurrent tasks cannot share a transaction connection; await "
                "the database calls sequentially inside the transaction, or "
                "start the child tasks outside it."
            )

    def wrapper(self, alias: str) -> Any | None:
        wrapper = self.wrappers.get(alias)
        if wrapper is None:
            if not is_native_alias(alias):
                return None
            from django.db import connections

            from .backend import wrapper_class

            settings_dict = connections.settings[alias]
            wrapper = wrapper_class(settings_dict["ENGINE"])(settings_dict, alias)
            self.wrappers[alias] = wrapper
        return wrapper

    async def aclose(self) -> None:
        """Return every connection to its pool; idempotent."""
        self.closed = True
        wrappers, self.wrappers = self.wrappers, {}
        errors = []
        for wrapper in wrappers.values():
            try:
                await wrapper.arelease()
            except Exception as exc:  # release the others before reporting
                errors.append(exc)
        if errors:
            raise errors[0]

    async def aclose_uncancelled(self) -> None:
        """``aclose()`` that a cancellation of the caller cannot interrupt."""
        if not self.wrappers:
            self.closed = True
            return
        # Returning connections rarely waits; shield (a new task) only if it does.
        iterator = self.aclose().__await__()
        try:
            waiting_on = iterator.send(None)
        except StopIteration:
            return
        await asyncio.shield(finish(iterator, waiting_on))


def active_scope() -> NativeScope | None:
    """The scope the current task should use, or None for a new one.

    Raises CrossTaskConnectionError when the task inherited another task's
    open transaction.
    """
    scope = _current_scope.get()
    if scope is None or scope.closed:
        return None
    if (scope.generator_frame is not None and not in_native_context()
            and not scope.frame_is_running()):
        # Consumer code outside the generator's `async with` block. (Inside a
        # greenlet the scope was bound explicitly by run_in_scope().)
        return None
    if scope.owned_by_current_task():
        return scope
    if scope.in_transaction:
        scope.check_owner()
    return None


current_scope = active_scope


async def run_in_scope(scope: NativeScope, fn: Callable[..., T], /,
                       *args: Any, **kwargs: Any) -> T:
    """Run ``fn`` natively with ``scope`` active, without binding it to the task."""
    token = _current_scope.set(scope)
    try:
        return await greenlet_spawn(fn, *args, **kwargs)
    finally:
        _current_scope.reset(token)


def in_sync_caller_loop() -> bool:
    """Whether ORM calls should run on the synchronous caller's thread.

    True on an event loop that ``async_to_sync()`` created because a
    synchronous thread called it (a Celery task, a management command, an async
    test on Django's ``TestCase``), unless ``NATIVE_IN_ASYNC_TO_SYNC`` is set.
    Such a loop lives for one call: a connection pool would be created and torn
    down every time, and the caller's own connection (and any transaction open
    on it) would be invisible to native queries. Its thread is idle until the
    loop finishes, so running the ORM there costs no extra thread.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    return (loop in AsyncToSync.loop_thread_executors
            and not conf.get("NATIVE_IN_ASYNC_TO_SYNC"))


async def run_native(fn: Callable[..., T], /, *args: Any, **kwargs: Any) -> T:
    """Run synchronous ORM code on the event loop with native async I/O.

    Uses the task's active scope (inside ``transaction.atomic()``) or a
    temporary one whose connections are returned to the pool afterwards. On a
    loop made by ``async_to_sync()`` for a synchronous caller, runs ``fn`` on
    that caller's thread instead (see ``in_sync_caller_loop()``).
    """
    if in_sync_caller_loop():
        return await run_in_caller_thread(fn, *args, **kwargs)
    scope = active_scope()
    if scope is not None:
        return await run_in_scope(scope, fn, *args, **kwargs)
    scope = NativeScope()
    try:
        return await run_in_scope(scope, fn, *args, **kwargs)
    finally:
        await scope.aclose_uncancelled()


def sync_to_native(fn: Callable[..., T]) -> Callable[..., Any]:
    """Like ``asgiref.sync.sync_to_async`` but runs ``fn`` on the event loop.

    Database I/O inside ``fn`` is awaited natively and the call joins any
    active ``transaction.atomic()`` block. Use it only for code whose blocking
    work is database access (or ``asyncdjango.cache.RedisCache``): other
    blocking I/O (HTTP clients, files, other cache backends) would stall the
    event loop for every request.

    Under ``async_to_sync()`` from synchronous code (a Celery task) ``fn`` runs
    on the calling thread, as ``sync_to_async`` would.
    """

    async def wrapper(*args: Any, **kwargs: Any) -> T:
        return await run_native(fn, *args, **kwargs)

    wrapper.__name__ = getattr(fn, "__name__", "sync_to_native")
    wrapper.__qualname__ = getattr(fn, "__qualname__", wrapper.__name__)
    wrapper.__wrapped__ = fn
    return wrapper


_hook_installed = False


def install_connection_hook() -> None:
    """Route ``django.db.connections[alias]`` to the active scope's wrapper."""
    global _hook_installed
    if _hook_installed:
        return
    from django.db import utils

    original = utils.ConnectionHandler.__getitem__

    def __getitem__(self: Any, alias: str) -> Any:
        scope = (_current_scope.get() is not None and self is _default_handler()
                 and _on_event_loop() and active_scope())
        if scope:
            wrapper = scope.wrapper(alias)
            if wrapper is not None:
                return wrapper
        # Worker threads (sync_to_async copies the caller's context into them)
        # keep Django's own per-thread connection.
        return original(self, alias)

    __getitem__.__wrapped__ = original
    utils.ConnectionHandler.__getitem__ = __getitem__
    _hook_installed = True


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _default_handler() -> Any:
    from django.db import connections

    return connections
