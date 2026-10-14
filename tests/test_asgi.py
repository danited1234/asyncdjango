"""ASGI lifespan checks that require no external services."""

import asyncio
import json
import unittest

from asyncdjango.asgi import ASGIApplication, DatabaseLifespan, JSONResponse, StreamingResponse


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


class HTTPPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_middleware_order_and_short_circuit(self) -> None:
        app = ASGIApplication(FakeDatabase())
        events = []

        async def view(request):
            events.append("view")
            return JSONResponse({"ok": True})

        async def outer(request, next_handler):
            events.append("outer-in")
            response = await next_handler(request)
            events.append("outer-out")
            return response

        async def inner(request, next_handler):
            events.append("inner-in")
            response = await next_handler(request)
            events.append("inner-out")
            return response

        app.add_route("/", view)
        app.add_middleware(outer)
        app.add_middleware(inner)
        sent = []
        inbox = asyncio.Queue()
        inbox.put_nowait({"type": "http.request", "body": b""})

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": "GET", "path": "/"}, inbox.get, send)
        self.assertEqual(events, ["outer-in", "inner-in", "view", "inner-out", "outer-out"])
        self.assertEqual(sent[0]["status"], 200)

        async def reject(request, next_handler):
            return JSONResponse({"detail": "denied"}, 403)

        app.add_middleware(reject)
        events.clear()
        sent.clear()
        inbox.put_nowait({"type": "http.request", "body": b""})
        await app({"type": "http", "method": "GET", "path": "/"}, inbox.get, send)
        self.assertNotIn("view", events)
        self.assertEqual(sent[0]["status"], 403)

    async def test_streaming_response_preserves_chunk_boundaries(self) -> None:
        sent = []

        async def chunks():
            yield b"first"
            await asyncio.sleep(0)
            yield b"second"

        async def send(message):
            sent.append(message)

        await StreamingResponse(chunks()).send(send)
        self.assertEqual([message["body"] for message in sent[1:]],
                         [b"first", b"second", b""])
        self.assertTrue(sent[1]["more_body"])
        self.assertFalse(sent[-1].get("more_body", False))

    async def test_disconnect_cancels_streaming_response(self) -> None:
        app = ASGIApplication(FakeDatabase())
        started = asyncio.Event()
        cancelled = asyncio.Event()
        inbox = asyncio.Queue()
        inbox.put_nowait({"type": "http.request", "body": b""})
        sent = []

        async def body():
            try:
                yield b"first"
                started.set()
                await asyncio.sleep(10)
                yield b"never"
            finally:
                cancelled.set()

        async def view(request):
            return StreamingResponse(body())

        async def send(message):
            sent.append(message)

        app.add_route("/stream", view)
        call = asyncio.create_task(app(
            {"type": "http", "method": "GET", "path": "/stream"}, inbox.get, send
        ))
        await asyncio.wait_for(started.wait(), 2)
        inbox.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(call, 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual([item.get("body") for item in sent[1:]], [b"first"])

    async def test_streamed_request_reaches_handler_before_body_complete(self) -> None:
        app = ASGIApplication(FakeDatabase())
        first_seen = asyncio.Event()
        inbox = asyncio.Queue()
        sent = []

        async def view(request):
            chunks = []
            async for chunk in request.stream():
                chunks.append(chunk.decode())
                first_seen.set()
            return JSONResponse({"chunks": chunks})

        async def send(message):
            sent.append(message)

        app.add_route("/upload", view, methods=("POST",), stream_request=True)
        inbox.put_nowait({"type": "http.request", "body": b"first", "more_body": True})
        call = asyncio.create_task(app(
            {"type": "http", "method": "POST", "path": "/upload"}, inbox.get, send
        ))
        await asyncio.wait_for(first_seen.wait(), 2)
        self.assertEqual(sent, [])
        inbox.put_nowait({"type": "http.request", "body": b"second", "more_body": False})
        await asyncio.wait_for(call, 2)
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(json.loads(sent[1]["body"]), {"chunks": ["first", "second"]})

    async def test_streamed_request_body_limit_even_when_handler_ignores_body(self) -> None:
        app = ASGIApplication(FakeDatabase(), max_body_size=3)
        inbox = asyncio.Queue()
        sent = []

        async def view(request):
            return JSONResponse({"accepted": True})

        async def send(message):
            sent.append(message)

        app.add_route("/upload", view, methods=("POST",), stream_request=True)
        inbox.put_nowait({"type": "http.request", "body": b"abcd"})
        await asyncio.wait_for(app(
            {"type": "http", "method": "POST", "path": "/upload"}, inbox.get, send
        ), 2)
        self.assertEqual(sent[0]["status"], 413)

    async def test_disconnect_cancels_streamed_request(self) -> None:
        app = ASGIApplication(FakeDatabase())
        first_seen = asyncio.Event()
        cancelled = asyncio.Event()
        inbox = asyncio.Queue()
        sent = []

        async def view(request):
            try:
                async for _ in request.stream():
                    first_seen.set()
            finally:
                cancelled.set()
            return JSONResponse({"ok": True})

        async def send(message):
            sent.append(message)

        app.add_route("/upload", view, methods=("POST",), stream_request=True)
        inbox.put_nowait({"type": "http.request", "body": b"first", "more_body": True})
        call = asyncio.create_task(app(
            {"type": "http", "method": "POST", "path": "/upload"}, inbox.get, send
        ))
        await asyncio.wait_for(first_seen.wait(), 2)
        inbox.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(call, 2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(sent, [])
