"""Find code that blocks the event loop in production.

With ``ASYNCDJANGO["STALL_THRESHOLD"]`` set, ``NativeASGIHandler`` starts a
watchdog for its event loop. The loop updates a heartbeat; a watchdog thread
notices when the heartbeat is late, and grabs the loop thread's stack while
the blocking call is still running. When the loop recovers it logs one warning
on the ``asyncdjango.stalls`` logger with the stall's length, the request, and
that stack.

The watchdog thread only reads the heartbeat and stack frames; no request
work runs on it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
import weakref
from types import FrameType
from typing import Any

from . import conf

logger = logging.getLogger("asyncdjango.stalls")

_watchdogs: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, LoopWatchdog
] = weakref.WeakKeyDictionary()


class LoopWatchdog:
    def __init__(self, loop: asyncio.AbstractEventLoop, threshold: float) -> None:
        self.loop = loop
        self.threshold = threshold
        self.interval = threshold / 4
        self.stalls = 0
        self.longest = 0.0
        self.total = 0.0
        self._loop_thread = threading.get_ident()
        self._due = time.monotonic() + self.interval
        # (due, request, stack) for the stall in progress, set by the watchdog.
        self._captured: tuple[float, str | None, str] | None = None
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name="asyncdjango-loop-watchdog")

    def start(self) -> None:
        self.loop.call_later(self.interval, self._beat)
        self._thread.start()

    def stats(self) -> dict[str, Any]:
        return {"threshold": self.threshold, "stalls": self.stalls,
                "longest": round(self.longest, 3), "total": round(self.total, 3)}

    def _beat(self) -> None:
        now = time.monotonic()
        late = now - self._due
        if late >= self.threshold:
            self._report(late)
        self._due = now + self.interval
        self.loop.call_later(self.interval, self._beat)

    def _report(self, late: float) -> None:
        self.stalls += 1
        self.longest = max(self.longest, late)
        self.total += late
        captured = self._captured
        if captured is not None and captured[0] == self._due:
            _, request, stack = captured
        else:
            # Over before the watchdog looked.
            request, stack = None, "  (not captured)\n"
        logger.warning("Event loop blocked for %.3fs%s; stack while blocked:\n%s",
                       late, f" during {request}" if request else "", stack)

    def _watch(self) -> None:
        while True:
            time.sleep(self.interval)
            if self.loop.is_closed():
                return
            due = self._due
            if (time.monotonic() - due < self.threshold or not self.loop.is_running()
                    or (self._captured is not None and self._captured[0] == due)):
                continue
            frame = sys._current_frames().get(self._loop_thread)
            if frame is not None:
                self._captured = (due, _request(frame), "".join(traceback.format_stack(frame)))


def _request(frame: FrameType | None) -> str | None:
    from .asgi import NativeASGIHandler

    code = NativeASGIHandler.get_response_async.__code__
    while frame is not None:
        if frame.f_code is code:
            request = frame.f_locals.get("request")
            if request is not None:
                return f"{request.method} {request.path}"
        frame = frame.f_back
    return None


def watch_event_loop(threshold: float | None = None) -> LoopWatchdog | None:
    """Start the running loop's watchdog, once. Returns None when disabled."""
    loop = asyncio.get_running_loop()
    watchdog = _watchdogs.get(loop)
    if watchdog is not None:
        return watchdog
    threshold = conf.get("STALL_THRESHOLD") if threshold is None else threshold
    if not threshold:
        return None
    watchdog = _watchdogs[loop] = LoopWatchdog(loop, threshold)
    watchdog.start()
    return watchdog


def stall_stats() -> dict[str, Any] | None:
    """Stall counters for the running loop, or None if it is not watched."""
    watchdog = _watchdogs.get(asyncio.get_running_loop())
    return None if watchdog is None else watchdog.stats()
