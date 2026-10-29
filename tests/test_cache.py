"""asyncdjango.cache.RedisCache: Django's Redis cache without worker threads."""

import asyncio
import os
import threading
import unittest
import uuid

from asgiref.sync import async_to_sync

from asyncdjango import sync_to_native
from asyncdjango.diagnostics import ThreadOffloadDetector

# A Redis database these tests may flush (aclear() runs FLUSHDB).
REDIS_URL = os.environ.get("ASYNCDJANGO_REDIS_URL")


def make_cache():
    from asyncdjango.cache import RedisCache

    return RedisCache(REDIS_URL, {"KEY_PREFIX": uuid.uuid4().hex[:8]})


@unittest.skipUnless(REDIS_URL, "set ASYNCDJANGO_REDIS_URL to run the Redis cache tests")
class NativeRedisCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cache = make_cache()
        self.offload = ThreadOffloadDetector()
        await self.offload.__aenter__()

    async def asyncTearDown(self):
        await self.offload.__aexit__(None, None, None)
        await self.cache.aclear()
        await self.cache.aclose()

    async def test_async_api_without_threads(self):
        cache = self.cache
        self.assertIsNone(await cache.aget("missing"))
        self.assertEqual(await cache.aget("missing", "default"), "default")
        await cache.aset("key", {"a": [1, 2]})
        self.assertEqual(await cache.aget("key"), {"a": [1, 2]})
        self.assertFalse(await cache.aadd("key", "other"))
        self.assertTrue(await cache.aadd("new", "value"))
        self.assertTrue(await cache.ahas_key("new"))
        self.assertTrue(await cache.atouch("new", 60))
        self.assertTrue(await cache.adelete("new"))
        self.assertFalse(await cache.ahas_key("new"))

        await cache.aset("counter", 1)
        self.assertEqual(await cache.aincr("counter", 5), 6)
        self.assertEqual(await cache.adecr("counter"), 5)
        with self.assertRaises(ValueError):
            await cache.aincr("no-counter")

        await cache.aset_many({"a": 1, "b": "two"}, timeout=60)
        self.assertEqual(await cache.aget_many(["a", "b", "c"]), {"a": 1, "b": "two"})
        await cache.adelete_many(["a", "b"])
        self.assertEqual(await cache.aget_many(["a", "b"]), {})

        self.assertEqual(await cache.aget_or_set("computed", lambda: 42), 42)
        self.assertEqual(await cache.aget("computed"), 42)
        self.assertEqual(self.offload.calls, [])

    async def test_sync_calls_from_native_code_are_awaited(self):
        cache = self.cache
        await cache.aset("key", "value")

        def read():
            return cache.get("key"), threading.get_ident()

        value, thread = await sync_to_native(read)()
        self.assertEqual((value, thread), ("value", threading.get_ident()))
        self.assertIn(asyncio.get_running_loop(), cache._cache._async_pools)

    async def test_concurrent_requests(self):
        await asyncio.gather(*(self.cache.aset(f"k{i}", i) for i in range(50)))
        values = await asyncio.gather(*(self.cache.aget(f"k{i}") for i in range(50)))
        self.assertEqual(values, list(range(50)))


@unittest.skipUnless(REDIS_URL, "set ASYNCDJANGO_REDIS_URL to run the Redis cache tests")
class SyncCallerTests(unittest.TestCase):
    def test_synchronous_code_uses_the_synchronous_client(self):
        cache = make_cache()
        cache.set("key", "value")
        self.assertEqual(cache.get("key"), "value")
        # async_to_sync() from a Celery task: the caller's thread, no pool per call.
        self.assertEqual(async_to_sync(cache.aget)("key"), "value")
        self.assertEqual(len(cache._cache._async_pools), 0)
        cache.clear()
