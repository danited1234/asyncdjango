"""Small PostgreSQL foundation with direct asynchronous driver I/O."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg


class _RollbackOnly(Exception):
    """Internal signal used to make the driver roll back on normal exit."""


class TransactionConnection:
    """A transaction connection owned by the task that opened it.

    Sharing one connection across concurrent tasks is unsafe. Use separate pool
    connections for independent work; nested transaction() calls use savepoints.
    """

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection
        self._owner = asyncio.current_task()
        self._active = True
        self._rollback_stack = [False]

    def _check_owner(self) -> None:
        if not self._active:
            raise RuntimeError("Transaction connection is no longer active")
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("Transaction connection belongs to another task")

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        self._check_owner()
        return await self._connection.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        self._check_owner()
        return await self._connection.fetchrow(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        self._check_owner()
        return await self._connection.execute(query, *args)

    async def stream(
        self, query: str, *args: Any, batch_size: int = 100
    ) -> AsyncIterator[asyncpg.Record]:
        self._check_owner()
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        async for row in self._connection.cursor(query, *args, prefetch=batch_size):
            self._check_owner()
            yield row

    def set_rollback(self) -> None:
        """Roll back the current transaction level when its block exits."""
        self._check_owner()
        self._rollback_stack[-1] = True

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[TransactionConnection]:
        """Create a nested transaction (a PostgreSQL savepoint)."""
        self._check_owner()
        self._rollback_stack.append(False)
        try:
            try:
                async with self._connection.transaction():
                    yield self
                    if self._rollback_stack[-1]:
                        raise _RollbackOnly()
            except _RollbackOnly:
                pass
        finally:
            self._rollback_stack.pop()


class AsyncPostgres:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        if min_size < 1 or max_size < min_size:
            raise ValueError("Require 1 <= min_size <= max_size")
        self.dsn = dsn
        self.min_size = min_size
        self.max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self._pool is not None:
            raise RuntimeError("Database pool is already open")
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn, min_size=self.min_size, max_size=self.max_size
        )

    async def close(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database pool is not open; call connect() first")
        return self._pool

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self._require_pool().acquire() as connection:
            return await connection.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self._require_pool().acquire() as connection:
            return await connection.execute(query, *args)

    async def stream(
        self, query: str, *args: Any, batch_size: int = 100
    ) -> AsyncIterator[asyncpg.Record]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        async with self._require_pool().acquire() as connection:
            async with connection.transaction():
                async for row in connection.cursor(
                    query, *args, prefetch=batch_size
                ):
                    yield row

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[TransactionConnection]:
        """Open a dedicated connection for an awaited transaction.

        Only methods on the yielded object participate in this transaction.
        Database-level fetch()/execute() acquire other connections.
        """
        async with self._require_pool().acquire() as connection:
            handle = TransactionConnection(connection)
            try:
                try:
                    async with connection.transaction():
                        yield handle
                        if handle._rollback_stack[0]:
                            raise _RollbackOnly()
                except _RollbackOnly:
                    pass
            finally:
                handle._active = False
