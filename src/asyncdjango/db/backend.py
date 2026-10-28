"""A Django PostgreSQL ``DatabaseWrapper`` that performs I/O with psycopg async.

``NativeDatabaseWrapper`` is the stock Django PostgreSQL wrapper with one
change: its raw connection is a ``GreenletConnection``, a synchronous facade
over a pooled ``psycopg.AsyncConnection``. Every facade method that performs
I/O calls ``await_()``, so Django's unchanged compiler, transaction, signal, and
model code runs on the event loop while each query is awaited natively.

The facade only works inside ``greenlet_spawn()``. Outside it, ``await_()``
raises ``MissingNativeContext`` (a ``SynchronousOnlyOperation``), so accidental
synchronous ORM use from async code still fails loudly.
"""

from __future__ import annotations

from functools import cache
from importlib import import_module
from typing import Any

import psycopg
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.postgresql import base as pg_base
from django.db.backends.postgresql.psycopg_any import is_psycopg3
from psycopg import sql as psycopg_sql
from psycopg.client_cursor import ClientCursorMixin
from psycopg.pq import Format

from . import pool as pool_registry
from .bridge import await_, in_native_context


class AsyncServerSideCursor(ClientCursorMixin, psycopg.AsyncServerCursor):
    """Named cursor with client-side parameter binding (see Django's
    ``ServerSideCursor``: the ORM does not generate prepareable SQL)."""


class GreenletCursor:
    """Synchronous DB-API cursor facade over a psycopg async cursor."""

    def __init__(self, cursor: Any, connection: GreenletConnection) -> None:
        self._cursor = cursor
        self._connection = connection

    @property
    def connection(self) -> GreenletConnection:
        return self._connection

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> GreenletCursor:
        await_(self._cursor.execute(query, params, **kwargs))
        return self

    def executemany(self, query: Any, params_seq: Any, **kwargs: Any) -> None:
        await_(self._cursor.executemany(query, params_seq, **kwargs))

    def fetchone(self) -> Any:
        return await_(self._cursor.fetchone())

    def fetchmany(self, size: int = 0) -> list[Any]:
        return await_(self._cursor.fetchmany(size))

    def fetchall(self) -> list[Any]:
        return await_(self._cursor.fetchall())

    def scroll(self, value: int, mode: str = "relative") -> None:
        await_(self._cursor.scroll(value, mode))

    def close(self) -> None:
        if self._cursor.closed:
            return
        if not in_native_context():
            # Reached from garbage collection of an abandoned iterator. A
            # client-side cursor holds no server state; a named cursor is
            # closed when its connection is returned to the pool.
            return
        await_(self._cursor.close())

    def callproc(self, name: Any, args: Any = None) -> Any:
        # Same SQL as Django's psycopg CursorMixin.callproc().
        if not isinstance(name, psycopg_sql.Identifier):
            name = psycopg_sql.Identifier(name)
        parts = [psycopg_sql.SQL("SELECT * FROM "), name, psycopg_sql.SQL("(")]
        if args:
            for item in args:
                parts.extend((psycopg_sql.Literal(item), psycopg_sql.SQL(",")))
            del parts[-1]
        parts.append(psycopg_sql.SQL(")"))
        self.execute(psycopg_sql.Composed(parts))
        return args

    def copy(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "COPY is not available through the synchronous facade; use "
            "psycopg's async cursor.copy() on a raw connection."
        )

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row

    def __enter__(self) -> GreenletCursor:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __getattr__(self, name: str) -> Any:
        # Non-I/O attributes: description, rowcount, statusmessage, _query, ...
        return getattr(self._cursor, name)


class _ReleasedConnection:
    """Stands in for a connection returned to the pool; any use fails."""

    closed = True
    broken = False

    def __getattr__(self, name: str) -> Any:
        raise psycopg.InterfaceError("the connection was returned to the pool")


class GreenletConnection:
    """Synchronous facade over a pooled ``psycopg.AsyncConnection``."""

    def __init__(self, connection: psycopg.AsyncConnection, alias: str) -> None:
        self._aconn = connection
        self.alias = alias

    @property
    def async_connection(self) -> psycopg.AsyncConnection:
        """The pooled connection behind this facade.

        Each ORM call gets a new facade, but the pooled connection is the same
        object on every checkout, so per-session state (``SET search_path``,
        for example) can be keyed by it.
        """
        return self._aconn

    def detach(self) -> psycopg.AsyncConnection | None:
        """Forget the pooled connection so stale wrappers cannot reuse it."""
        connection = self._aconn
        self._aconn = _ReleasedConnection()
        return None if isinstance(connection, _ReleasedConnection) else connection

    def cursor(self, *args: Any, **kwargs: Any) -> GreenletCursor:
        return GreenletCursor(self._aconn.cursor(*args, **kwargs), self)

    def commit(self) -> None:
        await_(self._aconn.commit())

    def rollback(self) -> None:
        await_(self._aconn.rollback())

    def close(self) -> None:
        await_(self._aconn.close())

    @property
    def autocommit(self) -> bool:
        return self._aconn.autocommit

    @autocommit.setter
    def autocommit(self, value: bool) -> None:
        await_(self._aconn.set_autocommit(value))

    @property
    def isolation_level(self) -> Any:
        return self._aconn.isolation_level

    @isolation_level.setter
    def isolation_level(self, value: Any) -> None:
        await_(self._aconn.set_isolation_level(value))

    def __getattr__(self, name: str) -> Any:
        # Non-I/O attributes: info, adapters, pgconn, closed, broken, ...
        return getattr(self._aconn, name)


def _unwrap(function: Any) -> Any:
    # Django guards these with @async_unsafe because they would block the
    # event loop. Here they only reach the database through await_(), which
    # refuses to run outside a native greenlet, so the guard is replaced.
    return getattr(function, "__wrapped__", function)


class NativeWrapperMixin:
    """Mixed into a PostgreSQL ``DatabaseWrapper`` class (see ``wrapper_class``)."""

    connect = _unwrap(BaseDatabaseWrapper.connect)
    ensure_connection = _unwrap(BaseDatabaseWrapper.ensure_connection)
    cursor = _unwrap(BaseDatabaseWrapper.cursor)
    commit = _unwrap(BaseDatabaseWrapper.commit)
    rollback = _unwrap(BaseDatabaseWrapper.rollback)
    close = _unwrap(BaseDatabaseWrapper.close)
    savepoint = _unwrap(BaseDatabaseWrapper.savepoint)
    savepoint_rollback = _unwrap(BaseDatabaseWrapper.savepoint_rollback)
    savepoint_commit = _unwrap(BaseDatabaseWrapper.savepoint_commit)
    clean_savepoints = _unwrap(BaseDatabaseWrapper.clean_savepoints)
    chunked_cursor = _unwrap(pg_base.DatabaseWrapper.chunked_cursor)

    # Django's synchronous psycopg pool is never used on the native path.
    pool = None

    def close_pool(self) -> None:
        pass

    def get_connection_params(self) -> dict[str, Any]:
        params = super().get_connection_params()
        server_side_binding = self.settings_dict["OPTIONS"].get("server_side_binding")
        params["cursor_factory"] = (
            psycopg.AsyncCursor if server_side_binding is True else psycopg.AsyncClientCursor
        )
        params["autocommit"] = True
        return params

    def get_new_connection(self, conn_params: dict[str, Any]) -> GreenletConnection:
        isolation_level = self.settings_dict["OPTIONS"].get("isolation_level")
        self.isolation_level = (
            pg_base.IsolationLevel.READ_COMMITTED
            if isolation_level is None
            else pg_base.IsolationLevel(isolation_level)
        )
        connection = await_(pool_registry.acquire(self.alias))
        # Seed Django's cached server version so feature checks need no query.
        self.__dict__.setdefault("pg_version", connection.info.server_version)
        return GreenletConnection(connection, self.alias)

    def init_connection_state(self) -> None:
        # The pool's configure callback already applied time zone, role, and
        # isolation level when the physical connection was opened.
        BaseDatabaseWrapper.init_connection_state(self)

    def _close(self) -> None:
        if self.connection is not None:
            for cursor in self._take_named_cursors():
                with self.wrap_database_errors:
                    await_(cursor.close())
            connection = self.connection.detach()
            if connection is not None:
                with self.wrap_database_errors:
                    await_(pool_registry.release(self.alias, connection))

    async def arelease(self) -> None:
        """Return the pooled connection without entering a greenlet."""
        if self.connection is not None:
            cursors = self._take_named_cursors()
            connection = self.connection.detach()
            self.connection = None
            if connection is None:
                return
            try:
                if not connection.closed and not connection.broken:
                    for cursor in cursors:
                        await cursor.close()
            except psycopg.Error:
                # The pool discards or resets a broken connection.
                pass
            await pool_registry.release(self.alias, connection)

    def _take_named_cursors(self) -> list[Any]:
        cursors = [cursor for cursor in self.__dict__.pop("_named_cursors", ())
                   if not cursor.closed]
        return cursors

    def create_cursor(self, name: str | None = None) -> GreenletCursor:
        connection = self.connection._aconn
        if name:
            if self.settings_dict["OPTIONS"].get("server_side_binding") is not True:
                cursor = AsyncServerSideCursor(
                    connection, name=name, scrollable=False, withhold=connection.autocommit
                )
            else:
                cursor = connection.cursor(
                    name, scrollable=False, withhold=connection.autocommit
                )
            # Close cursors left open by abandoned iterators before release.
            named = [c for c in self.__dict__.get("_named_cursors", ()) if not c.closed]
            named.append(cursor)
            self._named_cursors = named
        else:
            cursor = connection.cursor()
        tzloader = connection.adapters.get_loader(pg_base.TIMESTAMPTZ_OID, Format.TEXT)
        if self.timezone != tzloader.timezone:
            pg_base.register_tzloader(self.timezone, cursor)
        return GreenletCursor(cursor, self.connection)


@cache
def wrapper_class(engine: str) -> type | None:
    """Return the native wrapper class for an ENGINE, or None if unsupported."""
    if not is_psycopg3:
        return None
    try:
        backend = import_module(f"{engine}.base")
    except ImportError:
        return None
    base = getattr(backend, "DatabaseWrapper", None)
    if not (isinstance(base, type) and issubclass(base, pg_base.DatabaseWrapper)):
        return None
    return type(f"Native{base.__name__}", (NativeWrapperMixin, base), {})
