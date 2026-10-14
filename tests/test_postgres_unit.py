"""Transaction behavior with a controlled async driver double."""

import asyncio
from contextlib import asynccontextmanager
import unittest
from unittest.mock import AsyncMock, patch

from asyncdjango.db import AsyncPostgres


class FakeConnection:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.begins = 0
        self.block_query = False

    @asynccontextmanager
    async def transaction(self):
        self.begins += 1
        try:
            yield
        except BaseException:
            self.rollbacks += 1
            raise
        else:
            self.commits += 1

    async def fetch(self, query, *args):
        if self.block_query:
            await asyncio.sleep(3600)
        return [{"n": 1}]

    async def fetchrow(self, query, *args):
        return {"n": 1}

    async def execute(self, query, *args):
        return "OK"


class FakePool:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.acquired = 0
        self.released = 0
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        self.acquired += 1
        try:
            yield self.connection
        finally:
            self.released += 1

    async def close(self):
        self.closed = True


class PostgresUnitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.pool = FakePool()
        self.create_pool = patch(
            "asyncdjango.db.postgres.asyncpg.create_pool",
            new=AsyncMock(return_value=self.pool),
        )
        self.create_pool.start()
        self.database = AsyncPostgres("postgresql://unused")
        await self.database.connect()

    async def asyncTearDown(self) -> None:
        await self.database.close()
        self.create_pool.stop()

    async def test_task_ownership_and_closed_handle(self) -> None:
        async with self.database.transaction() as handle:
            with self.assertRaisesRegex(RuntimeError, "another task"):
                await asyncio.create_task(handle.fetch("SELECT 1"))
            self.assertEqual((await handle.fetch("SELECT 1"))[0]["n"], 1)
        with self.assertRaisesRegex(RuntimeError, "no longer active"):
            await handle.fetch("SELECT 1")
        self.assertEqual(self.pool.acquired, self.pool.released)

    async def test_nested_rollback_does_not_abort_outer_transaction(self) -> None:
        async with self.database.transaction() as handle:
            async with handle.transaction():
                handle.set_rollback()
            self.assertEqual((await handle.fetch("SELECT 1"))[0]["n"], 1)
        self.assertEqual(self.pool.connection.begins, 2)
        self.assertEqual(self.pool.connection.rollbacks, 1)
        self.assertEqual(self.pool.connection.commits, 1)

    async def test_outer_rollback_only(self) -> None:
        async with self.database.transaction() as handle:
            handle.set_rollback()
        self.assertEqual(self.pool.connection.rollbacks, 1)
        self.assertEqual(self.pool.connection.commits, 0)

    async def test_cancelled_transaction_rolls_back_and_releases(self) -> None:
        self.pool.connection.block_query = True

        async def transaction_task():
            async with self.database.transaction() as handle:
                await handle.fetch("BLOCK")

        task = asyncio.create_task(transaction_task())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.pool.connection.rollbacks, 1)
        self.assertEqual(self.pool.acquired, self.pool.released)
