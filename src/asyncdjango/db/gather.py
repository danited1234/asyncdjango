"""Run independent ORM calls of one request concurrently."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable
from typing import Any

from django.db.models.query import QuerySet, RawQuerySet

from ..exceptions import CrossTaskConnectionError
from .scope import active_scope


async def gather(*calls: Awaitable[Any] | QuerySet | RawQuerySet, limit: int = 4) -> list[Any]:
    """Await ORM calls concurrently and return their results in order.

    Each call gets its own pooled connection, at most ``limit`` at a time, so
    one request cannot take the whole pool. A queryset is evaluated to a list.
    If a call raises, the others are cancelled (returning their connections)
    and its exception propagates::

        customer, orders, open_count = await gather(
            Customer.objects.aget(pk=pk),
            Order.objects.filter(customer_id=pk)[:20],
            Ticket.objects.filter(customer_id=pk, open=True).acount(),
        )

    The calls run outside any transaction, so ``gather()`` refuses to run
    inside ``transaction.atomic()``.
    """
    if limit < 1:
        _close(calls)
        raise ValueError("limit must be at least 1")
    scope = active_scope()
    if scope is not None and scope.in_transaction and not scope.shared:
        _close(calls)
        raise CrossTaskConnectionError(
            "gather() inside transaction.atomic(): its calls would run on other "
            "connections, outside the transaction. Await them one after another "
            "instead, or call gather() before opening the transaction."
        )
    semaphore = asyncio.Semaphore(limit)

    async def run(call: Any) -> Any:
        async with semaphore:
            if isinstance(call, QuerySet | RawQuerySet):
                return [obj async for obj in call]
            return await call

    tasks = [asyncio.ensure_future(run(call)) for call in calls]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _close(calls: tuple[Any, ...]) -> None:
    # Avoid "coroutine was never awaited" warnings for calls we refuse to run.
    for call in calls:
        if inspect.iscoroutine(call):
            call.close()
