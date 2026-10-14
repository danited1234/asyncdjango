"""Per-process async request throttling for development and single-worker apps."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from time import monotonic
from typing import Any

from .asgi import Request


class BaseThrottle:
    wait: float | None = None

    async def allow_request(self, request: Request, view: Any) -> bool:
        raise NotImplementedError


class SimpleRateThrottle(BaseThrottle):
    """Exact per-process sliding window; use shared storage for multi-worker limits."""

    rate = "10/min"
    _history: dict[str, deque[float]] = {}
    _lock = asyncio.Lock()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls._history = {}
        cls._lock = asyncio.Lock()

    def get_cache_key(self, request: Request, view: Any) -> str:
        user = request.user
        if getattr(user, "is_authenticated", False):
            return f"user:{getattr(user, 'pk', id(user))}"
        client = request.scope.get("client")
        return f"ip:{client[0] if client else 'unknown'}"

    async def allow_request(self, request: Request, view: Any) -> bool:
        count_text, separator, period_text = self.rate.partition("/")
        periods = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60,
                   "minute": 60, "h": 3600, "hour": 3600, "d": 86400, "day": 86400}
        if not separator or not count_text.isdecimal() or int(count_text) < 1 or period_text not in periods:
            raise ValueError(f"Invalid throttle rate: {self.rate}")
        limit = int(count_text)
        duration = periods[period_text]
        key = self.get_cache_key(request, view)
        now = monotonic()
        async with type(self)._lock:
            history = type(self)._history.setdefault(key, deque())
            while history and history[0] <= now - duration:
                history.popleft()
            if len(history) >= limit:
                self.wait = max(0.0, history[0] + duration - now)
                return False
            history.append(now)
            self.wait = None
            if len(type(self)._history) > 10_000:
                for old_key, old_history in list(type(self)._history.items()):
                    if not old_history or old_history[-1] <= now - duration:
                        del type(self)._history[old_key]
            return True

    def retry_after(self) -> int | None:
        return math.ceil(self.wait) if self.wait is not None else None
