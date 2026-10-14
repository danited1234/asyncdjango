"""Development diagnostic for blocking work on an asyncio event loop."""

from __future__ import annotations

import asyncio


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
