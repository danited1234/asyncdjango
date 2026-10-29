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
* ``run_in_caller_thread``: ORM work on an event loop that ``async_to_sync()``
  made for a synchronous caller (a Celery task, a script, an async test on
  Django's ``TestCase``). asgiref runs it on that caller's own thread, which
  is blocked waiting for the loop anyway, so no thread is added; the work uses
  the caller's Django connection and sees its open transactions.
* ``run_blocking``: ``asyncdjango.mail.asend_mail()`` with an email backend
  that does blocking I/O (anything but asyncdjango's own and Django's test
  backends).

``asyncdjango.monitoring`` also starts one watchdog thread per event loop when
``STALL_THRESHOLD`` is set; it only reads the loop's heartbeat and stack.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from asgiref.sync import sync_to_async


async def run_in_request_thread(fn: Callable[..., Any], *args: Any) -> Any:
    return await sync_to_async(fn, thread_sensitive=True)(*args)


async def run_error_reporting(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await sync_to_async(fn, thread_sensitive=False)(*args, **kwargs)


async def run_in_caller_thread(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await sync_to_async(fn, thread_sensitive=True)(*args, **kwargs)


async def run_blocking(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return await sync_to_async(fn, thread_sensitive=False)(*args, **kwargs)
