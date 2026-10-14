"""Minimal ASGI lifespan wrapper for database pool startup and shutdown."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, AsyncIterable, Awaitable, Callable, Protocol

from .db.transaction import using_database


class DatabaseLifecycle(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...


ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[Any]], Callable[..., Awaitable[None]]], Awaitable[None]]


class DatabaseLifespan:
    """Open the database pool at ASGI startup and close it at shutdown."""

    def __init__(self, app: ASGIApp, database: DatabaseLifecycle) -> None:
        self.app = app
        self.database = database

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[Any]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.database.connect()
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                try:
                    await self.database.close()
                except Exception as exc:
                    await send({"type": "lifespan.shutdown.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return


class Request:
    def __init__(self, scope: dict[str, Any], body: bytes | None,
                 path_params: dict[str, str] | None = None,
                 stream_queue: asyncio.Queue[bytes | None] | None = None) -> None:
        self.scope = scope
        self.body = body
        self._stream_queue = stream_queue
        self._stream_started = False
        self.method = scope["method"]
        self.path = scope["path"]
        self.path_params = path_params or {}

    async def json(self) -> Any:
        """Decode a buffered body, or consume a streamed body once."""
        if self.body is None:
            self.body = b"".join([chunk async for chunk in self.stream()])
        return json.loads(self.body)

    async def stream(self):
        """Yield incoming chunks without waiting for the whole request body."""
        if self._stream_queue is None:
            yield self.body or b""
            return
        if self._stream_started:
            raise RuntimeError("A request body stream can only be read once")
        self._stream_started = True
        while True:
            chunk = await self._stream_queue.get()
            if chunk is None:
                return
            yield chunk


class JSONResponse:
    def __init__(self, data: Any, status_code: int = 200) -> None:
        self.status_code = status_code
        self.body = (b"" if status_code == 204 else
                     json.dumps(data, separators=(",", ":")).encode("utf-8"))

    async def send(self, send: Callable[..., Awaitable[None]]) -> None:
        await send({
            "type": "http.response.start",
            "status": self.status_code,
            "headers": ([] if self.status_code == 204 else
                        [(b"content-type", b"application/json")]),
        })
        await send({"type": "http.response.body", "body": self.body})


class StreamingResponse:
    """Send chunks as they arrive, without buffering the whole response."""

    def __init__(self, body: AsyncIterable[bytes], status_code: int = 200,
                 content_type: bytes = b"application/octet-stream") -> None:
        self.body = body
        self.status_code = status_code
        self.content_type = content_type

    async def send(self, send: Callable[..., Awaitable[None]]) -> None:
        await send({"type": "http.response.start", "status": self.status_code,
                    "headers": [(b"content-type", self.content_type)]})
        async for chunk in self.body:
            if not isinstance(chunk, bytes):
                raise TypeError("Streaming response chunks must be bytes")
            await send({"type": "http.response.body", "body": chunk,
                        "more_body": True})
        await send({"type": "http.response.body", "body": b""})


class _Disconnected(Exception):
    pass


Response = JSONResponse | StreamingResponse
Handler = Callable[[Request], Awaitable[Response]]
Middleware = Callable[[Request, Handler], Awaitable[Response]]


class ASGIApplication:
    """A small async HTTP router with database lifecycle and disconnect handling."""

    def __init__(self, database: DatabaseLifecycle, *, max_body_size: int = 1_048_576) -> None:
        if max_body_size < 1:
            raise ValueError("max_body_size must be positive")
        self.database = database
        self.max_body_size = max_body_size
        self._routes: list[tuple[str, str, re.Pattern[str], Handler, bool]] = []
        self._middleware: list[Middleware] = []
        self._lifespan = DatabaseLifespan(self._handle_http, database)

    def add_middleware(self, middleware: Middleware) -> None:
        """Register an async request/response wrapper; first added is outermost."""
        self._middleware.append(middleware)

    def add_route(self, path: str, handler: Handler, *, methods: tuple[str, ...] = ("GET",),
                  stream_request: bool = False) -> None:
        if not path.startswith("/"):
            raise ValueError("Route paths must start with /")
        pattern_parts = []
        for part in path.split("/"):
            if part.startswith("{") and part.endswith("}"):
                name = part[1:-1]
                if not name.isidentifier():
                    raise ValueError(f"Invalid route parameter: {name}")
                pattern_parts.append(f"(?P<{name}>[^/]+)")
            else:
                pattern_parts.append(re.escape(part))
        pattern = re.compile("/".join(pattern_parts))
        for method in methods:
            if any(existing_method == method.upper() and existing_path == path
                   for existing_method, existing_path, _, _, _ in self._routes):
                raise ValueError(f"Duplicate route: {(method.upper(), path)}")
            self._routes.append((method.upper(), path, pattern, handler, stream_request))

    async def __call__(self, scope, receive, send) -> None:
        await self._lifespan(scope, receive, send)

    async def _read_body(self, receive) -> bytes:
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _Disconnected
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body_size:
                raise ValueError("Request body is too large")
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    async def _wait_for_disconnect(self, receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return

    async def _pump_stream(self, receive, queue, body_done, too_large, disconnected) -> None:
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected.set()
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body_size:
                too_large.set()
                return
            if chunk:
                queue.put_nowait(chunk)
            if not message.get("more_body", False):
                queue.put_nowait(None)
                body_done.set()
                break
        await self._wait_for_disconnect(receive)
        disconnected.set()

    async def _handle_http(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            raise ValueError(f"Unsupported ASGI scope: {scope['type']}")
        path = scope["path"]
        method = scope["method"].upper()
        handler = None
        stream_request = False
        path_params = {}
        path_found = False
        for route_method, _, pattern, route_handler, route_stream_request in self._routes:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            path_found = True
            if route_method == method:
                handler = route_handler
                stream_request = route_stream_request
                path_params = match.groupdict()
                break
        if handler is None:
            status = 405 if path_found else 404
            await JSONResponse({"detail": "Not found" if status == 404 else "Method not allowed"}, status).send(send)
            return
        if stream_request:
            stream_queue = asyncio.Queue()
            body_done = asyncio.Event()
            too_large = asyncio.Event()
            disconnected = asyncio.Event()
            pump_task = asyncio.create_task(self._pump_stream(
                receive, stream_queue, body_done, too_large, disconnected
            ))
            request = Request(scope, None, path_params, stream_queue)
            disconnect_task = asyncio.create_task(disconnected.wait())
            too_large_task = asyncio.create_task(too_large.wait())
            body_done_task = asyncio.create_task(body_done.wait())
        else:
            try:
                body = await self._read_body(receive)
            except _Disconnected:
                return
            except ValueError:
                await JSONResponse({"detail": "Request body is too large"}, 413).send(send)
                return
            request = Request(scope, body, path_params)
            pump_task = None
            disconnect_task = asyncio.create_task(self._wait_for_disconnect(receive))
            too_large_task = None
            body_done_task = None

        async def dispatch(current_request: Request) -> Response:
            return await handler(current_request)

        next_handler = dispatch
        for middleware in reversed(self._middleware):
            inner = next_handler

            async def wrapped(current_request: Request, *, _middleware=middleware,
                              _inner=inner) -> Response:
                return await _middleware(current_request, _inner)

            next_handler = wrapped
        with using_database(self.database):
            try:
                handler_task = asyncio.create_task(next_handler(request))
                monitors = {disconnect_task}
                if too_large_task is not None:
                    monitors.add(too_large_task)
                done, _ = await asyncio.wait(
                    {handler_task, *monitors}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect_task in done or too_large_task in done:
                    handler_task.cancel()
                    await asyncio.gather(handler_task, return_exceptions=True)
                    if too_large_task in done and disconnect_task not in done:
                        await JSONResponse({"detail": "Request body is too large"}, 413).send(send)
                    return
                try:
                    response = await handler_task
                except Exception:
                    response = JSONResponse({"detail": "Internal server error"}, 500)
                if body_done_task is not None:
                    done, _ = await asyncio.wait(
                        {body_done_task, *monitors}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if disconnect_task in done:
                        return
                    if too_large_task in done:
                        await JSONResponse({"detail": "Request body is too large"}, 413).send(send)
                        return
                response_task = asyncio.create_task(response.send(send))
                done, _ = await asyncio.wait(
                    {response_task, *monitors}, return_when=asyncio.FIRST_COMPLETED
                )
                if disconnect_task in done or too_large_task in done:
                    response_task.cancel()
                    await asyncio.gather(response_task, return_exceptions=True)
                    return
                await response_task
            finally:
                pending = (disconnect_task, too_large_task, body_done_task, pump_task)
                for task in pending:
                    if task is not None:
                        task.cancel()
                await asyncio.gather(*(task for task in pending if task is not None),
                                     return_exceptions=True)
