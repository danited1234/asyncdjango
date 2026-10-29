"""Django's Redis cache backend without worker threads.

Django's ``RedisCache`` implements its async methods (``aget()``, ``aset()``,
...) with ``sync_to_async``, so each one runs a blocking redis-py call in a
worker thread. This backend runs the same code natively: it executes in a
greenlet on the event loop and awaits redis-py's asyncio client.

Synchronous cache calls made from native code (DRF throttles, the cached_db
session backend, a ``save()`` override) are awaited the same way instead of
blocking the event loop. Synchronous code elsewhere (management commands,
Celery tasks, sync views) keeps Django's synchronous client.

    CACHES = {
        "default": {
            "BACKEND": "asyncdjango.cache.RedisCache",
            "LOCATION": "redis://127.0.0.1:6379/0",
        },
    }
"""

from __future__ import annotations

import asyncio
import inspect
import weakref
from typing import Any

from django.core.cache.backends import redis as django_redis
from redis.asyncio import ConnectionPool as AsyncConnectionPool
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import Pipeline

from .db.bridge import await_, greenlet_spawn, in_native_context
from .db.scope import in_sync_caller_loop


class _Awaited:
    """A redis.asyncio client or pipeline whose commands are awaited in place."""

    def __init__(self, target: Any) -> None:
        self._target = target

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            # Pipeline commands only queue; execute() returns a coroutine.
            if isinstance(result, Pipeline):
                return _Awaited(result)
            return await_(result) if inspect.isawaitable(result) else result

        return call


class RedisCacheClient(django_redis.RedisCacheClient):
    def __init__(self, servers: list[str], **options: Any) -> None:
        super().__init__(servers, **options)
        # parser_class and pool_class are synchronous; redis.asyncio has its own.
        self._async_pool_options = {
            key: value for key, value in self._pool_options.items() if key != "parser_class"
        }
        # A redis.asyncio connection belongs to the loop that opened it.
        self._async_pools: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[int, AsyncConnectionPool]
        ] = weakref.WeakKeyDictionary()

    def get_client(self, key: Any = None, *, write: bool = False) -> Any:
        if not in_native_context():
            return super().get_client(key, write=write)
        loop = asyncio.get_running_loop()
        pools = self._async_pools.get(loop)
        if pools is None:
            pools = self._async_pools[loop] = {}
            loop.create_task(_disconnect_on_shutdown(pools), name="asyncdjango-redis-guard")
        index = self._get_connection_pool_index(write)
        if index not in pools:
            pools[index] = AsyncConnectionPool.from_url(
                self._servers[index], **self._async_pool_options)
        return _Awaited(AsyncRedis(connection_pool=pools[index]))

    async def aclose(self) -> None:
        for pool in self._async_pools.pop(asyncio.get_running_loop(), {}).values():
            await pool.disconnect()


async def _disconnect_on_shutdown(pools: dict[int, AsyncConnectionPool]) -> None:
    # asyncio.run() cancels this task when the loop finishes; the pools'
    # connections belong to that loop and would otherwise be left open.
    try:
        await asyncio.get_running_loop().create_future()
    except asyncio.CancelledError:
        for pool in pools.values():
            await pool.disconnect()
        raise


def _native(name: str) -> Any:
    async def method(self: RedisCache, *args: Any, **kwargs: Any) -> Any:
        if in_sync_caller_loop():
            # A loop async_to_sync() made for one call (a Celery task): use
            # Django's version, on the caller's thread, rather than a pool per call.
            return await getattr(super(RedisCache, self), f"a{name}")(*args, **kwargs)
        return await greenlet_spawn(getattr(self, name), *args, **kwargs)

    method.__name__ = method.__qualname__ = f"a{name}"
    return method


class RedisCache(django_redis.RedisCache):
    def __init__(self, server: str | list[str], params: dict[str, Any]) -> None:
        super().__init__(server, params)
        self._class = RedisCacheClient

    aadd = _native("add")
    aget = _native("get")
    aset = _native("set")
    atouch = _native("touch")
    adelete = _native("delete")
    aget_many = _native("get_many")
    ahas_key = _native("has_key")
    aincr = _native("incr")
    aset_many = _native("set_many")
    adelete_many = _native("delete_many")
    aclear = _native("clear")

    async def aclose(self, **kwargs: Any) -> None:
        """Disconnect this event loop's connections (Django never calls this)."""
        await self._cache.aclose()
