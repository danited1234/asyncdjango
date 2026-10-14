"""In-process async ASGI test client with lifespan and streaming support."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, AsyncIterable

from .asgi import ASGIApplication

_NOT_GIVEN = object()


@dataclass(frozen=True)
class TestResponse:
    status_code: int
    headers: dict[bytes, bytes]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class AsyncTestClient:
    def __init__(self, application: ASGIApplication) -> None:
        self.application = application
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_inbox: asyncio.Queue[dict[str, Any]] | None = None
        self._lifespan_outbox: asyncio.Queue[dict[str, Any]] | None = None

    async def __aenter__(self) -> "AsyncTestClient":
        self._lifespan_inbox = asyncio.Queue()
        self._lifespan_outbox = asyncio.Queue()
        self._lifespan_task = asyncio.create_task(self.application(
            {"type": "lifespan"}, self._lifespan_inbox.get, self._lifespan_outbox.put
        ))
        await self._lifespan_inbox.put({"type": "lifespan.startup"})
        message = await self._lifespan_outbox.get()
        if message["type"] != "lifespan.startup.complete":
            await self._lifespan_task
            raise RuntimeError(message.get("message", "ASGI startup failed"))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        assert self._lifespan_inbox is not None
        assert self._lifespan_outbox is not None
        assert self._lifespan_task is not None
        await self._lifespan_inbox.put({"type": "lifespan.shutdown"})
        message = await self._lifespan_outbox.get()
        await self._lifespan_task
        if message["type"] != "lifespan.shutdown.complete":
            raise RuntimeError(message.get("message", "ASGI shutdown failed"))

    async def request(self, method: str, path: str, *, body: bytes | AsyncIterable[bytes] = b"",
                      json_data: Any = _NOT_GIVEN,
                      headers: dict[str, str] | None = None) -> TestResponse:
        if json_data is not _NOT_GIVEN:
            if body != b"":
                raise ValueError("body and json_data are mutually exclusive")
            body = json.dumps(json_data).encode()
            headers = {**(headers or {}), "content-type": "application/json"}
        inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        sent: list[dict[str, Any]] = []

        async def supply_body() -> None:
            if isinstance(body, bytes):
                await inbox.put({"type": "http.request", "body": body})
            else:
                async for chunk in body:
                    await inbox.put({"type": "http.request", "body": chunk,
                                     "more_body": True})
                await inbox.put({"type": "http.request", "body": b""})

        producer = asyncio.create_task(supply_body())
        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope = {"type": "http", "method": method.upper(), "path": path,
                 "headers": [(key.lower().encode(), value.encode())
                             for key, value in (headers or {}).items()]}
        try:
            await self.application(scope, inbox.get, send)
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
        start = next(message for message in sent if message["type"] == "http.response.start")
        return TestResponse(start["status"], dict(start["headers"]), b"".join(
            message.get("body", b"") for message in sent
            if message["type"] == "http.response.body"
        ))

    async def get(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("POST", path, **kwargs)
