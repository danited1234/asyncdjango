"""ASGI lifespan checks that require no external services."""

import asyncio
import unittest

from asyncdjango.asgi import DatabaseLifespan


class FakeDatabase:
    def __init__(self, *, fail_startup: bool = False) -> None:
        self.events: list[str] = []
        self.fail_startup = fail_startup

    async def connect(self) -> None:
        self.events.append("connect")
        if self.fail_startup:
            raise RuntimeError("connection failed")

    async def close(self) -> None:
        self.events.append("close")


class DatabaseLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_and_shutdown(self) -> None:
        database = FakeDatabase()
        sent: list[dict] = []
        messages = iter((
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ))

        async def child_app(scope, receive, send):
            self.fail("Lifespan messages must not reach child app")

        async def receive():
            return next(messages)

        async def send(message):
            sent.append(message)

        await DatabaseLifespan(child_app, database)(
            {"type": "lifespan"}, receive, send
        )
        self.assertEqual(database.events, ["connect", "close"])
        self.assertEqual(sent, [
            {"type": "lifespan.startup.complete"},
            {"type": "lifespan.shutdown.complete"},
        ])

    async def test_startup_failure(self) -> None:
        database = FakeDatabase(fail_startup=True)
        sent: list[dict] = []

        async def child_app(scope, receive, send):
            self.fail("Lifespan messages must not reach child app")

        async def receive():
            return {"type": "lifespan.startup"}

        async def send(message):
            sent.append(message)

        await DatabaseLifespan(child_app, database)(
            {"type": "lifespan"}, receive, send
        )
        self.assertEqual(sent, [
            {"type": "lifespan.startup.failed", "message": "connection failed"}
        ])

    async def test_http_goes_to_child(self) -> None:
        database = FakeDatabase()
        scopes: list[dict] = []

        async def child_app(scope, receive, send):
            scopes.append(scope)

        async def unused():
            await asyncio.sleep(0)

        await DatabaseLifespan(child_app, database)(
            {"type": "http", "path": "/"}, unused, unused
        )
        self.assertEqual(scopes, [{"type": "http", "path": "/"}])
        self.assertEqual(database.events, [])
