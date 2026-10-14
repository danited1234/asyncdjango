"""Minimal ASGI lifespan wrapper for database pool startup and shutdown."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol


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
