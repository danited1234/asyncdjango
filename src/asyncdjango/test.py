"""Test helpers for code that uses the native async ORM.

Django's ``TestCase`` isolates tests with a transaction on its synchronous
connection. Native queries use separate pooled connections and cannot see
that transaction, so use these classes for async tests instead:

* ``NativeTestCase`` runs each test inside a native transaction that is
  rolled back afterwards (like Django's ``TestCase``). Fast; every ORM call in
  the test shares one connection.
* ``NativeTransactionTestCase`` lets each test commit for real and truncates
  the tables afterwards (like ``TransactionTestCase``). Use it to test
  concurrency, ``on_commit`` callbacks, or code that needs separate
  connections.

Run them with Django's test runner so the test databases exist.
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from typing import Any

from django.db import connections

from .db.bridge import greenlet_spawn
from .db.pool import close_pools
from .db.scope import NativeScope, _current_scope
from .db.transaction import Atomic


def _loop_factory() -> asyncio.AbstractEventLoop:
    # psycopg's async mode cannot use Windows' default ProactorEventLoop.
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


class _NativeAsyncTestCase(unittest.IsolatedAsyncioTestCase):
    # Django's test runner sets up the aliases listed here.
    databases: set[str] | str = {"default"}
    loop_factory = staticmethod(_loop_factory)

    if sys.version_info < (3, 13):
        # IsolatedAsyncioTestCase only honors loop_factory from Python 3.13.
        def _setupAsyncioRunner(self) -> None:
            assert self._asyncioRunner is None, "asyncio runner is already initialized"
            self._asyncioRunner = asyncio.Runner(debug=True, loop_factory=self.loop_factory)

    def _database_aliases(self) -> list[str]:
        return list(connections) if self.databases == "__all__" else sorted(self.databases)

    def _callTearDown(self) -> None:
        try:
            super()._callTearDown()
        finally:
            self._callAsync(close_pools)


class NativeTestCase(_NativeAsyncTestCase):
    """Each test runs in a native transaction that is rolled back afterwards.

    Every task in the test shares the transaction's connection, so a test can
    use ``asyncio.gather()``, but its queries run one at a time.
    """

    def _callSetUp(self) -> None:
        self._callAsync(self._enter_native_transaction)
        super()._callSetUp()

    def _callTearDown(self) -> None:
        try:
            super(_NativeAsyncTestCase, self)._callTearDown()
        finally:
            self._callAsync(self._exit_native_transaction)

    async def _enter_native_transaction(self) -> None:
        scope = NativeScope()
        scope.shared = True
        self._native_scope_token = _current_scope.set(scope)
        self._native_atomics = []
        for alias in self._database_aliases():
            atomic = Atomic(alias, True, False)
            atomic._from_testcase = True  # durable=True blocks are allowed inside
            await greenlet_spawn(atomic.__enter__)
            self._native_atomics.append((alias, atomic))

    async def _exit_native_transaction(self) -> None:
        from django.db import transaction

        scope = _current_scope.get()
        try:
            while self._native_atomics:
                alias, atomic = self._native_atomics.pop()

                def rollback() -> None:
                    transaction.set_rollback(True, using=alias)
                    atomic.__exit__(None, None, None)

                await greenlet_spawn(rollback)
        finally:
            _current_scope.reset(self._native_scope_token)
            await scope.aclose()
            await close_pools()


class NativeTransactionTestCase(_NativeAsyncTestCase):
    """Each test commits for real; tables are truncated after every test."""

    reset_sequences = False

    def tearDown(self) -> None:
        # Runs while the event loop is stopped, so synchronous Django is safe.
        # Same as TransactionTestCase: flush, then post_migrate re-creates
        # content types and permissions (and clears their caches).
        from django.core.management import call_command

        for alias in self._database_aliases():
            call_command("flush", verbosity=0, interactive=False, database=alias,
                         reset_sequences=self.reset_sequences, allow_cascade=True)
        super().tearDown()


class ASGIResponse:
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]],
                 body: bytes) -> None:
        self.status_code = status
        self.headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                        for key, value in headers}
        self.content = body

    def json(self) -> Any:
        import json

        return json.loads(self.content)


class ASGIClient:
    """Drive an ASGI application in-process, without an HTTP server."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def request(self, method: str, path: str, *, body: bytes = b"",
                      headers: dict[str, str] | None = None,
                      query_string: str = "") -> ASGIResponse:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string.encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver"),
                        (b"content-length", str(len(body)).encode())] + [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in (headers or {}).items()
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        response_started: dict[str, Any] = {}
        chunks: list[bytes] = []
        finished = asyncio.Event()

        async def receive() -> dict[str, Any]:
            if messages:
                return messages.pop(0)
            await finished.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                response_started.update(message)
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    finished.set()

        await self.app(scope, receive, send)
        return ASGIResponse(response_started["status"],
                            response_started.get("headers", []), b"".join(chunks))

    async def get(self, path: str, **kwargs: Any) -> ASGIResponse:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, data: Any = None, **kwargs: Any) -> ASGIResponse:
        return await self._send_json("POST", path, data, **kwargs)

    async def put(self, path: str, data: Any = None, **kwargs: Any) -> ASGIResponse:
        return await self._send_json("PUT", path, data, **kwargs)

    async def patch(self, path: str, data: Any = None, **kwargs: Any) -> ASGIResponse:
        return await self._send_json("PATCH", path, data, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> ASGIResponse:
        return await self.request("DELETE", path, **kwargs)

    async def _send_json(self, method: str, path: str, data: Any,
                         **kwargs: Any) -> ASGIResponse:
        import json

        headers = {"content-type": "application/json", **kwargs.pop("headers", {})}
        body = b"" if data is None else json.dumps(data).encode()
        return await self.request(method, path, body=body, headers=headers, **kwargs)
