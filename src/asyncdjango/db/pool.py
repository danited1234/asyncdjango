"""Per-event-loop psycopg async connection pools for Django database aliases.

A psycopg async connection belongs to the event loop that created it, so pools
are keyed by loop and alias. ASGI lifespan startup opens them eagerly through
``open_pools()``; otherwise the first query on a loop opens its pool.
"""

from __future__ import annotations

import asyncio
import weakref
from typing import Any

import psycopg
from psycopg import sql as psycopg_sql
from psycopg_pool import AsyncConnectionPool

from .. import conf

_pools: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, AsyncConnectionPool]
] = weakref.WeakKeyDictionary()
_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = weakref.WeakKeyDictionary()
_guards: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Task[None]
] = weakref.WeakKeyDictionary()


async def _close_on_shutdown() -> None:
    """Close this loop's pools when asyncio.run() cancels remaining tasks.

    A psycopg pool that is still opening connections retries through task
    cancellation, so without this an unclosed pool keeps asyncio.run() (and
    the process) from ever finishing.
    """
    loop = asyncio.get_running_loop()
    try:
        await loop.create_future()
    except asyncio.CancelledError:
        pools = _pools.pop(loop, {})
        for pool in pools.values():
            await pool.close(timeout=5)
        raise


def native_aliases() -> list[str]:
    """Return the configured aliases served by the native backend."""
    from django.db import connections

    from .backend import wrapper_class

    return [
        alias
        for alias, settings_dict in connections.settings.items()
        if wrapper_class(settings_dict["ENGINE"]) is not None
    ]


def is_native_alias(alias: str) -> bool:
    from django.db import connections

    from .backend import wrapper_class

    settings_dict = connections.settings.get(alias)
    return settings_dict is not None and wrapper_class(settings_dict["ENGINE"]) is not None


def _template_wrapper(alias: str) -> Any:
    from django.db import connections

    from .backend import wrapper_class

    settings_dict = connections.settings[alias]
    cls = wrapper_class(settings_dict["ENGINE"])
    if cls is None:
        raise ValueError(f"Database alias {alias!r} is not a psycopg 3 PostgreSQL backend")
    return cls(settings_dict, alias)


def _make_pool(alias: str) -> AsyncConnectionPool:
    wrapper = _template_wrapper(alias)
    options = wrapper.settings_dict["OPTIONS"]
    kwargs = wrapper.get_connection_params()
    timezone_name = wrapper.timezone_name
    set_time_zone_sql = wrapper.ops.set_time_zone_sql()
    role = options.get("assume_role")
    isolation_level = options.get("isolation_level")

    async def configure(connection: psycopg.AsyncConnection) -> None:
        # Mirrors DatabaseWrapper._configure_connection() and the isolation
        # level handling in get_new_connection(), once per physical connection.
        if timezone_name and connection.info.parameter_status("TimeZone") != timezone_name:
            await connection.execute(set_time_zone_sql, [timezone_name])
        if role:
            await connection.execute(
                psycopg_sql.SQL("SET ROLE {}").format(psycopg_sql.Literal(role))
            )
        if isolation_level is not None:
            await connection.set_isolation_level(psycopg.IsolationLevel(isolation_level))

    check = (
        AsyncConnectionPool.check_connection
        if wrapper.settings_dict["CONN_HEALTH_CHECKS"]
        else None
    )
    return AsyncConnectionPool(
        kwargs=kwargs,
        open=False,
        configure=configure,
        check=check,
        name=f"asyncdjango-{alias}",
        **conf.pool_options(alias),
    )


async def get_pool(alias: str) -> AsyncConnectionPool:
    loop = asyncio.get_running_loop()
    pools = _pools.setdefault(loop, {})
    pool = pools.get(alias)
    if pool is not None:
        return pool
    lock = _locks.setdefault(loop, asyncio.Lock())
    async with lock:
        pool = pools.get(alias)
        if pool is None:
            pool = _make_pool(alias)
            await pool.open(wait=False)
            pools[alias] = pool
            if loop not in _guards:
                _guards[loop] = loop.create_task(
                    _close_on_shutdown(), name="asyncdjango-pool-guard")
    return pool


async def acquire(alias: str) -> psycopg.AsyncConnection:
    pool = await get_pool(alias)
    return await pool.getconn()


async def release(alias: str, connection: psycopg.AsyncConnection) -> None:
    # Return it to the pool it came from: close_pools() may have replaced the
    # loop's pool since it was acquired.
    pool = getattr(connection, "_pool", None)
    if pool is None or pool.closed:
        await connection.close()
    else:
        await pool.putconn(connection)


async def open_pools(aliases: list[str] | None = None, *, wait: bool = False,
                     timeout: float = 30.0) -> None:
    """Open pools for the given (default: all native) aliases on this loop.

    With ``wait=True``, block until each pool has ``min_size`` connections and
    raise if the database is unreachable within ``timeout`` seconds.
    """
    for alias in native_aliases() if aliases is None else aliases:
        pool = await get_pool(alias)
        if wait:
            await pool.wait(timeout=timeout)


async def close_pools() -> None:
    """Close every pool opened on the running event loop."""
    loop = asyncio.get_running_loop()
    pools = _pools.pop(loop, {})
    guard = _guards.pop(loop, None)
    if guard is not None:
        guard.cancel()
    for pool in pools.values():
        await pool.close()


def pool_stats() -> dict[str, dict[str, int]]:
    """Return psycopg pool statistics for the running loop, keyed by alias."""
    pools = _pools.get(asyncio.get_running_loop(), {})
    return {alias: pool.get_stats() for alias, pool in pools.items()}
