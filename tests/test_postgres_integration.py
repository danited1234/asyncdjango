"""Run with ASYNCDJANGO_DATABASE_URL and asyncpg installed."""

import asyncio
import importlib.util
import os
import tracemalloc
import unittest


HAS_DATABASE = bool(os.environ.get("ASYNCDJANGO_DATABASE_URL"))
HAS_DRIVER = importlib.util.find_spec("asyncpg") is not None


@unittest.skipUnless(HAS_DATABASE and HAS_DRIVER, "PostgreSQL URL and asyncpg required")
class PostgresIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from asyncdjango.db import AsyncPostgres

        self.database = AsyncPostgres(
            os.environ["ASYNCDJANGO_DATABASE_URL"], max_size=4
        )
        await self.database.connect()

    async def asyncTearDown(self) -> None:
        await self.database.close()

    async def test_concurrent_queries(self) -> None:
        results = await asyncio.gather(*(
            self.database.fetch("SELECT pg_sleep(0.1), $1::int AS n", n)
            for n in range(4)
        ))
        self.assertEqual([rows[0]["n"] for rows in results], [0, 1, 2, 3])

    async def test_large_result_streaming_is_bounded(self) -> None:
        tracemalloc.start()
        try:
            count = 0
            async for _ in self.database.stream(
                "SELECT generate_series(1, 50000) AS n", batch_size=128
            ):
                count += 1
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(count, 50000)
        self.assertLess(peak_bytes, 25 * 1024 * 1024)

    async def test_cancelled_query_releases_pool_connection(self) -> None:
        task = asyncio.create_task(self.database.fetch("SELECT pg_sleep(10)"))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        rows = await asyncio.wait_for(self.database.fetch("SELECT 1 AS n"), 3)
        self.assertEqual(rows[0]["n"], 1)

    async def test_nested_savepoint_and_task_ownership(self) -> None:
        import asyncpg

        async with self.database.transaction() as connection:
            with self.assertRaisesRegex(RuntimeError, "another task"):
                await asyncio.create_task(connection.fetch("SELECT 1"))
            with self.assertRaises(asyncpg.PostgresError):
                async with connection.transaction():
                    await connection.fetch("SELECT 1 / 0")
            rows = await connection.fetch("SELECT 1 AS n")
            self.assertEqual(rows[0]["n"], 1)
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            await connection.fetch("SELECT 1")

    async def test_cancelled_transaction_rolls_back(self) -> None:
        lock_key = 20261014
        started = asyncio.Event()

        async def long_transaction() -> None:
            async with self.database.transaction() as connection:
                await connection.fetch("SELECT pg_advisory_xact_lock($1)", lock_key)
                started.set()
                await connection.fetch("SELECT pg_sleep(10)")

        task = asyncio.create_task(long_transaction())
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        rows = await asyncio.wait_for(
            self.database.fetch(
                "SELECT pg_try_advisory_xact_lock($1) AS available", lock_key
            ), 3
        )
        self.assertTrue(rows[0]["available"])
