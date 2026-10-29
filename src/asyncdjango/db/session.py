"""Per-request session state on pooled connections (``SESSION_SQL``).

Schema-per-tenant apps run ``SET search_path`` for the current tenant, but a
pooled connection may have served another tenant last. With

    ASYNCDJANGO = {"SESSION_SQL": "tenancy.db.session_sql"}

``session_sql(connection)`` is called before every query and returns the SQL
that puts the session in the state this query needs, or None::

    def session_sql(connection):
        schema = connection.ops.quote_name(get_current_schema_name())
        return f"SET search_path TO {schema}, public"

The SQL runs only when the physical connection last ran different SQL. A SET
inside a transaction is undone if the transaction rolls back, so it is
trusted for the rest of that transaction, kept on commit, and forgotten on a
rollback or a rollback to a savepoint. Works for native and synchronous
connections alike.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from typing import Any

from django.utils.module_loading import import_string

from .. import conf

# Physical connection -> (SQL last run, the wrapper whose open transaction ran
# it, or None once it is committed or ran in autocommit).
_applied: weakref.WeakKeyDictionary[
    Any, tuple[str, weakref.ref[Any] | None]
] = weakref.WeakKeyDictionary()


_SAVEPOINT_SQL = ("SAVEPOINT ", "RELEASE SAVEPOINT ", "ROLLBACK TO SAVEPOINT ")


def _physical(connection: Any) -> Any:
    raw = connection.connection
    return getattr(raw, "async_connection", raw)


def _run(connection: Any, sql: str) -> None:
    with connection.wrap_database_errors, connection.connection.cursor() as cursor:
        cursor.execute(sql)


def _apply(execute: Callable[..., Any], sql: str, params: Any, many: bool,
           context: dict[str, Any]) -> Any:
    connection = context["connection"]
    # Not before Django's own SAVEPOINT / RELEASE / ROLLBACK TO SAVEPOINT: a SET
    # sent just before a rollback to savepoint would be undone by it.
    setup = None if sql.startswith(_SAVEPOINT_SQL) else connection._asyncdjango_session_sql(
        connection)
    if setup is not None:
        key = _physical(connection)
        state = _applied.get(key)
        if (state is None or state[0] != setup
                or (state[1] is not None and state[1]() is not connection)):
            _run(connection, setup)
            in_transaction = connection.in_atomic_block or not connection.get_autocommit()
            _applied[key] = (setup, weakref.ref(connection) if in_transaction else None)
    return execute(sql, params, many, context)


def _settle(connection: Any, committed: bool) -> None:
    if connection.connection is None:
        return
    key = _physical(connection)
    state = _applied.get(key)
    if state is not None and state[1] is not None and state[1]() is connection:
        if committed:
            _applied[key] = (state[0], None)
        else:
            del _applied[key]


def install(sender: Any, connection: Any, **kwargs: Any) -> None:
    """``connection_created`` receiver."""
    path = conf.get("SESSION_SQL")
    if not path or connection.vendor != "postgresql" or _apply in connection.execute_wrappers:
        return
    connection._asyncdjango_session_sql = import_string(path)
    # First, so that popping a caller's execute_wrapper() never removes it.
    connection.execute_wrappers.insert(0, _apply)
    commit, rollback = connection._commit, connection._rollback
    savepoint_rollback = connection._savepoint_rollback

    def _commit() -> None:
        commit()
        _settle(connection, committed=True)

    def _rollback() -> None:
        _settle(connection, committed=False)
        rollback()

    def _savepoint_rollback(sid: str) -> None:
        _settle(connection, committed=False)
        savepoint_rollback(sid)

    connection._commit = _commit
    connection._rollback = _rollback
    connection._savepoint_rollback = _savepoint_rollback
