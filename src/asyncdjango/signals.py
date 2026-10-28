"""Signal dispatch from native ORM code.

Inside a native greenlet, Django's ``Signal.send()`` cannot reach async
receivers through ``async_to_sync`` (the event loop already runs on this
thread). ``install()`` patches ``send()`` and ``send_robust()`` so that, only
in that situation, async receivers are awaited natively, one after another in
the sending task. Elsewhere Django's implementation runs unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from django.dispatch import Signal
from django.dispatch.dispatcher import NO_RECEIVERS

from .db.bridge import await_, in_native_context
from .db.scope import run_native

logger = logging.getLogger("django.dispatch")

_originals: dict[str, Any] = {}


def _receivers(signal: Signal, sender: Any) -> tuple[list[Any], list[Any]]:
    if not signal.receivers or signal.sender_receivers_cache.get(sender) is NO_RECEIVERS:
        return [], []
    return signal._live_receivers(sender)


def send(self: Signal, sender: Any, **named: Any) -> list[tuple[Any, Any]]:
    # Same fast path as Django: pre_init/post_init fire for every model row.
    if not self.receivers or self.sender_receivers_cache.get(sender) is NO_RECEIVERS:
        return []
    if not in_native_context():
        return _originals["send"](self, sender, **named)
    sync_receivers, async_receivers = _receivers(self, sender)
    responses = [(receiver, receiver(signal=self, sender=sender, **named))
                 for receiver in sync_receivers]
    # Awaited one at a time in this task (not gathered in child tasks), so
    # their queries join the sender's connection and open transaction.
    for receiver in async_receivers:
        responses.append((receiver, await_(receiver(signal=self, sender=sender, **named))))
    return responses


def send_robust(self: Signal, sender: Any, **named: Any) -> list[tuple[Any, Any]]:
    if not self.receivers or self.sender_receivers_cache.get(sender) is NO_RECEIVERS:
        return []
    if not in_native_context():
        return _originals["send_robust"](self, sender, **named)
    sync_receivers, async_receivers = _receivers(self, sender)
    responses = []
    for receiver in sync_receivers:
        try:
            response = receiver(signal=self, sender=sender, **named)
        except Exception as err:
            self._log_robust_failure(receiver, err)
            responses.append((receiver, err))
        else:
            responses.append((receiver, response))
    for receiver in async_receivers:
        try:
            response = await_(receiver(signal=self, sender=sender, **named))
        except Exception as err:
            self._log_robust_failure(receiver, err)
            responses.append((receiver, err))
        else:
            responses.append((receiver, response))
    return responses


async def asend(signal: Signal, sender: Any, **named: Any) -> list[tuple[Any, Any]]:
    """``Signal.asend()`` with synchronous receivers run natively, not in a thread.

    Synchronous receivers run on the event loop, so they must not perform
    blocking I/O other than database access.
    """
    sync_receivers, async_receivers = _receivers(signal, sender)
    responses: list[tuple[Any, Any]] = []
    if sync_receivers:
        def call_sync() -> list[tuple[Any, Any]]:
            return [(receiver, receiver(signal=signal, sender=sender, **named))
                    for receiver in sync_receivers]

        responses = await run_native(call_sync)
    if async_receivers:
        async_responses = await asyncio.gather(*(
            receiver(signal=signal, sender=sender, **named)
            for receiver in async_receivers
        ))
        responses.extend(zip(async_receivers, async_responses, strict=True))
    return responses


def install() -> None:
    if _originals:
        return
    _originals["send"] = Signal.send
    _originals["send_robust"] = Signal.send_robust
    Signal.send = send
    Signal.send_robust = send_robust


def uninstall() -> None:
    if _originals:
        Signal.send = _originals.pop("send")
        Signal.send_robust = _originals.pop("send_robust")


_async_to_sync_call: Any = None


def install_async_to_sync() -> None:
    """Let ``async_to_sync()`` await directly when called from a native greenlet.

    Synchronous code reached through the native path (for example a DRF
    authentication class that wraps an async helper in ``async_to_sync``)
    already runs on the event loop thread, where asgiref would raise
    "You cannot use AsyncToSync in the same thread as an async event loop".
    In that one situation the coroutine is awaited through the greenlet bridge.
    """
    global _async_to_sync_call
    if _async_to_sync_call is not None:
        return
    from asgiref.sync import AsyncToSync

    _async_to_sync_call = AsyncToSync.__call__

    def __call__(self: Any, *args: Any, **kwargs: Any) -> Any:
        if in_native_context():
            return await_(self.awaitable(*args, **kwargs))
        return _async_to_sync_call(self, *args, **kwargs)

    AsyncToSync.__call__ = __call__
