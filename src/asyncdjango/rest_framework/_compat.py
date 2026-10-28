"""Await async hook results from synchronous DRF code."""

from __future__ import annotations

import datetime
import decimal
import inspect
import uuid
from typing import Any

from asgiref.sync import async_to_sync

from ..db.bridge import await_, in_native_context

# Values serializer fields commonly return; none of them is awaitable.
_PLAIN_TYPES = frozenset({
    str, int, float, bool, type(None), bytes, dict, list, tuple,
    decimal.Decimal, datetime.datetime, datetime.date, datetime.time, uuid.UUID,
})


async def _await(awaitable: Any) -> Any:
    return await awaitable


def resolve(value: Any) -> Any:
    """Return ``value``, awaiting it first if a hook returned an awaitable.

    Inside a native greenlet the awaitable runs on the event loop through the
    bridge. In a synchronous context (a sync view running in a thread) it is
    run with ``async_to_sync``, as ADRF does.
    """
    if type(value) in _PLAIN_TYPES or not inspect.isawaitable(value):
        return value
    if in_native_context():
        return await_(value)
    return async_to_sync(_await)(value)
