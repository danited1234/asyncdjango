"""Task-local database context and async transaction blocks."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Iterator


_current_database: ContextVar[Any | None] = ContextVar(
    "asyncdjango_current_database", default=None
)


def current_database() -> Any | None:
    return _current_database.get()


@contextmanager
def using_database(database: Any) -> Iterator[None]:
    """Bind a database pool to the current async task context."""
    token = _current_database.set(database)
    try:
        yield
    finally:
        _current_database.reset(token)


@asynccontextmanager
async def atomic() -> AsyncIterator[Any]:
    """Start a transaction or nested savepoint on the current database."""
    database = current_database()
    if database is None:
        raise RuntimeError("Use using_database(database) before transaction.atomic()")
    async with database.transaction() as connection:
        with using_database(connection):
            yield connection
