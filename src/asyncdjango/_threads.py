"""The deliberate, documented uses of worker threads.

Everything else in asyncdjango stays on the event loop (enforced by
tests/test_guards.py). These remain on threads on purpose:

* ``run_in_request_thread``: finishing a request that already ran code in
  Django's per-request thread (sync views, sync-only middleware), so that
  thread's Django connection is closed by ``request_finished`` in the thread
  that owns it.
* ``run_error_reporting``: 5xx logging and unexpected-exception handling.
  Logging handlers such as ``AdminEmailHandler`` send mail synchronously;
  Django runs these off the loop (``thread_sensitive=False``) and so do we.
"""

from __future__ import annotations

from typing import Any, Callable

from asgiref.sync import sync_to_async


async def run_in_request_thread(fn: Callable[..., Any], *args: Any) -> Any:
    return await sync_to_async(fn, thread_sensitive=True)(*args)


async def run_error_reporting(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await sync_to_async(fn, thread_sensitive=False)(*args, **kwargs)
