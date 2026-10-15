"""Development diagnostics: event-loop stalls and worker-thread offloading."""

from __future__ import annotations

import asyncio
import functools


class EventLoopBlockDetector:
    """Fail a test when event-loop scheduling lag exceeds a chosen threshold.

    This is a diagnostic, not a proof that every library call is nonblocking.
    Use a generous threshold on busy CI hosts to avoid false positives.
    """

    def __init__(self, *, interval: float = 0.01, threshold: float = 0.1) -> None:
        if interval <= 0 or threshold <= 0:
            raise ValueError("interval and threshold must be positive")
        self.interval = interval
        self.threshold = threshold
        self.max_lag = 0.0
        self._started = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> EventLoopBlockDetector:
        self._task = asyncio.create_task(self._watch())
        await self._started.wait()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await asyncio.sleep(self.interval)
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        if exc_type is None and self.max_lag > self.threshold:
            raise RuntimeError(
                f"Event loop blocked for {self.max_lag:.3f}s "
                f"(threshold {self.threshold:.3f}s)"
            )

    async def _watch(self) -> None:
        loop = asyncio.get_running_loop()
        self._started.set()
        while True:
            expected = loop.time() + self.interval
            await asyncio.sleep(self.interval)
            self.max_lag = max(self.max_lag, loop.time() - expected)


class ThreadOffloadDetector:
    """Record or forbid ``loop.run_in_executor()`` calls while active.

    ``asgiref.sync.sync_to_async`` and ``asyncio.to_thread`` both reach a
    worker thread through the running loop's ``run_in_executor``, so this
    detects Django's thread-based async ORM methods as well. With
    ``forbid=True`` (the default) the offloaded call raises instead of running.

    ``allow`` names callables that may still use a thread. By default that is
    ``getaddrinfo``: asyncio resolves host names in its executor, which psycopg
    does once per new physical connection (not per query). Configure HOST as
    an IP address to avoid even that.
    """

    def __init__(self, *, forbid: bool = True,
                 allow: frozenset[str] = frozenset({"getaddrinfo", "_getaddrinfo_debug"})) -> None:
        self.forbid = forbid
        self.allow = allow
        self.calls: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    async def __aenter__(self) -> ThreadOffloadDetector:
        self._loop = asyncio.get_running_loop()
        original = self._loop.run_in_executor

        def run_in_executor(executor, func, *args):
            target = func
            while isinstance(target, functools.partial):
                inner = getattr(target.func, "__qualname__", "")
                if inner == "Context.run" and target.args:
                    # asyncio.to_thread() submits partial(context.run, func, ...).
                    target = target.args[0]
                elif inner.endswith("SyncToAsync.thread_handler") and target.args:
                    # sync_to_async submits partial(thread_handler, ..., child).
                    target = target.args[-1]
                else:
                    target = target.func
            # Never repr(): a bound QuerySet method would evaluate the query.
            name = getattr(target, "__qualname__", None) or type(target).__qualname__
            if name.rsplit(".", 1)[-1] in self.allow:
                return original(executor, func, *args)
            self.calls.append(name)
            if self.forbid:
                raise AssertionError(f"Work was offloaded to a worker thread: {name}")
            return original(executor, func, *args)

        self._loop.run_in_executor = run_in_executor
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del self._loop.run_in_executor
